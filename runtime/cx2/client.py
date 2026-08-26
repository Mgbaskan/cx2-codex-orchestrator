from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable

from cx_home import resolve_cx_home

CX_HOME = resolve_cx_home()

CODEX_EXE = (
    CX_HOME
    / "runtime"
    / "venv"
    / "Lib"
    / "site-packages"
    / "codex_cli_bin"
    / "bin"
    / "codex.exe"
)

STAGE = (
    CX_HOME
    / "runtime"
    / "cx2"
)

STDERR_FILE = (
    STAGE
    / "app-server-stderr.log"
)

RESULT_FILE = (
    STAGE
    / "dispatcher-last.json"
)


from test_env import (
    build_test_environment,
    ExecutionEnvironmentProfile,
)


class AppServerProtocolError(
    RuntimeError
):
    pass


class _OmitType:
    pass


OMIT = _OmitType()


class AppServerClient:
    """
    Persistent bidirectional App Server transport.

    Routes three independent JSON-RPC message classes:

      1. Client request responses
      2. Server notifications
      3. Server -> client requests

    This becomes the transport core for CX 2.0.
    """

    def __init__(
        self,
        codex_exe: Path,
    ) -> None:

        self.codex_exe = codex_exe

        self.process: (
            subprocess.Popen[str]
            | None
        ) = None

        self._owned_env_profile: (
            ExecutionEnvironmentProfile
            | None
        ) = None

        self.launched_env: (
            dict[str, str]
            | None
        ) = None

        self.stderr_lines: list[str] = []

        self.notifications: queue.Queue[
            dict[str, Any]
        ] = queue.Queue()

        # Notifications retained by a selective terminal-boundary drain.
        # This backlog is client-owned so it survives individual turn runners.
        self._notification_backlog: deque[
            dict[str, Any]
        ] = deque()
        self._notification_drain_lock = threading.Lock()

        self.server_requests: queue.Queue[
            dict[str, Any]
        ] = queue.Queue()

        self.unknown_messages: queue.Queue[
            dict[str, Any]
        ] = queue.Queue()

        self._pending: dict[
            int,
            queue.Queue[dict[str, Any]],
        ] = {}

        self._pending_lock = (
            threading.Lock()
        )

        self._id_lock = (
            threading.Lock()
        )

        self._write_lock = (
            threading.Lock()
        )

        self._request_id = 0

        self._dispatcher_thread: (
            threading.Thread
            | None
        ) = None

        self._stderr_thread: (
            threading.Thread
            | None
        ) = None

    # =====================================================
    # Process
    # =====================================================

    def start(
        self,
        env: dict[str, str] | None = None,
    ) -> None:

        if not self.codex_exe.exists():
            raise FileNotFoundError(
                self.codex_exe
            )

        if self.process is not None:
            raise RuntimeError(
                "App Server zaten çalışıyor."
            )

        if env is None:
            self._owned_env_profile = (
                build_test_environment(
                    base_env=dict(
                        os.environ
                    )
                )
            )
            proc_env = (
                self._owned_env_profile.env_overrides
            )
        else:
            proc_env = dict(env)
            proc_env.setdefault(
                "PYTHONDONTWRITEBYTECODE",
                "1",
            )
            proc_env.setdefault(
                "GOTELEMETRY",
                "off",
            )

        self.launched_env = proc_env

        self.process = subprocess.Popen(
            [
                str(self.codex_exe),
                "app-server",
                "--stdio",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(
                Path.cwd()
            ),
            env=proc_env,
        )

        if (
            self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            raise RuntimeError(
                "App Server stdio pipes acilamadi."
            )

        self._dispatcher_thread = (
            threading.Thread(
                target=self._dispatch_loop,
                daemon=True,
                name="cx2-dispatcher",
            )
        )

        self._stderr_thread = (
            threading.Thread(
                target=self._stderr_loop,
                daemon=True,
                name="cx2-stderr",
            )
        )

        self._dispatcher_thread.start()
        self._stderr_thread.start()

    def close(self) -> None:

        process = self.process

        if process is None:
            error = {
                "__cx2_transport_error__":
                    "App Server client closed"
            }
            with self._pending_lock:
                pending = list(self._pending.values())
            for waiter in pending:
                try:
                    waiter.put_nowait(error)
                except Exception:
                    pass
            return

        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass

        try:
            process.wait(
                timeout=2.0
            )

        except subprocess.TimeoutExpired:
            process.terminate()

            try:
                process.wait(
                    timeout=2.0
                )

            except subprocess.TimeoutExpired:
                process.kill()

                process.wait(
                    timeout=2.0
                )

        if self._dispatcher_thread:
            self._dispatcher_thread.join(
                timeout=1.0
            )

        if self._stderr_thread:
            self._stderr_thread.join(
                timeout=1.0
            )

        try:
            if process.stdout:
                process.stdout.close()
        except Exception:
            pass

        try:
            if process.stderr:
                process.stderr.close()
        except Exception:
            pass

        error = {
            "__cx2_transport_error__":
                "App Server client closed"
        }
        with self._pending_lock:
            pending = list(self._pending.values())
        for waiter in pending:
            try:
                waiter.put_nowait(error)
            except Exception:
                pass

        try:
            STDERR_FILE.write_text(
                "".join(
                    self.stderr_lines
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        if self._owned_env_profile is not None:
            try:
                self._owned_env_profile.cleanup()
            except Exception:
                pass
            self._owned_env_profile = None

        self.process = None

    # =====================================================
    # Reader / dispatcher
    # =====================================================

    def _stderr_loop(self) -> None:

        assert self.process is not None
        assert self.process.stderr is not None

        for line in self.process.stderr:
            self.stderr_lines.append(
                line
            )

    def _dispatch_loop(self) -> None:

        assert self.process is not None
        assert self.process.stdout is not None

        try:
            for raw_line in self.process.stdout:

                line = raw_line.rstrip(
                    "\r\n"
                )

                if not line:
                    continue

                try:
                    message = json.loads(
                        line
                    )

                except json.JSONDecodeError:
                    self.unknown_messages.put(
                        {
                            "type": "invalid-json",
                            "raw": line,
                        }
                    )

                    continue

                if not isinstance(
                    message,
                    dict,
                ):
                    self.unknown_messages.put(
                        {
                            "type": "non-object",
                            "value": message,
                        }
                    )

                    continue

                self._route_message(
                    message
                )

        finally:
            error = {
                "__cx2_transport_error__":
                    "App Server stdout closed"
            }

            with self._pending_lock:
                pending = list(
                    self._pending.values()
                )

            for waiter in pending:
                try:
                    waiter.put_nowait(
                        error
                    )
                except queue.Full:
                    pass

    def _route_message(
        self,
        message: dict[str, Any],
    ) -> None:

        has_id = (
            "id" in message
        )

        has_method = (
            isinstance(
                message.get("method"),
                str,
            )
        )

        # ---------------------------------------------
        # Server -> client request
        # ---------------------------------------------

        if has_id and has_method:
            self.server_requests.put(
                message
            )
            return

        # ---------------------------------------------
        # Notification
        # ---------------------------------------------

        if has_method:
            self.notifications.put(
                message
            )
            return

        # ---------------------------------------------
        # Response to one of our requests
        # ---------------------------------------------

        if has_id:
            request_id = message.get(
                "id"
            )

            with self._pending_lock:
                waiter = (
                    self._pending.get(
                        request_id
                    )
                )

            if waiter is not None:
                waiter.put(
                    message
                )
                return

        # ---------------------------------------------
        # Unknown
        # ---------------------------------------------

        self.unknown_messages.put(
            message
        )

    # =====================================================
    # Transport
    # =====================================================

    def send(
        self,
        message: dict[str, Any],
    ) -> None:

        process = self.process

        if (
            process is None
            or process.stdin is None
        ):
            raise RuntimeError(
                "App Server çalışmıyor."
            )

        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self._write_lock:
            try:
                process.stdin.write(
                    payload + "\n"
                )

                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AppServerProtocolError(
                    f"App Server pipe broken: {exc}"
                ) from exc

    def _next_request_id(
        self,
    ) -> int:

        with self._id_lock:
            self._request_id += 1
            return self._request_id

    # =====================================================
    # Client -> server request
    # =====================================================

    def request(
        self,
        method: str,
        params: Any = OMIT,
        timeout: float = 15.0,
    ) -> Any:

        request_id = (
            self._next_request_id()
        )

        waiter: queue.Queue[
            dict[str, Any]
        ] = queue.Queue(
            maxsize=1
        )

        with self._pending_lock:
            self._pending[
                request_id
            ] = waiter

        message: dict[
            str,
            Any,
        ] = {
            "id": request_id,
            "method": method,
        }

        if params is not OMIT:
            message[
                "params"
            ] = params

        try:
            self.send(
                message
            )

            try:
                response = waiter.get(
                    timeout=timeout
                )

            except queue.Empty as exc:
                raise TimeoutError(
                    f"{method} response timeout."
                ) from exc

        finally:
            with self._pending_lock:
                self._pending.pop(
                    request_id,
                    None,
                )

        transport_error = (
            response.get(
                "__cx2_transport_error__"
            )
        )

        if transport_error:
            raise AppServerProtocolError(
                transport_error
            )

        if "error" in response:
            raise AppServerProtocolError(
                json.dumps(
                    response["error"],
                    ensure_ascii=False,
                )
            )

        if "result" not in response:
            raise AppServerProtocolError(
                f"{method}: result yok: "
                f"{response!r}"
            )

        return response[
            "result"
        ]

    # =====================================================
    # Client notification
    # =====================================================

    def notify(
        self,
        method: str,
        params: Any = OMIT,
    ) -> None:

        message: dict[
            str,
            Any,
        ] = {
            "method": method,
        }

        if params is not OMIT:
            message[
                "params"
            ] = params

        self.send(
            message
        )

    # =====================================================
    # Reply to server request
    # =====================================================

    def respond(
        self,
        request_id: Any,
        result: Any,
    ) -> None:

        self.send(
            {
                "id": request_id,
                "result": result,
            }
        )

    def respond_error(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:

        self.send(
            {
                "id": request_id,
                "error": {
                    "code": code,
                    "message": message,
                },
            }
        )

    # =====================================================
    # Event queues
    # =====================================================

    @staticmethod
    def _drain(
        source: queue.Queue[
            dict[str, Any]
        ],
    ) -> list[dict[str, Any]]:

        result: list[
            dict[str, Any]
        ] = []

        while True:
            try:
                result.append(
                    source.get_nowait()
                )

            except queue.Empty:
                break

        return result

    def drain_notifications(
        self,
    ) -> list[dict[str, Any]]:

        with self._notification_drain_lock:
            result = list(
                self._notification_backlog
            )
            self._notification_backlog.clear()
            result.extend(
                self._drain(
                    self.notifications
                )
            )
            return result

    def drain_matching_notifications(
        self,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> list[dict[str, Any]]:
        """Consume matching notifications while preserving all others in FIFO order."""

        with self._notification_drain_lock:
            available = list(
                self._notification_backlog
            )
            self._notification_backlog.clear()
            available.extend(
                self._drain(
                    self.notifications
                )
            )

            matched: list[dict[str, Any]] = []
            for notification in available:
                if predicate(notification):
                    matched.append(notification)
                else:
                    self._notification_backlog.append(
                        notification
                    )

            return matched

    def drain_server_requests(
        self,
    ) -> list[dict[str, Any]]:

        return self._drain(
            self.server_requests
        )

    def drain_unknown(
        self,
    ) -> list[dict[str, Any]]:

        return self._drain(
            self.unknown_messages
        )


# =========================================================
# Synthetic zero-process dispatcher regression
# =========================================================

def dispatcher_regression() -> None:

    client = AppServerClient(
        CODEX_EXE
    )

    # Response branch
    waiter: queue.Queue[
        dict[str, Any]
    ] = queue.Queue(
        maxsize=1
    )

    with client._pending_lock:
        client._pending[
            999
        ] = waiter

    client._route_message(
        {
            "id": 999,
            "result": {
                "ok": True,
            },
        }
    )

    response = waiter.get_nowait()

    assert (
        response["result"]["ok"]
        is True
    )

    # Notification branch
    client._route_message(
        {
            "method": "turn/started",
            "params": {
                "synthetic": True,
            },
        }
    )

    notifications = (
        client.drain_notifications()
    )

    assert len(
        notifications
    ) == 1

    assert (
        notifications[0][
            "method"
        ]
        == "turn/started"
    )

    # Server request branch
    client._route_message(
        {
            "id": "server-test-1",
            "method":
                "item/fileChange/requestApproval",
            "params": {
                "synthetic": True,
            },
        }
    )

    requests = (
        client.drain_server_requests()
    )

    assert len(
        requests
    ) == 1

    assert (
        requests[0][
            "id"
        ]
        == "server-test-1"
    )

    # Unknown branch
    client._route_message(
        {
            "unexpected": True,
        }
    )

    unknown = (
        client.drain_unknown()
    )

    assert len(
        unknown
    ) == 1


# =========================================================
# Real App Server validation
# =========================================================

def main() -> int:

    print(
        "=== CX2 BIDIRECTIONAL DISPATCHER ==="
    )

    dispatcher_regression()

    print(
        "Synthetic routing : PASS"
    )

    client = AppServerClient(
        CODEX_EXE
    )

    try:
        client.start()

        print(
            "Process           : STARTED"
        )

        initialize = client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "cx2",
                    "title": "CX 2.0",
                    "version": "2.0.3",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                    "optOutNotificationMethods": None,
                },
            },
            timeout=15.0,
        )

        if not isinstance(
            initialize,
            dict,
        ):
            raise AppServerProtocolError(
                "Initialize result object değil."
            )

        print(
            "Initialize        : OK"
        )

        client.notify(
            "initialized",
            {},
        )

        print(
            "Initialized       : SENT"
        )

        # -------------------------------------------------
        # Concurrent request routing test.
        #
        # account/rateLimits/read is a metadata RPC.
        # It does not start a model turn.
        # -------------------------------------------------

        def read_rate_limits() -> Any:

            return client.request(
                "account/rateLimits/read",
                timeout=15.0,
            )

        with ThreadPoolExecutor(
            max_workers=2
        ) as executor:

            future_a = executor.submit(
                read_rate_limits
            )

            future_b = executor.submit(
                read_rate_limits
            )

            result_a = (
                future_a.result()
            )

            result_b = (
                future_b.result()
            )

        print(
            "Concurrent RPC A  : OK"
        )

        print(
            "Concurrent RPC B  : OK"
        )

        # Give async notifications a very small window
        # to reach the dispatcher.
        time.sleep(
            0.25
        )

        notifications = (
            client.drain_notifications()
        )

        server_requests = (
            client.drain_server_requests()
        )

        unknown = (
            client.drain_unknown()
        )

        notification_methods = [
            item.get(
                "method"
            )
            for item in notifications
        ]

        server_request_methods = [
            item.get(
                "method"
            )
            for item in server_requests
        ]

        print(
            "Notifications     :",
            len(
                notifications
            ),
        )

        print(
            "Server requests   :",
            len(
                server_requests
            ),
        )

        print(
            "Unknown messages  :",
            len(
                unknown
            ),
        )

        if unknown:
            raise AppServerProtocolError(
                "Unknown transport messages observed."
            )

        RESULT_FILE.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "version": "2.0.1",
                    "initialize": {
                        "userAgent":
                            initialize.get(
                                "userAgent"
                            ),
                        "platformFamily":
                            initialize.get(
                                "platformFamily"
                            ),
                        "platformOs":
                            initialize.get(
                                "platformOs"
                            ),
                    },
                    "concurrent_requests": 2,
                    "rate_limit_results": [
                        isinstance(
                            result_a,
                            dict,
                        ),
                        isinstance(
                            result_b,
                            dict,
                        ),
                    ],
                    "notification_methods":
                        notification_methods,
                    "server_request_methods":
                        server_request_methods,
                    "unknown_count":
                        len(
                            unknown
                        ),
                    "thread_start_called":
                        False,
                    "turn_start_called":
                        False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            "Thread start      : NOT CALLED"
        )

        print(
            "Turn start        : NOT CALLED"
        )

        print(
            "Dispatcher        : PASS"
        )

        return 0

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
