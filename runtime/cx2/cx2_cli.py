from __future__ import annotations

# CX2_HISTORY_CLI_BINDING_V1
from history_cli import handle_history_command

import argparse
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


from cx_home import resolve_cx_home

CX_HOME = resolve_cx_home()
CX2_HOME = CX_HOME / "runtime" / "cx2"
PRODUCTION_SRC = CX_HOME / "src"

for candidate in (
    str(CX2_HOME),
    str(PRODUCTION_SRC),
):
    if candidate not in sys.path:
        sys.path.insert(
            0,
            candidate,
        )


import cx as production_cx
from cx import (
    InvalidUnicodeInputError,
    normalize_external_text,
    ensure_valid_unicode,
)

# CX2_ATTACHMENT_CLI_V1
from input_adapter import CX2InputAction, build_cli_input_items

from prompt_transport import (
    PromptTransportError,
    capture_multiline_paste,
    resolve_prompt_source,
)

from budget_adapter import (
    read_live_quota,
)

import traceback

from client import (
    AppServerProtocolError,
)

from cx2_runtime import (
    CX2Runtime,
    CX2RuntimeError,
    EXPECTED_ROUTER_VERSION,
    RUNTIME_VERSION,
    TurnTimeoutError,
    _CX2_TERMINAL,
)

from terminal_pager import page_text
from terminal_markdown import render_markdown

from session_adapter import (
    detect_repo,
)


CLI_VERSION = "2.0.12"


class CX2CLIError(
    RuntimeError
):
    pass


def check_contract() -> None:

    if (
        production_cx.ROUTER_VERSION
        != EXPECTED_ROUTER_VERSION
    ):
        raise CX2CLIError(
            "Router/runtime version mismatch: "
            f"{production_cx.ROUTER_VERSION!r} != "
            f"{EXPECTED_ROUTER_VERSION!r}"
        )


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="cx",
        description=(
            "CX 2.0 direct Codex App Server runtime"
        ),
    )

    parser.add_argument(
        "prompt",
        nargs="*",
        help="Codex görevi",
    )

    parser.add_argument(
        "--prompt-file",
        metavar="PATH",
        help="UTF-8 dosya içeriğini birincil prompt olarak kullanır",
    )

    parser.add_argument(
        "--stdin",
        action="store_true",
        help="UTF-8 promptu stdin üzerinden okur",
    )

    parser.add_argument(
        "--doctor",
        action="store_true",
        help="CX runtime sağlık kontrolü",
    )

    parser.add_argument(
        "--route",
        metavar="TEXT",
        help=(
            "Sadece lokal routing sonucunu göster; "
            "model turn'ü başlatmaz"
        ),
    )

    parser.add_argument(
        "--route-file",
        metavar="PATH",
        help="Dosyadaki promptun routing sonucunu model turn olmadan gösterir",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Yerel token telemetri özetini göster",
    )

    parser.add_argument(
        "--quota",
        action="store_true",
        help=(
            "Canlı Codex kota durumunu göster; "
            "model turnü başlatmaz"
        ),
    )

    parser.add_argument(
        "--session",
        action="store_true",
        help="Aktif repo session bilgisini göster",
    )

    parser.add_argument(
        "--new",
        action="store_true",
        help="Persisted repo session bağlantısını sıfırla",
    )

    parser.add_argument(
        "--version",
        action="store_true",
        help="CX2 runtime sürümünü göster",
    )

    parser.add_argument(
        "--attach",
        action=CX2InputAction,
        input_kind="attach",
        default=[],
        metavar="PATH",
        help=(
            "Yerel dosya ekle; resimler native localImage, "
            "diğer dosyalar path mention olarak gönderilir"
        ),
    )

    parser.add_argument(
        "--image",
        action=CX2InputAction,
        input_kind="image",
        default=[],
        metavar="PATH",
        help="Yerel resmi native image input olarak ekle",
    )

    parser.add_argument(
        "--image-url",
        action=CX2InputAction,
        input_kind="image_url",
        default=[],
        metavar="URL",
        help="Uzak resmi URL image input olarak ekle",
    )

    parser.add_argument(
        "--file",
        action=CX2InputAction,
        input_kind="file",
        default=[],
        metavar="PATH",
        help="Yerel dosya/PDF/binary path mention ekle",
    )

    # Internal canary/test hook.
    # Normal cx usage never needs this.
    parser.add_argument(
        "--usage-db",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )

    return parser


def open_usage_db(
    override: str | None,
) -> sqlite3.Connection:

    if override:

        path = Path(
            override
        ).expanduser().resolve()

        if not path.exists():
            raise CX2CLIError(
                f"usage DB bulunamadı: {path}"
            )

        return sqlite3.connect(
            str(path)
        )

    return production_cx.init_db()


def print_local_route(
    prompt: str,
    cwd: Path,
) -> dict[str, Any]:

    policy = production_cx.load_policy()
    repo = detect_repo(
        cwd
    )

    route = production_cx.classify(
        prompt,
        repo,
        policy,
    )

    visible = (
        production_cx.cached_visible_models()
    )

    model = None

    if visible:

        model = (
            production_cx.choose_model(
                route["tier"],
                visible,
                policy,
            )
        )

    print(
        "=== CX LOCAL ROUTE ==="
    )

    print(
        f"CWD      : {cwd}"
    )

    print(
        f"Git      : {repo['git']}"
    )

    print(
        f"Root     : {repo['root']}"
    )

    print(
        "Stacks   :",
        (
            ", ".join(
                repo["stacks"]
            )
            if repo["stacks"]
            else "-"
        ),
    )

    print(
        f"Monorepo : {repo['monorepo']}"
    )

    print(
        f"Dirty    : {repo['dirty_files']}"
    )

    print()

    production_cx.print_route(
        route,
        model,
    )

    cce_enabled, cce_reason = (
        production_cx.should_use_cce(
            prompt,
            repo,
            route,
            policy,
        )
    )

    print(
        "CCE      : "
        + (
            "ON"
            if cce_enabled
            else "OFF"
        )
        + f" ({cce_reason})"
    )

    return {
        "repo":
            repo,

        "route":
            route,

        "model":
            model,

        "cce_enabled":
            cce_enabled,

        "cce_reason":
            cce_reason,
    }


def print_stats_db(
    db: sqlite3.Connection,
) -> None:

    row = db.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(input_tokens), 0),
            COALESCE(SUM(cached_input_tokens), 0),
            COALESCE(SUM(output_tokens), 0),
            COALESCE(SUM(reasoning_output_tokens), 0)
        FROM turns
        """
    ).fetchone()

    print(
        "=== CX STATS ==="
    )

    print(
        f"Turns            : {row[0]}"
    )

    print(
        f"Input tokens     : {row[1]}"
    )

    print(
        f"Cached input     : {row[2]}"
    )

    print(
        f"Output tokens    : {row[3]}"
    )

    print(
        f"Reasoning output : {row[4]}"
    )

    print()

    routes = db.execute(
        """
        SELECT route, COUNT(*)
        FROM turns
        GROUP BY route
        ORDER BY COUNT(*) DESC
        """
    ).fetchall()

    for route, count in routes:
        print(
            f"{route:10} : {count}"
        )

    print()

    try:

        escalation_count = db.execute(
            """
            SELECT COUNT(*)
            FROM escalation_events
            """
        ).fetchone()[0]

    except sqlite3.OperationalError:

        escalation_count = 0

    print(
        f"Escalations       : {escalation_count}"
    )


def print_session_db(
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> dict[str, Any] | None:

    session = (
        production_cx.load_repo_session(
            db,
            repo,
        )
    )

    print(
        "=== CX SESSION ==="
    )

    print(
        f"Repo     : {repo['root']}"
    )

    if not repo.get(
        "git"
    ):

        print(
            "Status   : disabled (not a git repo)"
        )

        return None

    if not session:

        print(
            "Status   : none"
        )

        return None

    policy = production_cx.load_policy()

    reusable, reason = (
        production_cx.session_reusable(
            session,
            repo,
            policy,
        )
    )

    print(
        "Status   :",
        (
            "reusable"
            if reusable
            else "stale"
        ),
    )

    print(
        f"Reason   : {reason}"
    )

    print(
        f"Thread   : {session['thread_id']}"
    )

    print(
        f"Branch   : {session.get('branch')}"
    )

    age = (
        production_cx.session_age_minutes(
            session
        )
    )

    print(
        "Age      :",
        (
            f"{age:.1f} min"
            if isinstance(
                age,
                (int, float),
            )
            else "?"
        ),
    )

    print(
        "Turns    :",
        session.get(
            "user_turns"
        ),
    )

    percent = session.get(
        "context_percent"
    )

    print(
        "Context  :",
        (
            f"{percent:.1f}%"
            if isinstance(
                percent,
                (int, float),
            )
            else "unknown"
        ),
    )

    return session


def print_interactive_help() -> None:

    print(f"=== CX {CLI_VERSION} KOMUTLARI ===")
    print()
    print("Temel")
    print("  /help                  Bu yardımı göster.")
    print("  /paste                 Çok satırlı prompt giriş modu (.send ile gönder, .cancel ile iptal et).")
    print("  /clear                 Terminal ekranını temizle (/cls).")
    print("  /exit                  CX interaktif moddan çık.")
    print()
    print("Oturum")
    print("  /new                   Persisted repo session/thread bağlantısını sıfırla.")
    print("  /session               Aktif repo session bilgisini göster.")
    print("  /quota                 Canlı Codex kota durumunu göster.")
    print("  /last [--page]         Bu çalışma alanındaki son görünür yanıtı göster.")
    print("  /transcript clear      Bu çalışma alanının görünür transcript'ini sil (onay ister).")
    print("  /trace                 Son turn'ün kısa araç izini göster.")
    print("  /stats                 Yerel token telemetri özetini göster.")
    print("  /route <görev>         Model turnü başlatmadan routing sonucunu göster.")
    print("  /doctor                CX runtime sağlık kontrolünü çalıştır.")
    print()
    print("Geçmiş")
    print("  /history [filtre]      Native Codex thread geçmişini listele.")
    print("  /search <metin>        Native thread geçmişi içinde ara.")
    print("  /thread [id|no]        Thread detaylarını göster (varsayılan: aktif).")
    print("  /turns [id|no]         Thread turn geçmişini göster (varsayılan: aktif).")
    print("  /resume <id|no>        Aynı repo thread'ini aktif session yap.")
    print("  /rename <id|no> <yeni-ad> Native thread adını değiştir.")
    print("  /archive <id|no>       Thread'i arşivle.")
    print("  /unarchive <id|no>     Arşivlenmiş thread'i geri getir.")
    print("  /delete <id|no>        Kalıcı siler, onay ister.")
    print()
    print("İpucu: /history ve /search sonuçlarındaki [1], [2] gibi numaralar ([1], [2], [3])")
    print("thread ID yerine kullanılabilir (örn: /resume 1, /thread 2).")


def handle_interactive_command(
    value: str,
    *,
    runtime: CX2Runtime | None,
    db: sqlite3.Connection,
    cwd: Path,
    repo: dict[str, Any],
) -> tuple[bool, bool]:
    """
    Returns:
        handled, should_exit
    """

    text = value.strip()
    folded = text.casefold()

    if folded in {
        "/exit",
        "/quit",
        "exit",
        "quit",
    }:

        return True, True

    if folded in {
        "/help",
        "--help",
        "/?",
    }:

        print_interactive_help()

        return True, False

    if folded in {
        "/paste",
        "--paste",
        "paste",
    }:

        pasted = capture_multiline_paste()

        if pasted:

            if runtime is not None:
                execute_interactive_prompt(
                    runtime,
                    prompt=pasted,
                    cwd=cwd,
                    repo=repo,
                    db=db,
                )

        return True, False

    if folded in {
        "/doctor",
        "--doctor",
    }:

        production_cx.doctor()

        return True, False

    if folded in {
        "/stats",
        "--stats",
    }:

        print_stats_db(
            db
        )

        return True, False

    if folded in {
        "/session",
        "--session",
    }:

        print_session_db(
            db,
            repo,
        )

        return True, False

    if folded in {
        "/quota",
        "--quota",
    }:

        if runtime is None:

            with CX2Runtime(
                live=False
            ) as quota_runtime:

                quota = read_live_quota(
                    quota_runtime.client
                )

        else:

            runtime.start()

            quota = read_live_quota(
                runtime.client
            )

        print(
            "=== CX QUOTA ==="
        )

        _CX2_TERMINAL.set_status_snapshot(quota=quota)

        production_cx.print_quota(
            quota
        )

        return True, False

    if folded in {"/last", "/last --page", "/last page"}:
        if runtime is None:
            print("[cx] /last yalnızca interaktif runtime içinde kullanılabilir.")
            return True, False
        response = runtime.last_visible_response(cwd=cwd, repo=repo, db=db)
        if response is None:
            print("[cx] Bu çalışma alanı için görünür transcript bulunamadı.")
            return True, False
        header = (
            f"[cx] last · {response.state} · {response.line_count} lines · "
            f"{response.retained_bytes}/{response.total_bytes} bytes"
        )
        body = render_markdown(response.text, color=bool(getattr(sys.stdout, "isatty", lambda: False)()))
        if response.truncated:
            body += "\n[cx] Uyarı: transcript 16 MiB sınırında kesildi."
        rendered = header + "\n\n" + body
        if folded != "/last":
            page_text(rendered)
        else:
            print(rendered)
        return True, False

    if folded in {"/transcript clear", "/transcript-clear"}:
        if runtime is None:
            print("[cx] Transcript runtime içinde kullanılabilir.")
            return True, False
        if not (getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)()):
            print(
                "[cx] Transcript temizleme yalnızca etkileşimli terminalde onaylanabilir.",
                file=sys.stderr,
            )
            return True, False
        try:
            answer = input(
                f"Görünür transcript yalnızca bu çalışma alanı için ({cwd}) silinsin mi? [y/N] "
            ).strip().casefold()
        except (EOFError, KeyboardInterrupt):
            print("\n[cx] İptal edildi.")
            return True, False
        if answer not in {"y", "yes", "e", "evet"}:
            print("[cx] İptal edildi; transcript korunuyor.")
            return True, False
        count = runtime.clear_visible_transcript(cwd=cwd)
        print(f"[cx] {count} transcript kaydı silindi.")
        return True, False

    if folded in {"/trace", "/trace last"}:
        trace = getattr(runtime, "last_trace", None) if runtime is not None else None
        if not trace:
            print("[cx] Bu runtime için araç izi yok.")
        else:
            dropped_entries = int(getattr(runtime, "last_trace_dropped_entries", 0) or 0)
            if dropped_entries:
                print(f"[cx] trace bounded to 64 commands; dropped={dropped_entries} older entries")
            for index, entry in enumerate(trace, start=1):
                print(
                    f"{index}. {entry.get('command', '<command>')} · "
                    f"{entry.get('status', 'unknown')} · exit={entry.get('exit_code', '?')} · "
                    f"{entry.get('duration_ms', '?')}ms · cwd={entry.get('cwd', '?')}"
                )
                print(
                    f"   classification={entry.get('classification', '')} · "
                    f"host_execution={bool(entry.get('host_execution'))}"
                )
                if entry.get("output_snippet"):
                    print(f"   output={entry['output_snippet']}")
                for field in ("command", "cwd", "status", "classification"):
                    dropped = int(entry.get(f"{field}_dropped_bytes", 0) or 0)
                    if dropped:
                        print(f"   {field} truncated; dropped={dropped} bytes")
                if entry.get("output_truncated") or entry.get("output_dropped_bytes"):
                    print(
                        f"   output truncated; retained={len(str(entry.get('output_snippet', '')).encode('utf-8'))}/"
                        f"{entry.get('output_total_bytes', 0)} bytes; dropped={entry.get('output_dropped_bytes', 0)} bytes"
                    )
        return True, False

    if folded in {
        "/new",
        "--new",
    }:

        if runtime is not None:
            runtime.reset_memory_session()

        production_cx.clear_repo_session(
            db,
            repo,
        )

        print(
            "[cx] Aktif oturum bağlantısı sıfırlandı. "
            "Yeni thread bir sonraki promptta başlayacak."
        )

        return True, False


    if folded in {
        "/clear",
        "/cls",
    }:

        os.system(
            "cls"
            if os.name == "nt"
            else "clear"
        )

        return True, False

    for prefix in (
        "/route",
        "--route",
    ):

        if folded == prefix:

            print(
                f"Kullanım: {prefix} <görev>"
            )

            return True, False

        if folded.startswith(
            prefix + " "
        ):

            route_prompt = (
                text[
                    len(prefix):
                ]
                .strip()
            )

            print_local_route(
                route_prompt,
                cwd,
            )

            print()
            print(
                "[cx] /route sadece ÖNİZLEME yapar; "
                "model turnü başlatmaz."
            )

            return True, False

    if handle_history_command(
        text,
        runtime=runtime,
        db=db,
        cwd=cwd,
        repo=repo,
    ):

        return True, False

    # Never silently forward command-like input to Codex.
    if (
        text.startswith("/")
        or text.startswith("--")
    ):

        print(
            f"[cx] Bilinmeyen komut: {text}"
        )

        print(
            "[cx] Komutlar için /help"
        )

        return True, False

    return False, False


def _write_crash_log(
    exc: BaseException | None = None,
) -> None:
    crash_path = (
        CX2_HOME
        / "cx2-cli-last-crash.txt"
    )
    try:
        crash_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        if exc is not None:
            tb_text = "".join(
                traceback.format_exception(
                    type(exc),
                    exc,
                    exc.__traceback__,
                )
            )
        else:
            tb_text = traceback.format_exc()
        crash_path.write_text(
            tb_text,
            encoding="utf-8",
        )
    except Exception:
        pass


def _safe_write_crash_log(
    exc: BaseException | None = None,
) -> None:
    try:
        _write_crash_log(exc)
    except Exception:
        pass


def _timeout_message(exc: TimeoutError) -> str:
    if isinstance(exc, TurnTimeoutError):
        if exc.kind == "idle":
            return (
                "[cx] Tur ilerleme olmadığı için zaman aşımına uğradı "
                f"(idle: {exc.configured_idle_timeout:g}s)."
            )
        return (
            "[cx] Tur maksimum çalışma süresine ulaştı "
            f"(hard: {exc.configured_hard_timeout:g}s)."
        )
    return "[cx] Tur zaman aşımına uğradı."


def execute_interactive_prompt(
    runtime: CX2Runtime,
    *,
    prompt: str,
    cwd: Path,
    repo: dict[str, Any],
    db: sqlite3.Connection,
) -> None:
    """Shared failure boundary for ordinary and /paste interactive turns."""
    try:
        runtime.execute_prompt(
            prompt=prompt,
            cwd=cwd,
            repo=repo,
            db=db,
        )
    except KeyboardInterrupt:
        print("\n[cx] Tur durduruldu.")
    except TimeoutError as exc:
        print("\n" + _timeout_message(exc))
        try:
            runtime.reset_memory_session()
        except Exception:
            pass
    except AppServerProtocolError as exc:
        _safe_write_crash_log(exc)
        print(
            "\n[cx] Codex App Server bağlantısı koptu; "
            "sonraki istekte yeniden başlatılacak."
        )
        try:
            runtime.close()
        except Exception:
            pass
    except ValueError as exc:
        print(f"\n[cx] Geçersiz parametre: {exc}")
    except (CX2RuntimeError, RuntimeError) as exc:
        _safe_write_crash_log(exc)
        print(f"\n[cx] Çalışma zamanı hatası: {exc}")
        try:
            runtime.close()
        except Exception:
            pass
    except Exception as exc:
        _safe_write_crash_log(exc)
        print(
            f"\n[cx] Beklenmeyen hata oluştu: {exc}. "
            "Ayrıntılar hata günlüğüne yazıldı."
        )
        try:
            runtime.close()
        except Exception:
            pass


def execute_one_shot(
    prompt: str,
    *,
    cwd: Path,
    repo: dict[str, Any],
    db: sqlite3.Connection,
    input_items: list[dict] | None = None,
) -> int:

    try:
        prompt = normalize_external_text(
            prompt
        )
    except InvalidUnicodeInputError:
        print(
            "[cx] Girdi kodlaması geçersiz; metin kayıpsız çözülemedi.",
            file=sys.stderr,
        )
        return 1

    runtime = CX2Runtime(
        live=True
    )

    try:

        result = runtime.execute_prompt(
            prompt=prompt,
            cwd=cwd,
            repo=repo,
            db=db,
            input_items=input_items,
        )

        outcome = result.outcome
        if outcome == "COMPLETED":
            return 0
        if outcome == "BLOCKED":
            return 2
        if outcome == "INTERRUPTED":
            return 130
        return 1

    except KeyboardInterrupt:
        print("\n[cx] Tur durduruldu.", file=sys.stderr)
        return 130

    except TimeoutError as exc:
        print(_timeout_message(exc), file=sys.stderr)
        return 1

    except AppServerProtocolError as exc:
        _safe_write_crash_log(exc)
        print(f"[cx] Codex App Server bağlantı hatası: {exc}", file=sys.stderr)
        return 1

    except ValueError as exc:
        print(f"[cx] Geçersiz parametre: {exc}", file=sys.stderr)
        return 1

    except (CX2RuntimeError, RuntimeError) as exc:
        _safe_write_crash_log(exc)
        print(f"[cx] Çalışma zamanı hatası: {exc}", file=sys.stderr)
        return 1

    except Exception as exc:
        _safe_write_crash_log(exc)
        print(f"[cx] Beklenmeyen hata: {exc}", file=sys.stderr)
        return 1

    finally:

        try:
            runtime.close()
        except Exception:
            pass


def interactive_loop(
    *,
    cwd: Path,
    repo: dict[str, Any],
    db: sqlite3.Connection,
) -> int:

    runtime = CX2Runtime(
        live=True,
        interactive=True,
    )

    print(
        f"=== CX {CLI_VERSION} ==="
    )

    is_project = repo.get("git") or bool(repo.get("stacks"))

    if is_project:
        print(
            f"Repo : {repo['root']}"
        )
        print(
            "Stack:",
            (
                ", ".join(
                    repo["stacks"]
                )
                if repo["stacks"]
                else "-"
            ),
        )
    else:
        print(
            f"Konum : {repo.get('cwd', repo['root'])}"
        )
        print(
            "Proje : Algılanmadı"
        )
        print(
            "Stack : -"
        )

    print(
        "Komutlar: /help"
    )

    print(
        "Çıkmak için: /exit"
    )

    print()

    try:

        while True:

            try:
                raw_input_text = input(
                    "cx> "
                ).strip()
                prompt = normalize_external_text(
                    raw_input_text
                )
            except InvalidUnicodeInputError:
                print(
                    "[cx] Girdi kodlaması geçersiz; metin kayıpsız çözülemedi."
                )
                continue
            except (
                EOFError,
                KeyboardInterrupt,
            ):

                print()

                break

            if not prompt:
                continue

            handled, should_exit = (
                handle_interactive_command(
                    prompt,
                    runtime=runtime,
                    db=db,
                    cwd=cwd,
                    repo=repo,
                )
            )

            if should_exit:
                break

            if handled:
                continue

            execute_interactive_prompt(
                runtime,
                prompt=prompt,
                cwd=cwd,
                repo=repo,
                db=db,
            )

    finally:

        try:
            runtime.close()
        except Exception:
            pass

    return 0


def main(
    argv: list[str] | None = None,
) -> int:

    check_contract()

    parser = build_parser()

    args = parser.parse_args(
        argv
    )

    cwd = Path.cwd().resolve()

    repo = detect_repo(
        cwd
    )

    if args.version:

        print(
            f"CX2 CLI {CLI_VERSION}"
        )

        print(
            f"CX2 runtime {RUNTIME_VERSION}"
        )

        print(
            f"Router {EXPECTED_ROUTER_VERSION}"
        )

        return 0

    if args.doctor:

        return production_cx.doctor()

    try:
        resolved_source = resolve_prompt_source(
            args,
            cwd,
        )
    except PromptTransportError as exc:
        print(
            f"[cx] prompt error: {exc}",
            file=sys.stderr,
        )
        return 1

    if resolved_source.is_route_only:
        assert resolved_source.prompt is not None
        print_local_route(
            resolved_source.prompt,
            cwd,
        )
        return 0

    one_shot_prompt = resolved_source.prompt

    db = None

    try:

        if (
            args.stats
            or args.session
            or args.new
            or one_shot_prompt
            or (
                not args.quota
                and not args.doctor
            )
        ):

            db = open_usage_db(
                args.usage_db
            )

        if args.stats:

            assert db is not None

            print_stats_db(
                db
            )

            return 0

        if args.session:

            assert db is not None

            print_session_db(
                db,
                repo,
            )

            return 0

        if args.new:

            assert db is not None

            production_cx.clear_repo_session(
                db,
                repo,
            )

            print(
                "[cx] Persisted session cleared."
            )

            return 0

        if args.quota:

            with CX2Runtime(
                live=False
            ) as runtime:

                quota = read_live_quota(
                    runtime.client
                )

            print(
                "=== CX QUOTA ==="
            )

            production_cx.print_quota(
                quota
            )

            return 0

        if one_shot_prompt:

            assert db is not None

            return execute_one_shot(
                one_shot_prompt,
                cwd=cwd,
                repo=repo,
                db=db,
                input_items=build_cli_input_items(args, cwd),
            )

        assert db is not None

        return interactive_loop(
            cwd=cwd,
            repo=repo,
            db=db,
        )

    finally:

        if db is not None:
            db.close()


# CX2_CRASH_LOG_V1
def _run_cli_entrypoint() -> int:
    """
    Persist an exact traceback before propagating a fatal CLI error.

    This does not suppress failures and does not alter model/runtime
    semantics. It exists only so PowerShell/native stderr handling
    cannot destroy the diagnostic traceback.
    """
    crash_path = (
        CX2_HOME
        / "cx2-cli-last-crash.txt"
    )

    try:
        if crash_path.exists():
            crash_path.unlink()
    except OSError:
        pass

    try:
        return main()

    except BaseException as exc:
        _write_crash_log(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(
        _run_cli_entrypoint()
    )
