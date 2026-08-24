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
)

from session_adapter import (
    detect_repo,
)


CLI_VERSION = "2.0.11"


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

                try:
                    runtime.execute_prompt(
                        prompt=pasted,
                        cwd=cwd,
                        repo=repo,
                        db=db,
                    )
                except KeyboardInterrupt:
                    print("\n[cx] Tur durduruldu.")

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

        production_cx.print_quota(
            quota
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

        return (
            2
            if result.blocked
            else 0
        )

    except KeyboardInterrupt:
        print("\n[cx] Tur durduruldu.", file=sys.stderr)
        return 130

    except TimeoutError:
        print("[cx] Tur zaman aşımına uğradı.", file=sys.stderr)
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

            try:
                runtime.execute_prompt(
                    prompt=prompt,
                    cwd=cwd,
                    repo=repo,
                    db=db,
                )
            except KeyboardInterrupt:
                print("\n[cx] Tur durduruldu.")
            except TimeoutError:
                print("\n[cx] Tur zaman aşımına uğradı.")
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
                print(
                    f"\n[cx] Geçersiz parametre: {exc}"
                )
            except (CX2RuntimeError, RuntimeError) as exc:
                _safe_write_crash_log(exc)
                print(
                    f"\n[cx] Çalışma zamanı hatası: {exc}"
                )
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
