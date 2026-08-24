from __future__ import annotations

"""
CX2 2.0.10 Bounded Verification Execution.

Generic, explicit, bounded verification-command execution path that permits
legitimate runtime/cache/temp writes without changing the model turn from read-only.
Features true bounded, non-blocking stream capture for stdout and stderr.
"""

from dataclasses import asdict, dataclass, field
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    def start(self) -> None:
        if self.stream is None:
            return
        self.thread = threading.Thread(target=self._drain, daemon=True, name=f"cx-stream-reader-{self.stream_name}")
        self.thread.start()

    def _drain(self) -> None:
        if self.stream is None:
            return
        try:
            while True:
                chunk = self.stream.read(CHUNK_SIZE)
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
        except Exception:
            pass
        finally:
            try:
                self.stream.close()
            except Exception:
                pass

    def join(self, timeout: float = 5.0) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=timeout)

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


def kill_process_tree(pid: int) -> None:
    """Terminate the process and all of its descendants immediately."""
    if pid <= 0:
        return

    if sys.platform == "win32":
        try:
            # /F: Forcefully terminate process
            # /T: Terminate the specified process and any child processes started by it
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
        except Exception:
            pass
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


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
    guarantees full process-tree termination on timeout, cleans up temp profile, and returns real exit code.
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
    proc: subprocess.Popen | None = None
    stdout_reader: BoundedStreamReader | None = None
    stderr_reader: BoundedStreamReader | None = None

    try:
        if sys.platform == "win32":
            # On Windows, wrap raw_cmd in outer quotes for cmd.exe /c so internal quotes are preserved
            # and use /d to suppress registry AutoRun commands
            cmd_line = f'cmd.exe /d /c "{raw_cmd}"'
            proc = subprocess.Popen(
                cmd_line,
                cwd=str(resolved_cwd),
                env=env_profile.env_overrides,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
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

        stdout_reader = BoundedStreamReader(proc.stdout, max_bytes=max_stdout_bytes, stream_name="stdout")
        stderr_reader = BoundedStreamReader(proc.stderr, max_bytes=max_stderr_bytes, stream_name="stderr")
        stdout_reader.start()
        stderr_reader.start()

        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass

            if stdout_reader:
                stdout_reader.join(timeout=2.0)
            if stderr_reader:
                stderr_reader.join(timeout=2.0)

            stdout_text, stdout_trunc, stdout_total = stdout_reader.get_text() if stdout_reader else ("", False, 0)
            stderr_text, stderr_trunc, stderr_total = stderr_reader.get_text() if stderr_reader else ("", False, 0)

            duration_ms = int((time.monotonic() - t_start) * 1000)
            combined = (stdout_text + "\n" + stderr_text).strip() if stderr_text else stdout_text.strip()
            return BoundedExecutionResult(
                command=raw_cmd,
                cwd=str(resolved_cwd),
                exit_code=-1,
                stdout=stdout_text,
                stderr=stderr_text + f"\n[CX] Process tree terminated after timeout ({timeout}s).",
                duration_ms=duration_ms,
                output_snippet=f"Command timed out after {timeout}s",
                classification_text=combined or f"Command timed out after {timeout}s",
                bounded_host_execution=True,
                stdout_truncated=stdout_trunc,
                stderr_truncated=stderr_trunc,
                stdout_bytes_total=stdout_total,
                stderr_bytes_total=stderr_total,
            )

        # Normal completion: join stream readers
        if stdout_reader:
            stdout_reader.join(timeout=5.0)
        if stderr_reader:
            stderr_reader.join(timeout=5.0)

        duration_ms = int((time.monotonic() - t_start) * 1000)
        stdout_text, stdout_trunc, stdout_total = stdout_reader.get_text() if stdout_reader else ("", False, 0)
        stderr_text, stderr_trunc, stderr_total = stderr_reader.get_text() if stderr_reader else ("", False, 0)

        combined = (stdout_text + "\n" + stderr_text).strip() if stderr_text else stdout_text.strip()
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
        )

    except Exception as exc:
        if proc is not None and proc.poll() is None:
            kill_process_tree(proc.pid)

        if stdout_reader:
            stdout_reader.join(timeout=2.0)
        if stderr_reader:
            stderr_reader.join(timeout=2.0)

        duration_ms = int((time.monotonic() - t_start) * 1000)
        return BoundedExecutionResult(
            command=raw_cmd,
            cwd=str(resolved_cwd),
            exit_code=-1,
            stdout="",
            stderr=f"Spawn failure: {type(exc).__name__}: {exc}",
            duration_ms=duration_ms,
            output_snippet=f"Spawn failure: {exc}",
            classification_text=f"Spawn failure: {exc}",
            bounded_host_execution=True,
        )

    finally:
        env_profile.cleanup()
