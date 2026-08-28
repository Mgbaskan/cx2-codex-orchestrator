from __future__ import annotations

"""
CX2 2.0.10 Bounded Verification Execution.

Generic, explicit, bounded verification-command execution path that permits
legitimate runtime/cache/temp writes without changing the model turn from read-only.
Features true bounded, non-blocking stream capture for stdout and stderr.
"""

from dataclasses import asdict, dataclass, field
import ctypes
from ctypes import wintypes
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO

from verification_gate import (
    CommandExecutionSummary,
    classify_command_outcome,
)
from test_env import build_test_environment, ExecutionEnvironmentProfile


VERIFICATION_CATEGORIES = frozenset({"TEST", "BUILD", "TYPECHECK", "LINT"})

# Strict sandbox / write-restriction reason codes only.
# Generic environment or infrastructure init failures (e.g. ENVIRONMENT_INIT_FAILED)
# do NOT qualify unless specifically proven to be sandbox/temp/write restrictions.
BLOCKED_WRITE_REASON_CODES = frozenset({
    "SANDBOX_DENIED",
    "WORKSPACE_WRITE_REQUIRED",
    "TEMP_CACHE_UNAVAILABLE",
})

# Deterministic byte bounds for captured output retention
MAX_STDOUT_BYTES: int = 512 * 1024  # 512 KiB
MAX_STDERR_BYTES: int = 512 * 1024  # 512 KiB
CHUNK_SIZE: int = 64 * 1024        # 64 KiB read buffer
PROCESS_CLEANUP_TIMEOUT: float = 2.0
READER_CLEANUP_TIMEOUT: float = 2.0
# Windows may report a drained Job Object and completed parent wait before the
# kernel releases the final descendant current-directory reference. Keep that
# deferred object teardown inside the owned-resource cleanup contract.
WINDOWS_KERNEL_RELEASE_GRACE: float = 0.5

_WINDOWS_JOB_LAUNCHER = (
    "import subprocess,sys; "
    "command=sys.stdin.read(); "
    "child=subprocess.Popen('cmd.exe /d /c \"'+command+'\"'); "
    "raise SystemExit(child.wait())"
)


@dataclass
class BoundedExecutionResult:
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    output_snippet: str
    classification_text: str
    bounded_host_execution: bool = True
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes_total: int = 0
    stderr_bytes_total: int = 0
    process_tree_termination_attempted: bool = False
    process_tree_termination_verified: bool = False
    resource_cleanup_verified: bool = True
    cleanup_diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessTerminationOutcome:
    attempted: bool
    verified: bool
    return_code: int | None
    diagnostic: str


class _WindowsKillOnCloseJob:
    """Small, dependency-free Job Object wrapper for verified tree ownership."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        pass

    _ExtendedLimitInformation._fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    def __init__(self) -> None:
        self.handle: int | None = None
        self.diagnostic = "not initialized"

    @staticmethod
    def _kernel32() -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def assign(self, proc: subprocess.Popen[Any]) -> bool:
        if sys.platform != "win32":
            return False
        kernel32 = self._kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.diagnostic = f"CreateJobObjectW failed: {ctypes.get_last_error()}"
            return False
        info = self._ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle,
            wintypes.HANDLE(int(proc._handle)),
        )
        if not assigned:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            self.diagnostic = f"Job Object assignment failed: {error}"
            return False
        self.handle = int(handle)
        self.diagnostic = "process tree assigned to kill-on-close Job Object"
        return True

    def terminate(self) -> ProcessTerminationOutcome:
        if self.handle is None:
            return ProcessTerminationOutcome(False, False, None, self.diagnostic)
        kernel32 = self._kernel32()
        ok = bool(kernel32.TerminateJobObject(wintypes.HANDLE(self.handle), 1))
        error = 0 if ok else ctypes.get_last_error()
        drained = False
        if ok:
            deadline = time.monotonic() + PROCESS_CLEANUP_TIMEOUT
            while time.monotonic() < deadline:
                info = self._BasicAccountingInformation()
                returned = wintypes.DWORD()
                queried = kernel32.QueryInformationJobObject(
                    wintypes.HANDLE(self.handle),
                    self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                    ctypes.byref(returned),
                )
                if queried and info.ActiveProcesses == 0:
                    drained = True
                    break
                time.sleep(0.01)
        self.close()
        return ProcessTerminationOutcome(
            True,
            ok and drained,
            0 if ok else error,
            "Job Object tree termination verified"
            if ok and drained
            else "Job Object termination did not drain every process before deadline"
            if ok
            else f"TerminateJobObject failed: {error}",
        )

    def close(self) -> None:
        if self.handle is None:
            return
        kernel32 = self._kernel32()
        kernel32.CloseHandle(wintypes.HANDLE(self.handle))
        self.handle = None


class BoundedStreamReader:
    """
    Concurrent, non-blocking stream reader that drains a pipe in 64 KiB chunks
    and retains up to max_bytes in memory. Once the limit is reached, it continues
    draining and discarding excess bytes so the child process never deadlocks.
    """

    def __init__(self, stream: BinaryIO | None, max_bytes: int, stream_name: str = "output") -> None:
        self.stream = stream
        self.max_bytes = max(0, int(max_bytes))
        self.stream_name = stream_name
        self.buffer = bytearray()
        self.total_bytes = 0
        self.truncated = False
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.cleanup_close_requested = False
        self.stop_requested = threading.Event()

    def start(self) -> None:
        if self.stream is None:
            return
        self.thread = threading.Thread(target=self._drain, daemon=True, name=f"cx-stream-reader-{self.stream_name}")
        self.thread.start()

    def _drain(self) -> None:
        if self.stream is None:
            return
        try:
            read_chunk = getattr(self.stream, "read1", self.stream.read)
            while not self.stop_requested.is_set():
                # BufferedReader.read() may wait for the entire 64 KiB request;
                # read1() returns the bytes currently available from the pipe.
                chunk = read_chunk(CHUNK_SIZE)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if len(self.buffer) < self.max_bytes:
                    available = self.max_bytes - len(self.buffer)
                    self.buffer.extend(chunk[:available])
                    if len(chunk) > available:
                        self.truncated = True
                else:
                    self.truncated = True
        except Exception as exc:
            if not self.cleanup_close_requested:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self.stream.close()
            except Exception:
                pass

    def join(self, timeout: float = 5.0) -> bool:
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=timeout)
        return self.thread is None or not self.thread.is_alive()

    def request_pipe_close(self) -> tuple[bool, str]:
        """Cancel a pending read after process death without blocking the caller."""
        if self.stream is None:
            return True, f"{self.stream_name} pipe was absent"
        self.cleanup_close_requested = True
        self.stop_requested.set()
        if sys.platform == "win32" and self.thread is not None:
            native_id = self.thread.native_id
            if native_id is None:
                return False, f"{self.stream_name} reader had no native thread id"
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenThread.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenThread.restype = wintypes.HANDLE
            kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
            kernel32.CancelSynchronousIo.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            thread_terminate = 0x0001
            thread_handle = kernel32.OpenThread(thread_terminate, False, native_id)
            if not thread_handle:
                return False, (
                    f"{self.stream_name} reader OpenThread failed: "
                    f"WinError {ctypes.get_last_error()}"
                )
            cancelled = bool(kernel32.CancelSynchronousIo(thread_handle))
            error = 0 if cancelled else ctypes.get_last_error()
            kernel32.CloseHandle(thread_handle)
            # ERROR_NOT_FOUND means no synchronous I/O was pending at the
            # instant of cancellation; the bounded join remains authoritative.
            if cancelled or error == 1168:
                return True, f"{self.stream_name} reader I/O cancellation requested"
            return False, (
                f"{self.stream_name} reader I/O cancellation failed: WinError {error}"
            )

        raw_stream = getattr(self.stream, "raw", None)
        target = raw_stream if raw_stream is not None else self.stream
        try:
            target.close()
        except Exception as exc:
            return False, (
                f"{self.stream_name} raw pipe close failed: "
                f"{type(exc).__name__}: {exc}"
            )
        return True, f"{self.stream_name} raw pipe close requested"

    def cleanup_status(self) -> tuple[bool, str]:
        if self.thread is not None and self.thread.is_alive():
            return False, f"{self.stream_name} reader thread remained alive"
        if self.error:
            return False, f"{self.stream_name} reader failed: {self.error}"
        stream_closed = self.stream is None or bool(getattr(self.stream, "closed", False))
        if not stream_closed:
            try:
                self.stream.close()
            except Exception as exc:
                return False, (
                    f"{self.stream_name} pipe close failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            stream_closed = bool(getattr(self.stream, "closed", False))
        return (
            stream_closed,
            f"{self.stream_name} reader and pipe released"
            if stream_closed
            else f"{self.stream_name} pipe remained open",
        )

    def get_text(self) -> tuple[str, bool, int]:
        """
        Decode retained bytes using UTF-8 (with replacement for invalid sequences)
        and append truncation notice if excess bytes were discarded.
        """
        decoded = self.buffer.decode("utf-8", errors="replace")
        if self.truncated:
            decoded += f"\n... [CX truncated: {self.stream_name} exceeded limit ({self.total_bytes} bytes total)]"
        return decoded, self.truncated, self.total_bytes


def is_verification_command_eligible(
    summary: CommandExecutionSummary,
    *,
    permissions: str = ":read-only",
) -> bool:
    """
    Deterministically check if a command execution qualifies for bounded verification execution.

    Requirements:
      1. Turn permissions are read-only (:read-only).
      2. Command is classified under a verification category (TEST, BUILD, TYPECHECK, LINT).
      3. Command outcome is strictly BLOCKED due to a sandbox write/permission restriction.
      4. Conclusive project test failures (FAILED / TEST_FAILURE) are NEVER eligible.
      5. Generic infrastructure failures are NEVER eligible.
    """
    if str(permissions).strip().lower() != ":read-only":
        return False

    outcome = classify_command_outcome(summary)
    if outcome.outcome != "BLOCKED":
        return False

    if outcome.reason_code not in BLOCKED_WRITE_REASON_CODES:
        return False

    categories = set(summary.categories)
    if not categories.intersection(VERIFICATION_CATEGORIES):
        return False

    return True


def kill_process_tree(pid: int) -> ProcessTerminationOutcome:
    """Request tree termination and return evidence instead of swallowing it."""
    if pid <= 0:
        return ProcessTerminationOutcome(False, False, None, "invalid process id")

    if sys.platform == "win32":
        try:
            # /F: Forcefully terminate process
            # /T: Terminate the specified process and any child processes started by it
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=1.0,
            )
            return ProcessTerminationOutcome(
                True,
                False,
                completed.returncode,
                f"taskkill returned exit code {completed.returncode}; "
                "full tree quiescence was not independently verified",
            )
        except Exception as exc:
            return ProcessTerminationOutcome(
                True, False, None, f"taskkill failed: {type(exc).__name__}: {exc}"
            )
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            return ProcessTerminationOutcome(True, True, 0, "SIGKILL sent to process group")
        except Exception as group_exc:
            try:
                os.kill(pid, signal.SIGKILL)
                return ProcessTerminationOutcome(True, True, 0, "SIGKILL sent to process")
            except Exception as exc:
                return ProcessTerminationOutcome(
                    True,
                    False,
                    None,
                    "process-tree termination failed: "
                    f"group={type(group_exc).__name__}; process={type(exc).__name__}: {exc}",
                )


def _wait_for_parent_exit(
    proc: subprocess.Popen[Any], timeout: float = PROCESS_CLEANUP_TIMEOUT
) -> tuple[bool, str]:
    try:
        proc.wait(timeout=timeout)
    except Exception as exc:
        return False, f"parent wait failed: {type(exc).__name__}: {exc}"
    try:
        exited = proc.poll() is not None
    except Exception as exc:
        return False, f"parent poll failed: {type(exc).__name__}: {exc}"
    return (
        exited,
        "parent process exit verified"
        if exited
        else "parent process remained live after wait",
    )


def _terminate_windows_owned_tree(
    proc: subprocess.Popen[Any], job: _WindowsKillOnCloseJob
) -> ProcessTerminationOutcome:
    """Cover both Win32 parent relationships and Job Object membership."""
    taskkill = kill_process_tree(proc.pid)
    job_outcome = job.terminate()
    taskkill_succeeded = taskkill.return_code == 0
    verified = taskkill_succeeded and job_outcome.verified
    return ProcessTerminationOutcome(
        attempted=True,
        verified=verified,
        return_code=taskkill.return_code,
        diagnostic=(
            f"{taskkill.diagnostic}; {job_outcome.diagnostic}; "
            + (
                "Windows parent tree and Job Object drain verified"
                if verified
                else "combined Windows tree termination was not verified"
            )
        ),
    )


def _settle_readers(
    *readers: BoundedStreamReader | None,
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    verified = True
    for reader in readers:
        if reader is None:
            continue
        joined = reader.join(timeout=0.1)
        if not joined:
            close_ok, close_note = reader.request_pipe_close()
            verified = verified and close_ok
            notes.append(close_note)
            joined = reader.join(timeout=READER_CLEANUP_TIMEOUT)
        if not joined:
            verified = False
            notes.append(f"{reader.stream_name} reader join exceeded cleanup bound")
            continue
        settled, diagnostic = reader.cleanup_status()
        verified = verified and settled
        notes.append(diagnostic)
    return verified, notes


def _close_process_handle(proc: subprocess.Popen[Any] | None) -> tuple[bool, str]:
    if sys.platform != "win32" or proc is None:
        return True, "no explicit Windows process handle cleanup required"
    try:
        if proc.poll() is None:
            return False, "process handle retained because parent remained live"
    except Exception as exc:
        return False, f"process poll before handle close failed: {type(exc).__name__}: {exc}"
    process_handle = getattr(proc, "_handle", None)
    if process_handle is None:
        return True, "Windows process handle already released"
    close_handle = getattr(process_handle, "Close", None)
    if not callable(close_handle):
        return False, "Windows process handle did not expose a close operation"
    try:
        close_handle()
    except Exception as exc:
        return False, f"Windows process handle close failed: {type(exc).__name__}: {exc}"
    return True, "Windows process handle released"


def _verify_working_directory_release(
    cwd: Path, timeout: float = PROCESS_CLEANUP_TIMEOUT
) -> tuple[bool, str]:
    """Prove Windows would grant delete access without modifying the directory."""
    if sys.platform != "win32":
        return True, "working-directory release probe not required"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    delete_access = 0x00010000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
    invalid_handle = ctypes.c_void_p(-1).value
    deadline = time.monotonic() + max(0.0, timeout)
    last_error = 0
    while True:
        handle = kernel32.CreateFileW(
            str(cwd),
            delete_access,
            share_all,
            None,
            open_existing,
            backup_semantics,
            None,
        )
        handle_value = int(handle) if handle is not None else 0
        if handle_value not in (0, invalid_handle):
            kernel32.CloseHandle(wintypes.HANDLE(handle_value))
            return True, "working-directory delete access verified"
        last_error = ctypes.get_last_error()
        if time.monotonic() >= deadline:
            return (
                False,
                f"working-directory delete access remained blocked: WinError {last_error}",
            )
        time.sleep(0.01)


def _append_cx_diagnostic(existing: str, message: str) -> str:
    prefix = "\n" if existing else ""
    return f"{existing}{prefix}[CX] {message}"


def execute_bounded_verification_command(
    command: str,
    cwd: Path | str,
    *,
    timeout: float = 60.0,
    base_env: dict[str, str] | None = None,
    max_stdout_bytes: int = MAX_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_STDERR_BYTES,
) -> BoundedExecutionResult:
    """
    Execute an approved verification command in an isolated, bounded subprocess with clean temp environment.
    Concurrently drains stdout and stderr with deterministic memory bounds, enforces timeout,
    verifies owned process-tree and pipe cleanup, cleans up the temp profile,
    and returns the real exit code or truthful cleanup uncertainty.
    """
    raw_cmd = str(command or "").strip()
    if not raw_cmd:
        return BoundedExecutionResult(
            command="",
            cwd=str(cwd),
            exit_code=-1,
            stdout="",
            stderr="Empty command string provided.",
            duration_ms=0,
            output_snippet="Empty command string provided.",
            classification_text="Empty command string provided.",
            bounded_host_execution=True,
        )

    resolved_cwd = Path(cwd).resolve()
    if not resolved_cwd.exists() or not resolved_cwd.is_dir():
        return BoundedExecutionResult(
            command=raw_cmd,
            cwd=str(resolved_cwd),
            exit_code=-1,
            stdout="",
            stderr=f"Target working directory does not exist or is not a directory: {resolved_cwd}",
            duration_ms=0,
            output_snippet=f"Directory not found: {resolved_cwd}",
            classification_text=f"Target working directory does not exist: {resolved_cwd}",
            bounded_host_execution=True,
        )

    env_profile = build_test_environment(base_env=base_env or dict(os.environ))
    t_start = time.monotonic()
    proc: subprocess.Popen[Any] | None = None
    stdout_reader: BoundedStreamReader | None = None
    stderr_reader: BoundedStreamReader | None = None
    windows_job: _WindowsKillOnCloseJob | None = None
    termination = ProcessTerminationOutcome(False, False, None, "termination not required")
    timed_out = False
    exit_code = -1
    wait_verified = False
    cleanup_notes: list[str] = []
    execution_error: Exception | None = None

    try:
        if sys.platform == "win32":
            # The launcher waits for stdin before creating the command. This is
            # the race-free ownership point for the kill-on-close Job Object.
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _WINDOWS_JOB_LAUNCHER],
                cwd=str(resolved_cwd),
                env=env_profile.env_overrides,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
            )
            windows_job = _WindowsKillOnCloseJob()
            if not windows_job.assign(proc):
                if proc.stdin is not None:
                    proc.stdin.close()
                raise RuntimeError(
                    "Unable to establish verified Windows process-tree ownership: "
                    f"{windows_job.diagnostic}"
                )
            assert proc.stdin is not None
            proc.stdin.write(raw_cmd.encode("utf-8"))
            proc.stdin.close()
        else:
            proc = subprocess.Popen(
                raw_cmd,
                shell=True,
                cwd=str(resolved_cwd),
                env=env_profile.env_overrides,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )

        stdout_reader = BoundedStreamReader(
            proc.stdout, max_bytes=max_stdout_bytes, stream_name="stdout"
        )
        stderr_reader = BoundedStreamReader(
            proc.stderr, max_bytes=max_stderr_bytes, stream_name="stderr"
        )
        stdout_reader.start()
        stderr_reader.start()

        try:
            exit_code = proc.wait(timeout=timeout)
            wait_verified = True
        except subprocess.TimeoutExpired:
            timed_out = True

        if timed_out:
            termination = (
                _terminate_windows_owned_tree(proc, windows_job)
                if windows_job is not None and windows_job.handle is not None
                else kill_process_tree(proc.pid)
            )
            wait_verified, wait_note = _wait_for_parent_exit(proc)
            cleanup_notes.extend([termination.diagnostic, wait_note])
        elif windows_job is not None and windows_job.handle is not None:
            termination = windows_job.terminate()
            wait_verified, wait_note = _wait_for_parent_exit(proc)
            cleanup_notes.extend([termination.diagnostic, wait_note])

    except Exception as exc:
        execution_error = exc
        if proc is not None:
            try:
                alive = proc.poll() is None
            except Exception:
                alive = True
            if alive:
                termination = (
                    _terminate_windows_owned_tree(proc, windows_job)
                    if windows_job is not None and windows_job.handle is not None
                    else kill_process_tree(proc.pid)
                )
                cleanup_notes.append(termination.diagnostic)
                wait_verified, wait_note = _wait_for_parent_exit(proc)
                cleanup_notes.append(wait_note)

    readers_verified, reader_notes = _settle_readers(stdout_reader, stderr_reader)
    cleanup_notes.extend(reader_notes)
    handle_verified, handle_note = _close_process_handle(proc)
    cleanup_notes.append(handle_note)
    if windows_job is not None and windows_job.handle is not None:
        windows_job.close()
        cleanup_notes.append("Job Object closed without verified drain")
    if (
        sys.platform == "win32"
        and termination.verified
        and wait_verified
        and readers_verified
        and handle_verified
    ):
        time.sleep(WINDOWS_KERNEL_RELEASE_GRACE)
        cleanup_notes.append(
            f"Windows kernel handle-release grace completed "
            f"({WINDOWS_KERNEL_RELEASE_GRACE}s)"
        )
    cwd_verified, cwd_note = _verify_working_directory_release(resolved_cwd)
    cleanup_notes.append(cwd_note)

    profile_verified = True
    try:
        env_profile.cleanup()
        cleanup_notes.append("disposable execution environment removed")
    except Exception as exc:
        profile_verified = False
        cleanup_notes.append(
            f"disposable execution environment cleanup failed: "
            f"{type(exc).__name__}: {exc}"
        )

    tree_verified = termination.verified if termination.attempted else not timed_out
    resource_cleanup_verified = (
        wait_verified
        and readers_verified
        and handle_verified
        and cwd_verified
        and profile_verified
        and tree_verified
        and execution_error is None
    )
    cleanup_diagnostic = "; ".join(note for note in cleanup_notes if note)
    stdout_text, stdout_trunc, stdout_total = (
        stdout_reader.get_text() if stdout_reader else ("", False, 0)
    )
    stderr_text, stderr_trunc, stderr_total = (
        stderr_reader.get_text() if stderr_reader else ("", False, 0)
    )
    duration_ms = int((time.monotonic() - t_start) * 1000)

    if execution_error is not None:
        message = f"Spawn failure: {type(execution_error).__name__}: {execution_error}"
        if cleanup_diagnostic:
            message = _append_cx_diagnostic(message, cleanup_diagnostic[:160])
        return BoundedExecutionResult(
            command=raw_cmd,
            cwd=str(resolved_cwd),
            exit_code=-1,
            stdout=stdout_text,
            stderr=message,
            duration_ms=duration_ms,
            output_snippet=f"Spawn failure: {execution_error}",
            classification_text=f"Spawn failure: {execution_error}",
            bounded_host_execution=True,
            stdout_truncated=stdout_trunc,
            stderr_truncated=stderr_trunc,
            stdout_bytes_total=stdout_total,
            stderr_bytes_total=stderr_total,
            process_tree_termination_attempted=termination.attempted,
            process_tree_termination_verified=termination.verified,
            resource_cleanup_verified=False,
            cleanup_diagnostic=cleanup_diagnostic,
        )

    combined = (
        (stdout_text + "\n" + stderr_text).strip()
        if stderr_text
        else stdout_text.strip()
    )
    if timed_out:
        termination_message = (
            f"Process tree termination and owned-resource cleanup verified after "
            f"timeout ({timeout}s)."
            if resource_cleanup_verified
            else "Process-tree termination or owned-resource cleanup could not be "
            f"verified after timeout ({timeout}s): {cleanup_diagnostic[:96]}."
        )
        stderr_text = _append_cx_diagnostic(stderr_text, termination_message)
        return BoundedExecutionResult(
            command=raw_cmd,
            cwd=str(resolved_cwd),
            exit_code=-1,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_ms=duration_ms,
            output_snippet=f"Command timed out after {timeout}s",
            classification_text=combined or f"Command timed out after {timeout}s",
            bounded_host_execution=True,
            stdout_truncated=stdout_trunc,
            stderr_truncated=stderr_trunc,
            stdout_bytes_total=stdout_total,
            stderr_bytes_total=stderr_total,
            process_tree_termination_attempted=termination.attempted,
            process_tree_termination_verified=termination.verified,
            resource_cleanup_verified=resource_cleanup_verified,
            cleanup_diagnostic=cleanup_diagnostic,
        )

    if not resource_cleanup_verified:
        stderr_text = _append_cx_diagnostic(
            stderr_text,
            f"Owned-resource cleanup could not be verified: {cleanup_diagnostic[:96]}.",
        )
        combined = (stdout_text + "\n" + stderr_text).strip()
    snippet = combined[:500] if len(combined) > 500 else combined
    return BoundedExecutionResult(
        command=raw_cmd,
        cwd=str(resolved_cwd),
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        duration_ms=duration_ms,
        output_snippet=snippet,
        classification_text=combined,
        bounded_host_execution=True,
        stdout_truncated=stdout_trunc,
        stderr_truncated=stderr_trunc,
        stdout_bytes_total=stdout_total,
        stderr_bytes_total=stderr_total,
        process_tree_termination_attempted=termination.attempted,
        process_tree_termination_verified=termination.verified,
        resource_cleanup_verified=resource_cleanup_verified,
        cleanup_diagnostic=cleanup_diagnostic,
    )
