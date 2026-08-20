from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any


CX_HOME = Path.home() / ".cx"
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

from client import (
    AppServerClient,
    CODEX_EXE,
)

from codex_compat import (
    assess_codex_compatibility,
    CompatibilityState,
)

from budget_adapter import (
    build_execution_plan,
    read_live_quota,
)

from session_adapter import (
    acquire_thread,
    canonical_cwd_key,
    save_session,
)

from telemetry_adapter import (
    context_info_from_turn_result,
)

from turn_runner import (
    StreamingTurnRunner,
    TurnRunResult,
    _CX2_TERMINAL,
)

from verification_gate import (
    CommandExecutionSummary,
    VerificationAssessment,
    assess_turn,
    is_explicit_verification_skip,
)


EXPECTED_ROUTER_VERSION = "1.2.0"
RUNTIME_VERSION = "2.0.6"


class CX2RuntimeError(
    RuntimeError
):
    pass


class ProductionStatusView:
    """
    SDK-compatible status facade.

    Production CX currently reads:
        status.value
    or:
        str(status)

    Keep both semantics.
    """

    def __init__(
        self,
        value: str,
    ) -> None:

        self.value = str(
            value
        )

    def __str__(
        self,
    ) -> str:

        return self.value


class ProductionResultView:
    """
    Adapt StreamingTurnRunner result to the subset of the
    existing production SDK result contract consumed by:

        record_turn()
        escalation_reason()
        usage_context_info()
    """

    def __init__(
        self,
        turn_result: Any,
    ) -> None:

        self.raw = turn_result

        self.final_response = str(
            getattr(
                turn_result,
                "agent_text",
                "",
            )
            or ""
        )

        usage = getattr(
            turn_result,
            "token_usage",
            None,
        )

        self.usage = (
            usage
            if isinstance(
                usage,
                dict,
            )
            else {}
        )

        self.status = (
            ProductionStatusView(
                str(
                    getattr(
                        turn_result,
                        "status",
                        "",
                    )
                )
            )
        )

        duration = getattr(
            turn_result,
            "duration_ms",
            None,
        )

        self.duration_ms = (
            int(
                duration
            )
            if isinstance(
                duration,
                (int, float),
            )
            else None
        )


@dataclass
class CX2ExecutionResult:

    blocked: bool

    thread_id: str | None

    session_mode: str | None

    plan: dict[str, Any]

    quota: dict[str, Any]

    final_result: ProductionResultView | None

    raw_turn_result: Any | None

    attempts_used: int

    escalations: int

    verification_summary: dict[str, Any] | None = None


def _check_contract() -> None:

    version = getattr(
        production_cx,
        "ROUTER_VERSION",
        None,
    )

    if version != EXPECTED_ROUTER_VERSION:

        raise CX2RuntimeError(
            "Production router version mismatch: "
            f"{version!r}"
        )


def initialize_params() -> dict[str, Any]:

    return {
        "clientInfo": {
            "name":
                "cx2",

            "title":
                "CX 2.0",

            "version":
                RUNTIME_VERSION,
        },

        "capabilities": {
            "experimentalApi":
                True,

            "requestAttestation":
                False,

            "optOutNotificationMethods":
                None,
        },
    }


# CX2_ATTACHMENT_PROPAGATION_V1
# CX2_ROUTED_APPROVAL_POLICY_V1
def approval_policy_for_route(
    route: dict[str, Any],
) -> str:
    """
    Conservative turn-level approval routing.

    read-only + non-mutating -> never
    every other execution profile -> on-request
    """

    sandbox = str(
        route.get(
            "sandbox",
            "",
        )
    )

    mutating = bool(
        route.get(
            "mutating",
            False,
        )
    )

    if (
        sandbox == "read-only"
        and not mutating
    ):
        return "never"

    return "on-request"


# CX2_AUTO_WEB_ROUTER_V1

# User explicit negation patterns (user asking NOT to use web)
_NEGATION_PATTERNS = [
    r"\bweb\s+aramas[ıi]\s+yapma\b",
    r"\binternete\s+bakma\b",
    r"\binternetten\s+ara[sş]t[ıi]rma\b",
    r"\bweb\s+kullanma\b",
    r"\binternete\s+eri[sş]meden\s+cevapla\b",
    r"\bg[uü]ncel\s+bilgi\s+arama\b",
    r"\barama\s+yapma\b",
    r"\binternete\s+ba[gğ]lanma\b",
    r"\bdo\s+not\s+search\s+(?:the\s+)?web\b",
    r"\bdon'?t\s+search\s+(?:the\s+)?(?:web|online|internet)\b",
    r"\bdon'?t\s+browse\b",
    r"\bdo\s+not\s+browse\b",
    r"\bdo\s+not\s+use\s+(?:the\s+)?internet\b",
    r"\bwithout\s+(?:web\s+search|browsing|internet)\b",
    r"\banswer\s+without\s+browsing\b",
    r"\bdo\s+not\s+look\s+up\b",
]
_NEGATION_RE = re.compile("|".join(_NEGATION_PATTERNS), re.IGNORECASE)

# Coding false-positive protection patterns: queries targeting code, features, files, variables, or bugs
_CODE_CONTEXT_RE = re.compile(
    r"\b(?:current\s+branch|currentUser|binary\s+search|search\s+component|search\s+input|search\s+algorithm|weather\s+API|weather\s+service|price\s+variable|price\s+de[gğ]i[sş]ken|latestVersion|latest_diff|d[uü]n\s+yazd[ıi][gğ][ıi]m|bulundu[gğ]un\s+klas[oö]r|bulundu[gğ]un\s+dizin|i[çc]inde\s+bulundu[gğ]un|hatas[ıi]n[ıi]\s+d[uü]zelt|hatay[ıi]\s+d[uü]zelt|bug['ıi]?\s+d[uü]zelt|se[çc]ene[gğ]ini\s+kald[ıi]r|flag['ıi]ni\s+kald[ıi]r|flag['ıi]ni\s+sil)\b",
    re.IGNORECASE,
)

# Explicit Turkish search triggers
_EXPLICIT_TR_PATTERNS = [
    r"@web\b",
    r"\binternette\s+ara[sş]t[ıi]r",
    r"\binternetten\s+ara[sş]t[ıi]r",
    r"\bwebde\s+ara[sş]t[ıi]r",
    r"\bwebden\s+ara[sş]t[ıi]r",
    r"\bweb'?de\s+ara[sş]t[ıi]r",
    r"\bweb'?den\s+ara[sş]t[ıi]r",
    r"\binternete\s+bak\b",
    r"\binternetten\s+bak\b",
    r"\bwebden\s+bak\b",
    r"\binternetten\s+bul\b",
    r"\binternetten\s+kontrol\s+et\b",
    r"\bwebden\s+kontrol\s+et\b",
    r"\bweb\s+aramas[ıi]\b",
    r"\bara[sş]t[ıi]r[ıi]p\s+s[oö]yle\b",
    r"\bara[sş]t[ıi]rsana\b",
    r"\bara[sş]t[ıi]rabilir\s+misin\b",
    r"\bara[sş]t[ıi]r[ıi]r\s+m[ıi]s[ıi]n\b",
    r"\bara[sş]t[ıi]rma\s+yap\b",
    r"\bara[sş]t[ıi]r\b",
    r"\bgoogle'?da\s+ara\b",
    r"\bgoogle'?dan\s+ara\b",
    r"\bgoogle'?dan\s+bak\b",
]
_EXPLICIT_TR_RE = re.compile("|".join(_EXPLICIT_TR_PATTERNS), re.IGNORECASE)

# Explicit English search triggers
_EXPLICIT_EN_PATTERNS = [
    r"\bsearch\s+the\s+web\b",
    r"\bsearch\s+online\b",
    r"\bbrowse\s+the\s+web\b",
    r"\bbrowse\s+online\b",
    r"\blook\s+it\s+up\b",
    r"\blook\s+up\s+online\b",
    r"\bfind\s+online\b",
    r"\bcheck\s+online\b",
    r"\bcheck\s+the\s+internet\b",
    r"\bweb\s+search\b",
]
_EXPLICIT_EN_RE = re.compile("|".join(_EXPLICIT_EN_PATTERNS), re.IGNORECASE)

# Sports freshness triggers
_SPORTS_TEAMS = r"(?:fenerbah[çc]e|fener|galatasaray|gs|be[sş]ikta[sş]|bjk|trabzonspor|ts|lyon|real\s+madrid|barcelona|arsenal|liverpool|milan|inter|bayern|manchester)"
_SPORTS_KEYWORDS = r"(?:ma[çc]|ma[çc][ıi]|ma[çc][ıi]n|skor|skoru|skorunu|ka[çc]\s+ka[çc]|sonu[çc]|sonucu|sonucunu|fikst[uü]r|puan\s+durumu|score|match|game\s+result|standings)"
_SPORTS_TIME = r"(?:d[uü]n|d[uü]nk[uü]|d[uü]n\s+k[uü]|bug[uü]n|bug[uü]nk[uü]|son|en\s+son|canl[ıi]|latest|current|yesterday|today)"

_SPORTS_RE = re.compile(
    rf"(?:{_SPORTS_TEAMS}.*?{_SPORTS_KEYWORDS}|{_SPORTS_KEYWORDS}.*?{_SPORTS_TEAMS}|{_SPORTS_TIME}.*?{_SPORTS_KEYWORDS}|{_SPORTS_KEYWORDS}.*?{_SPORTS_TIME}|ka[çc]\s+ka[çc]\s+bitti|current\s+score|latest\s+score)",
    re.IGNORECASE,
)

# Weather freshness
_WEATHER_RE = re.compile(
    r"\b(?:hava\s+nas[ıi]l|hava\s+durumu|ya[gğ]mur\s+ya[gğ]acak\s+m[ıi]|kar\s+ya[gğ]acak\s+m[ıi]|s[ıi]cakl[ıi]k\s+ka[çc]|today'?s\s+weather|tomorrow'?s\s+weather|weather\s+forecast)\b",
    re.IGNORECASE,
)

# News freshness
_NEWS_RE = re.compile(
    r"\b(?:son\s+dakika|bug[uü]nk[uü]\s+haberler|g[uü]n[uü]n\s+haberleri|g[uü]ncel\s+haberler|latest\s+news|recent\s+news|breaking\s+news)\b",
    re.IGNORECASE,
)

# Price / Market / Currency freshness
_PRICE_RE = re.compile(
    r"\b(?:dolar\s+(?:[sş]u\s+an|ka[çc]|bug[uü]n)|euro\s+(?:[sş]u\s+an|ka[çc]|bug[uü]n)|alt[ıi]n\s+fiyat[ıi]|bitcoin\s+[sş]u\s+an|btc\s+[sş]u\s+an|[sş]u\s+anda\s+ka[çc]\s+tl|enflasyon\s+oran[ıi]|current\s+price|exchange\s+rate|stock\s+price)\b",
    re.IGNORECASE,
)

# Software latest version / external release freshness
_SOFTWARE_VERSION_RE = re.compile(
    r"\b(?:g[uü]ncel\s+[A-Za-z0-9\._\-]+\s+s[uü]r[uü]m[uü]|en\s+son\s+[A-Za-z0-9\._\-]+\s+s[uü]r[uü]m[uü]|latest\s+[A-Za-z0-9\._\-]+\s+version|latest\s+[A-Za-z0-9\._\-]+\s+release|current\s+version\s+of\s+[A-Za-z0-9\._\-]+)\b",
    re.IGNORECASE,
)

# Current roles (e.g. CEO, president)
_ROLES_RE = re.compile(
    r"\b(?:[sş]irketinin\s+ceo['uü]?su\s+kim|[sş]u\s+an\s+cumhurba[sş]kan[ıi]\s+kim|current\s+ceo\s+of|current\s+president\s+of)\b",
    re.IGNORECASE,
)


def web_search_mode_for_prompt(
    prompt: str,
) -> str:
    """
    Deterministically classify whether the prompt requires native live web search.
    Keep native web disabled for ordinary local coding work.
    Enable LIVE search when explicit web search, HTTP(S) URL, or fresh external
    time-sensitive/sports/market/weather/version information is requested.
    """
    raw = str(prompt or "").strip()
    if not raw:
        return "disabled"

    text = raw.replace("İ", "i").replace("I", "ı").lower()

    if "https://" in text or "http://" in text:
        return "live"

    if _CODE_CONTEXT_RE.search(text):
        return "disabled"

    if _NEGATION_RE.search(text):
        return "disabled"

    if _EXPLICIT_TR_RE.search(text) or _EXPLICIT_EN_RE.search(text):
        return "live"

    if (
        _SPORTS_RE.search(text)
        or _WEATHER_RE.search(text)
        or _NEWS_RE.search(text)
        or _PRICE_RE.search(text)
        or _SOFTWARE_VERSION_RE.search(text)
        or _ROLES_RE.search(text)
    ):
        return "live"

    return "disabled"


classify_web_requirement = web_search_mode_for_prompt



class CX2Runtime:
    """
    Long-lived direct Codex App Server runtime.

    Intended lifecycle:

        runtime = CX2Runtime()
        runtime.start()

        runtime.execute_prompt(...)
        runtime.execute_prompt(...)
        ...

        runtime.close()

    One-shot mode may simply use it as a context manager.

    This class owns transport + orchestration only.
    Routing/session/quota/telemetry policy remains sourced from
    the existing production CX modules.
    """

    def __init__(
        self,
        *,
        live: bool = True,
        interactive: bool = False,
    ) -> None:

        _check_contract()

        self.live = bool(
            live
        )

        self.interactive = bool(
            interactive
        )

        self.client = AppServerClient(
            CODEX_EXE
        )

        self.started = False
        self.initialized = False

        self.active_non_git_thread_id: str | None = None
        self.active_non_git_cwd_key: str | None = None
        self.active_non_git_turns: int = 0


    def reset_memory_session(
        self,
    ) -> None:

        self.active_non_git_thread_id = None
        self.active_non_git_cwd_key = None
        self.active_non_git_turns = 0


    def __enter__(
        self,
    ) -> "CX2Runtime":

        self.start()

        return self


    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:

        self.close()


    # ---------------------------------------------------------
    # Transport lifecycle
    # ---------------------------------------------------------

    def start(
        self,
    ) -> None:

        proc = getattr(self.client, "process", None)
        if isinstance(proc, subprocess.Popen) and proc.poll() is not None:
            self.close()
        elif hasattr(proc, "poll") and isinstance(proc.poll(), int):
            self.close()

        if self.initialized:
            return

        if not self.started:

            compat = assess_codex_compatibility(self.client.codex_exe)
            if compat.is_fatal or compat.core_state == CompatibilityState.INCOMPATIBLE:
                err_msg = "; ".join(compat.issues) if compat.issues else "incompatible core contract"
                raise CX2RuntimeError(
                    f"Codex compatibility check failed: {err_msg}"
                )

            self.client.start()

            self.started = True

        response = self.client.request(
            "initialize",
            initialize_params(),
            timeout=15.0,
        )

        if not isinstance(
            response,
            dict,
        ):

            self.close()

            raise CX2RuntimeError(
                "App Server initialize response invalid."
            )

        self.client.notify(
            "initialized",
            {},
        )

        self.initialized = True


    def close(
        self,
    ) -> None:

        try:

            self.client.close()

        finally:

            self.started = False
            self.initialized = False
            self.reset_memory_session()



    # ---------------------------------------------------------
    # Planning
    # ---------------------------------------------------------

    def build_plan(
        self,
        prompt: str,
        cwd: Path,
        *,
        quota: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Pure routing/budget operation.

        Does NOT start App Server and does NOT run a model turn
        when called directly with an existing quota snapshot.
        """

        _check_contract()

        return build_execution_plan(
            prompt,
            cwd.resolve(),
            quota,
        )


    def live_plan(
        self,
        prompt: str,
        cwd: Path,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
    ]:
        """
        App Server quota read + deterministic execution plan.
        No thread/start or turn/start.
        """

        self.start()

        quota = read_live_quota(
            self.client
        )

        plan = self.build_plan(
            prompt,
            cwd,
            quota=quota,
        )

        return quota, plan


    # ---------------------------------------------------------
    # Full prompt execution
    # ---------------------------------------------------------

    def execute_prompt(
        self,
        *,
        prompt: str,
        cwd: Path,
        repo: dict[str, Any],
        db: Any,
        input_items: list[dict] | None = None,
        quota_override: dict[str, Any] | None = None,
    ) -> CX2ExecutionResult:

        # CX2_AUTO_WEB_TURN_V1
        web_search_mode = (
            web_search_mode_for_prompt(
                prompt
            )
        )

        _check_contract()

        cwd = cwd.resolve()

        policy = production_cx.load_policy()

        cce_enabled = bool(
            policy.get(
                "cce",
                {},
            ).get(
                "enabled",
                False,
            )
        )

        if cce_enabled:

            raise CX2RuntimeError(
                "Direct CX2 runtime requires CCE policy OFF. "
                "CCE managed path is not migrated yet."
            )

        self.start()

        quota = (
            quota_override
            if isinstance(
                quota_override,
                dict,
            )
            else read_live_quota(
                self.client
            )
        )

        plan = self.build_plan(
            prompt,
            cwd,
            quota=quota,
        )

        if plan.get(
            "blocked"
        ):

            print()
            print(
                "[cx] BLOCKED: "
                "Codex quota/spend limit reached. "
                "No model turn was started."
            )
            print()

            return CX2ExecutionResult(
                blocked=True,
                thread_id=None,
                session_mode=None,
                plan=plan,
                quota=quota,
                final_result=None,
                raw_turn_result=None,
                attempts_used=0,
                escalations=0,
            )

        attempts = plan.get(
            "attempts"
        )

        if (
            not isinstance(
                attempts,
                list,
            )
            or not attempts
        ):

            raise CX2RuntimeError(
                "Execution plan has no attempts."
            )

        base_route = dict(
            plan[
                "route"
            ]
        )

        approval_policy = (
            approval_policy_for_route(
                base_route
            )
        )

        current_cwd_key = canonical_cwd_key(
            cwd
        )

        if repo.get("git"):
            self.reset_memory_session()
            candidate_memory_thread = None
        else:
            if (
                self.active_non_git_cwd_key == current_cwd_key
                and self.active_non_git_thread_id
            ):
                candidate_memory_thread = (
                    self.active_non_git_thread_id
                )
            else:
                self.reset_memory_session()
                candidate_memory_thread = None

        is_reusable = self.interactive or bool(
            repo.get("git")
        )

        first_attempt = attempts[0]

        acquired = acquire_thread(
            self.client,
            db,
            repo,
            root=cwd,
            model=str(
                first_attempt[
                    "model"
                ]
            ),
            permissions=str(
                first_attempt[
                    "permissions"
                ]
            ),
            web_search_mode=
                web_search_mode,
            active_memory_thread_id=
                candidate_memory_thread,
            reusable=
                is_reusable,
        )

        thread_id = str(
            acquired[
                "thread_id"
            ]
        )

        session_mode = str(
            acquired[
                "mode"
            ]
        )

        resume_error = acquired.get(
            "resume_error"
        )

        if resume_error:
            print(
                "[cx] session=NEW "
                "(stored thread unavailable)"
            )

        runner = StreamingTurnRunner(
            self.client,
            live=self.live,
        )

        attempt_input = prompt

        previous_tier = None
        previous_model = None
        previous_reason = None

        final_result = None
        final_raw = None

        attempts_used = 0
        escalations = 0

        for attempt_index, attempt in enumerate(
            attempts,
            start=1,
        ):

            tier = str(
                attempt[
                    "tier"
                ]
            )

            model = str(
                attempt[
                    "model"
                ]
            )

            effort = str(
                attempt[
                    "reasoning"
                ]
            )

            permissions = str(
                attempt[
                    "permissions"
                ]
            )

            attempt_route = dict(
                base_route
            )

            attempt_route[
                "tier"
            ] = tier

            attempt_route[
                "reasoning"
            ] = effort

            if (
                previous_tier is not None
                and previous_model is not None
                and previous_reason is not None
            ):

                production_cx.record_escalation(
                    db,
                    thread_id=thread_id,
                    reason=previous_reason,
                    from_tier=previous_tier,
                    to_tier=tier,
                    from_model=previous_model,
                    to_model=model,
                )

                escalations += 1

                print(
                    f"[cx] ESCALATE "
                    f"{previous_model} -> {model} "
                    f"({previous_reason})"
                )

            if self.live:
                _CX2_TERMINAL.render_turn_header(
                    session_mode=session_mode,
                    model=model,
                    effort=effort,
                    sandbox=str(
                        attempt_route.get(
                            "sandbox"
                        )
                        or "full"
                    ),
                    quota=quota,
                )

            # Transport/auth/server errors are allowed to raise.
            # They are NOT evidence that a stronger model is needed.
            raw_result = runner.run_turn(
                thread_id=thread_id,
                prompt=attempt_input,
                cwd=cwd,
                model=model,
                effort=effort,
                permissions=permissions,
                approval_policy=approval_policy,
                input_items=input_items,
                timeout=300.0,
            )

            attempts_used += 1

            result = ProductionResultView(
                raw_result
            )

            try:
                production_cx.record_turn(
                    db,
                    cwd=cwd,
                    thread_id=thread_id,
                    prompt=attempt_input,
                    route=attempt_route,
                    model=model,
                    result=result,
                )
            except (sqlite3.Error, UnicodeEncodeError, OSError) as exc:
                print(
                    f"[cx] Uyarı: Telemetri kaydı oluşturulamadı: {exc}",
                    file=sys.stderr,
                )

            final_result = result
            final_raw = raw_result

            reason = production_cx.escalation_reason(
                result,
                policy,
            )

            is_last = (
                attempt_index
                >= len(
                    attempts
                )
            )

            if (
                reason is None
                or is_last
            ):
                break

            previous_tier = tier
            previous_model = model
            previous_reason = reason

            attempt_input = (
                production_cx.escalation_prompt(
                    reason
                )
            )

        # -------------------------------------------------------------
        # CX2 2.0.2 VERIFICATION ASSURANCE LAYER
        # -------------------------------------------------------------
        verification_assessment = None
        user_skip = is_explicit_verification_skip(prompt)
        quota_state = str(quota.get("budget_state", "normal"))

        if final_raw is not None:
            raw_cmds = [
                CommandExecutionSummary(
                    command=str(cmd.get("command") or ""),
                    exit_code=cmd.get("exit_code"),
                    duration_ms=cmd.get("duration_ms"),
                    sequence=int(cmd.get("sequence", 0)),
                    categories=list(cmd.get("categories", [])),
                    is_masked=bool(cmd.get("is_masked", False)),
                )
                for cmd in getattr(final_raw, "command_executions", [])
                if isinstance(cmd, dict)
            ]

            status_str = str(getattr(final_raw, "status", ""))
            if status_str == "interrupted" or getattr(final_raw, "interrupt_requested", False):
                verification_assessment = VerificationAssessment(
                    status="INTERRUPTED",
                    reason="INTERRUPTED",
                    evidence_level="NONE",
                    requires_continuation=False,
                    mutation_detected=bool(getattr(final_raw, "changed_files", [])),
                    changed_files=list(getattr(final_raw, "changed_files", [])),
                    last_mutation_sequence=getattr(final_raw, "last_mutation_sequence", 0),
                )
            else:
                verification_assessment = assess_turn(
                    changed_files=list(getattr(final_raw, "changed_files", [])),
                    command_executions=raw_cmds,
                    last_mutation_seq=getattr(final_raw, "last_mutation_sequence", 0),
                    is_continuation=False,
                    user_skip=user_skip,
                    quota_state=quota_state,
                    repo_root=repo.get("root", cwd),
                )

            # Verification continuation (MAX 1)
            if (
                verification_assessment.requires_continuation
                and status_str == "completed"
                and quota_state not in {"reached", "hard_stop", "stop"}
            ):
                if self.live:
                    _CX2_TERMINAL.verification_continuation_started()

                continuation_prompt = (
                    "[CX doğrulama]\n\n"
                    "Az önce yaptığın değişiklikler için yeterli doğrulama kanıtı yok.\n\n"
                    "Değişiklikleri projenin ilgili non-destructive doğrulama mekanizmasıyla doğrula.\n"
                    "Repository talimatlarını ve mevcut test yapısını kullan.\n\n"
                    "Eğer doğrulama başarısız olursa root cause'u düzelt ve değişiklikten sonra doğrulamayı tekrar çalıştır.\n\n"
                    "Başarılı doğrulamadan sonra sonucu ve kullandığın komutları kısaca bildir.\n\n"
                    "Destructive işlem yapma."
                )

                cont_raw_result = runner.run_turn(
                    thread_id=thread_id,
                    prompt=continuation_prompt,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    permissions=permissions,
                    approval_policy=approval_policy,
                    timeout=300.0,
                )

                attempts_used += 1
                cont_result = ProductionResultView(
                    cont_raw_result
                )

                try:
                    production_cx.record_turn(
                        db,
                        cwd=cwd,
                        thread_id=thread_id,
                        prompt=continuation_prompt,
                        route=attempt_route,
                        model=model,
                        result=cont_result,
                    )
                except (sqlite3.Error, UnicodeEncodeError, OSError) as exc:
                    print(
                        f"[cx] Uyarı: Telemetri kaydı oluşturulamadı: {exc}",
                        file=sys.stderr,
                    )

                # Combine turn 1 and turn 2 artifacts
                t1_files = list(getattr(final_raw, "changed_files", []))
                t2_files = list(getattr(cont_raw_result, "changed_files", []))
                combined_files = t1_files + t2_files

                t1_seq = getattr(final_raw, "event_sequence", 0)
                t2_cmds = [
                    CommandExecutionSummary(
                        command=str(cmd.get("command") or ""),
                        exit_code=cmd.get("exit_code"),
                        duration_ms=cmd.get("duration_ms"),
                        sequence=int(cmd.get("sequence", 0)) + t1_seq,
                        categories=list(cmd.get("categories", [])),
                        is_masked=bool(cmd.get("is_masked", False)),
                        display_command=str(cmd.get("display_command") or ""),
                    )
                    for cmd in getattr(cont_raw_result, "command_executions", [])
                    if isinstance(cmd, dict)
                ]
                combined_cmds = raw_cmds + t2_cmds

                t2_mut_seq = getattr(cont_raw_result, "last_mutation_sequence", 0)
                combined_last_mutation = (
                    (t2_mut_seq + t1_seq)
                    if t2_mut_seq > 0
                    else getattr(final_raw, "last_mutation_sequence", 0)
                )

                cont_status_str = str(getattr(cont_raw_result, "status", ""))
                if cont_status_str == "interrupted" or getattr(cont_raw_result, "interrupt_requested", False):
                    verification_assessment = VerificationAssessment(
                        status="INTERRUPTED",
                        reason="INTERRUPTED",
                        evidence_level="NONE",
                        requires_continuation=False,
                        mutation_detected=bool(combined_files),
                        changed_files=combined_files,
                        last_mutation_sequence=combined_last_mutation,
                        turns_evaluated=2,
                    )
                else:
                    verification_assessment = assess_turn(
                        changed_files=combined_files,
                        command_executions=combined_cmds,
                        last_mutation_seq=combined_last_mutation,
                        is_continuation=True,
                        user_skip=user_skip,
                        quota_state=quota_state,
                        repo_root=repo.get("root", cwd),
                    )

                # Authoritative response is Turn 2's response
                final_result = cont_result
                final_raw = cont_raw_result

        # Production parity:
        # persist after a result exists, not only status=completed.
        if final_result is not None:

            if final_raw is not None:

                context_info = (
                    context_info_from_turn_result(
                        final_raw
                    )
                )

            else:

                context_info = (
                    production_cx.usage_context_info(
                        final_result
                    )
                )

            try:
                if repo.get("git"):
                    save_session(
                        db,
                        repo,
                        thread_id,
                        context=context_info,
                        turns_delta=attempts_used,
                    )
                else:
                    self.active_non_git_thread_id = (
                        thread_id
                    )
                    self.active_non_git_cwd_key = (
                        current_cwd_key
                    )
                    self.active_non_git_turns += (
                        attempts_used
                    )
                    save_session(
                        db,
                        repo,
                        thread_id,
                        context=context_info,
                        turns_delta=attempts_used,
                    )
            except (sqlite3.Error, OSError) as exc:
                print(
                    f"[cx] Uyarı: Oturum kaydedilemedi: {exc}",
                    file=sys.stderr,
                )


            if self.live:
                _CX2_TERMINAL.render_context_summary(
                    context_info,
                    policy,
                )


        if (
            not self.live
            and final_result is not None
            and final_result.final_response
        ):

            print()
            print(
                final_result.final_response
            )
            print()

        if verification_assessment is not None:
            _CX2_TERMINAL.render_verification_summary(
                verification_assessment
            )

        return CX2ExecutionResult(
            blocked=False,
            thread_id=thread_id,
            session_mode=session_mode,
            plan=plan,
            quota=quota,
            final_result=final_result,
            raw_turn_result=final_raw,
            attempts_used=attempts_used,
            escalations=escalations,
            verification_summary=(
                verification_assessment.to_dict()
                if verification_assessment is not None
                else None
            ),
        )


__all__ = [
    "CX2ExecutionResult",
    "CX2Runtime",
    "CX2RuntimeError",
    "EXPECTED_ROUTER_VERSION",
    "ProductionResultView",
    "ProductionStatusView",
    "RUNTIME_VERSION",
    "initialize_params",
]
