from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid
from typing import Any

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
    evaluate_session,
    save_session,
)

from transcript_store import (
    StoredResponse,
    TranscriptStore,
)
from file_write_grants import FileWriteGrantRegistry

from telemetry_adapter import (
    context_info_from_turn_result,
)

from turn_runner import (
    StreamingTurnRunner,
    TurnRunResult,
    TurnTimeoutError,
    _CX2_TERMINAL,
)

from verification_gate import (
    CommandExecutionSummary,
    VerificationAssessment,
    _apply_required_coverage_to_assessment,
    assess_turn,
    is_explicit_verification_skip,
)

from required_verification import (
    RequiredVerificationPlan,
    extract_required_verification_plan,
)


EXPECTED_ROUTER_VERSION = "1.2.2"
RUNTIME_VERSION = "2.0.13"

DEFAULT_TURN_IDLE_TIMEOUTS: dict[str, float] = {
    "routine": 300.0,
    "standard": 450.0,
    "deep": 600.0,
}


def _bounded_trace_text(value: Any, limit: int) -> tuple[str, int, int]:
    encoded = str(value or "").encode("utf-8", errors="replace")
    total = len(encoded)
    retained = encoded[: max(0, int(limit))]
    while retained:
        try:
            text = retained.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            retained = retained[:exc.start]
    else:
        text = ""
    return text, total, max(0, total - len(retained))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
DEFAULT_TURN_HARD_TIMEOUTS: dict[str, float] = {
    "routine": 1800.0,
    "standard": 2700.0,
    "deep": 3600.0,
}

# Compatibility names for callers that treated the former absolute timeout as
# the tier timeout. It now denotes the idle limit.
DEFAULT_TURN_TIMEOUTS = DEFAULT_TURN_IDLE_TIMEOUTS

MIN_TURN_IDLE_TIMEOUT_SEC: float = 30.0
MAX_TURN_IDLE_TIMEOUT_SEC: float = 1800.0
MIN_TURN_HARD_TIMEOUT_SEC: float = 60.0
MAX_TURN_HARD_TIMEOUT_SEC: float = 7200.0
MIN_TURN_TIMEOUT_SEC = MIN_TURN_IDLE_TIMEOUT_SEC
MAX_TURN_TIMEOUT_SEC = MAX_TURN_IDLE_TIMEOUT_SEC


@dataclass(frozen=True)
class TurnTimeoutLimits:
    idle_timeout_sec: float
    hard_timeout_sec: float


def _bounded_timeout_value(
    raw_value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    import math

    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return default
    try:
        numeric_value = float(raw_value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(numeric_value):
        return default
    return max(minimum, min(numeric_value, maximum))


def resolve_turn_timeouts(
    route_or_tier: dict[str, Any] | str | None,
    policy: dict[str, Any] | None = None,
) -> TurnTimeoutLimits:
    """Resolve bounded idle/hard limits with deterministic legacy precedence.

    ``turn_idle_timeout_sec`` wins when that map exists. Only when the new idle
    map is absent does legacy ``turn_timeout_sec`` override the idle default.
    The hard limit always resolves independently from
    ``turn_hard_timeout_sec`` or its tier default. If hard resolves below idle,
    hard is raised to idle; turns therefore remain finite without discarding a
    deliberate idle setting.
    """
    if isinstance(route_or_tier, dict):
        tier = str(route_or_tier.get("tier") or "routine").lower().strip()
    elif isinstance(route_or_tier, str):
        tier = route_or_tier.lower().strip()
    else:
        tier = "routine"
    if tier not in DEFAULT_TURN_IDLE_TIMEOUTS:
        tier = "routine"

    idle_default = DEFAULT_TURN_IDLE_TIMEOUTS[tier]
    hard_default = DEFAULT_TURN_HARD_TIMEOUTS[tier]
    execution_cfg = (
        policy.get("execution")
        if isinstance(policy, dict)
        else None
    )
    if not isinstance(execution_cfg, dict):
        return TurnTimeoutLimits(idle_default, hard_default)

    if "turn_idle_timeout_sec" in execution_cfg:
        idle_cfg = execution_cfg.get("turn_idle_timeout_sec")
    else:
        idle_cfg = execution_cfg.get("turn_timeout_sec")
    hard_cfg = execution_cfg.get("turn_hard_timeout_sec")

    idle_raw = idle_cfg.get(tier) if isinstance(idle_cfg, dict) else None
    hard_raw = hard_cfg.get(tier) if isinstance(hard_cfg, dict) else None
    idle = _bounded_timeout_value(
        idle_raw,
        default=idle_default,
        minimum=MIN_TURN_IDLE_TIMEOUT_SEC,
        maximum=MAX_TURN_IDLE_TIMEOUT_SEC,
    )
    hard = _bounded_timeout_value(
        hard_raw,
        default=hard_default,
        minimum=MIN_TURN_HARD_TIMEOUT_SEC,
        maximum=MAX_TURN_HARD_TIMEOUT_SEC,
    )
    return TurnTimeoutLimits(idle, max(idle, hard))


def resolve_turn_timeout(
    route_or_tier: dict[str, Any] | str | None,
    policy: dict[str, Any] | None = None,
) -> float:
    """Compatibility resolver returning the effective idle timeout."""
    return resolve_turn_timeouts(route_or_tier, policy).idle_timeout_sec


BROAD_AUDIT_DEVELOPER_INSTRUCTIONS = """
Whole-project audit mode is active for this read-only task.

Execution and budget guidelines:
1. Prioritize critical risk surfaces: authentication, authorization, payment/billing, data consistency, security boundaries, and production configuration.
2. Avoid exhaustive traversal: Do not attempt to sequentially inspect every file in the repository. Use targeted sampling and focused queries.
3. Run verification early: Execute the most relevant test, typecheck, or lint commands during the middle phase of the audit rather than delaying them to the end.
4. Conclude with sufficient evidence: When key findings and evidence are established, stop further exploratory traversal and reserve sufficient budget to synthesize the final structured report.
5. Disclose limitations clearly: Explicitly state unverified, uninspected, or blocked areas rather than attempting unbounded traversal.
""".strip()


def is_broad_project_audit(route: dict[str, Any] | None) -> bool:
    """
    Check whether the route contains a composite broad project audit signal.
    """
    if not isinstance(route, dict):
        return False

    scope_signals = route.get("risk_signals", {}).get("scope", [])
    if "broad-project-audit" in scope_signals:
        return True

    reasons = route.get("reasons", [])
    if any("broad-project-audit" in str(r) for r in reasons):
        return True

    return False


def developer_instructions_for_route(route: dict[str, Any] | None) -> str | None:
    """
    Resolve process-local developer instructions for the given base route.
    Returns None for routine/standard/focused tasks.
    """
    if is_broad_project_audit(route):
        return BROAD_AUDIT_DEVELOPER_INSTRUCTIONS
    return None


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

    @property
    def outcome(self) -> str:
        if self.blocked:
            return "BLOCKED"
        raw_outcome = getattr(self.raw_turn_result, "outcome", None)
        if isinstance(raw_outcome, str) and raw_outcome:
            return raw_outcome
        if self.final_result is not None:
            status = str(self.final_result.status).casefold()
            if status in {"completed", "success"}:
                return "COMPLETED"
            if status == "failed":
                return "FAILED"
            if status == "interrupted":
                return "INTERRUPTED"
        return "PROCESS_OR_PROTOCOL_FAILURE"


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
        self._transcript_store: TranscriptStore | None = None
        self._transcript_store_failed = False
        self.last_trace: list[dict[str, Any]] = []
        self.last_trace_dropped_entries = 0
        self.runtime_instance_nonce = uuid.uuid4().hex
        self.file_write_grants = FileWriteGrantRegistry(self.runtime_instance_nonce)
        self._active_runtime_context: tuple[str, str] | None = None

    def _clear_non_git_identity(self) -> None:
        self.active_non_git_thread_id = None
        self.active_non_git_cwd_key = None
        self.active_non_git_turns = 0

    def reset_memory_session(
        self,
    ) -> None:
        self._clear_non_git_identity()
        self.file_write_grants.clear()
        self.last_trace = []
        self.last_trace_dropped_entries = 0
        self._active_runtime_context = None

    def _activate_runtime_context(self, *, thread_id: str, cwd_key: str) -> None:
        context = (str(thread_id), str(cwd_key))
        if self._active_runtime_context is not None and self._active_runtime_context != context:
            self.file_write_grants.clear()
            self.last_trace = []
            self.last_trace_dropped_entries = 0
        self._active_runtime_context = context

    def _visible_transcript_store(self) -> TranscriptStore | None:
        if self._transcript_store_failed:
            return None
        if self._transcript_store is None:
            try:
                self._transcript_store = TranscriptStore(
                    CX_HOME / "data" / "visible-transcript.sqlite3"
                )
            except Exception as exc:
                self._transcript_store_failed = True
                print(
                    f"[cx] Uyarı: Kalıcı transcript devre dışı ({exc}).",
                    file=sys.stderr,
                )
        return self._transcript_store

    def last_visible_response(
        self,
        *,
        cwd: Path,
        repo: dict[str, Any],
        db: Any,
    ) -> StoredResponse | None:
        store = self._visible_transcript_store()
        if store is None:
            return None
        thread_id: str | None = None
        try:
            if repo.get("git"):
                session_info = evaluate_session(db, repo)
                session = session_info.get("session")
                if not isinstance(session, dict) or not session_info.get("reusable", False):
                    # A missing, stale, or branch-mismatched persisted binding
                    # is not a safe context for showing another thread's
                    # response.
                    return None
                if not session.get("thread_id"):
                    return None
                thread_id = str(session["thread_id"])
            else:
                thread_id = self.active_non_git_thread_id
                if not thread_id:
                    # Non-git identity is intentionally memory-only. After a
                    # process restart there is no proof that a workspace row
                    # belongs to the current interaction context.
                    return None
        except Exception:
            return None
        return store.get_last(
            workspace_key=canonical_cwd_key(cwd),
            thread_id=thread_id,
        )

    def clear_visible_transcript(
        self,
        *,
        cwd: Path,
        thread_id: str | None = None,
    ) -> int:
        store = self._visible_transcript_store()
        if store is None:
            return 0
        return store.clear_scope(
            workspace_key=canonical_cwd_key(cwd),
            thread_id=thread_id,
        )

    def _capture_trace(self, raw_result: Any | None) -> None:
        entries: list[dict[str, Any]] = []
        commands = getattr(raw_result, "command_executions", []) or []
        if not isinstance(commands, list):
            commands = []
        self.last_trace_dropped_entries = max(0, len(commands) - 64)
        for raw in commands[-64:]:
            if not isinstance(raw, dict):
                continue
            command, command_total, command_dropped = _bounded_trace_text(
                raw.get("display_command") or raw.get("command") or "", 16 * 1024
            )
            cwd, cwd_total, cwd_dropped = _bounded_trace_text(raw.get("cwd") or "", 4096)
            status, status_total, status_dropped = _bounded_trace_text(
                raw.get("status") or (
                    "success" if raw.get("exit_code") in {0, "0"} else "failure"
                ), 256
            )
            classification, classification_total, classification_dropped = _bounded_trace_text(
                raw.get("classification_text") or "", 64 * 1024
            )
            output, observed_output_total, output_projection_dropped = _bounded_trace_text(
                raw.get("output_snippet") or "", 4096
            )
            output_total = max(
                observed_output_total,
                _nonnegative_int(raw.get("output_total_bytes")),
            )
            output_dropped = max(
                output_projection_dropped,
                _nonnegative_int(raw.get("output_dropped_bytes") or raw.get("dropped_bytes")),
                max(0, output_total - len(output.encode("utf-8"))),
            )
            entries.append({
                "command": command,
                "command_total_bytes": command_total,
                "command_dropped_bytes": command_dropped,
                "cwd": cwd,
                "cwd_total_bytes": cwd_total,
                "cwd_dropped_bytes": cwd_dropped,
                "status": status,
                "status_total_bytes": status_total,
                "status_dropped_bytes": status_dropped,
                "classification": classification,
                "classification_total_bytes": classification_total,
                "classification_dropped_bytes": classification_dropped,
                "exit_code": raw.get("exit_code"),
                "duration_ms": raw.get("duration_ms"),
                "sequence": raw.get("sequence"),
                "output_snippet": output,
                "output_total_bytes": output_total,
                "host_execution": bool(raw.get("bounded_host_execution") or raw.get("host_execution")),
                "output_dropped_bytes": output_dropped,
                "output_truncated": bool(
                    raw.get("output_truncated") or raw.get("truncated") or output_dropped
                ),
            })
        self.last_trace = entries


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
            if self._transcript_store is not None:
                try:
                    self._transcript_store.close()
                except Exception:
                    pass
                self._transcript_store = None
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
            # Git uses its persisted binding; clearing runtime-scoped grants on
            # every prompt would make accept-for-session a one-turn grant.
            self._clear_non_git_identity()
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
        dev_instructions = developer_instructions_for_route(base_route)

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
            developer_instructions=
                dev_instructions,
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

        self._activate_runtime_context(
            thread_id=thread_id,
            cwd_key=current_cwd_key,
        )

        transcript_store = self._visible_transcript_store()
        transcript_workspace_key = canonical_cwd_key(cwd)

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
            file_write_grants=self.file_write_grants,
            runtime_instance_nonce=self.runtime_instance_nonce,
        )
        if self.live:
            _CX2_TERMINAL.compact_tools = True

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
                    effective_sandbox=attempt_route.get(
                        "effective_sandbox"
                    ),
                    sandbox_compatibility_mode=attempt_route.get(
                        "sandbox_compatibility_mode"
                    ),
                    quota=quota,
                )

            attempt_timeouts = resolve_turn_timeouts(
                attempt_route,
                policy,
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
                idle_timeout=attempt_timeouts.idle_timeout_sec,
                hard_timeout=attempt_timeouts.hard_timeout_sec,
                transcript_store=transcript_store,
                transcript_workspace_key=transcript_workspace_key,
                transcript_display_workspace=str(cwd),
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
        required_plan = extract_required_verification_plan(prompt)

        if final_raw is not None:
            raw_cmds = [
                CommandExecutionSummary(
                    command=str(cmd.get("command") or ""),
                    exit_code=cmd.get("exit_code"),
                    duration_ms=cmd.get("duration_ms"),
                    sequence=int(cmd.get("sequence", 0)),
                    categories=list(cmd.get("categories", [])),
                    is_masked=bool(cmd.get("is_masked", False)),
                    output_snippet=str(cmd.get("output_snippet") or ""),
                    display_command=str(cmd.get("display_command") or ""),
                    classification_text=str(cmd.get("classification_text") or cmd.get("output_snippet") or ""),
                    cwd=cmd.get("cwd"),
                )
                for cmd in getattr(final_raw, "command_executions", [])
                if isinstance(cmd, dict)
            ]

            status_str = str(getattr(final_raw, "status", ""))
            if status_str == "interrupted" or getattr(final_raw, "interrupt_requested", False):
                base_interrupted = VerificationAssessment(
                    status="INTERRUPTED",
                    reason="INTERRUPTED",
                    evidence_level="NONE",
                    requires_continuation=False,
                    mutation_detected=bool(getattr(final_raw, "changed_files", [])),
                    changed_files=list(getattr(final_raw, "changed_files", [])),
                    last_mutation_sequence=getattr(final_raw, "last_mutation_sequence", 0),
                )
                verification_assessment = _apply_required_coverage_to_assessment(
                    base_interrupted,
                    required_plan,
                    raw_cmds,
                    repo_root=repo.get("root", cwd),
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
                    required_plan=required_plan,
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

                cont_timeouts = resolve_turn_timeouts(
                    attempt_route,
                    policy,
                )

                cont_raw_result = runner.run_turn(
                    thread_id=thread_id,
                    prompt=continuation_prompt,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    permissions=permissions,
                    approval_policy=approval_policy,
                    idle_timeout=cont_timeouts.idle_timeout_sec,
                    hard_timeout=cont_timeouts.hard_timeout_sec,
                    transcript_store=transcript_store,
                    transcript_workspace_key=transcript_workspace_key,
                    transcript_display_workspace=str(cwd),
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
                        output_snippet=str(cmd.get("output_snippet") or ""),
                        display_command=str(cmd.get("display_command") or ""),
                        classification_text=str(cmd.get("classification_text") or cmd.get("output_snippet") or ""),
                        cwd=cmd.get("cwd"),
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
                    base_cont_interrupted = VerificationAssessment(
                        status="INTERRUPTED",
                        reason="INTERRUPTED",
                        evidence_level="NONE",
                        requires_continuation=False,
                        mutation_detected=bool(combined_files),
                        changed_files=combined_files,
                        last_mutation_sequence=combined_last_mutation,
                        turns_evaluated=2,
                    )
                    verification_assessment = _apply_required_coverage_to_assessment(
                        base_cont_interrupted,
                        required_plan,
                        combined_cmds,
                        repo_root=repo.get("root", cwd),
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
                        required_plan=required_plan,
                    )

                # Authoritative response is Turn 2's response
                final_result = cont_result
                final_raw = cont_raw_result

        # Production parity:
        # persist after a result exists, not only status=completed.
        if final_result is not None:

            self._capture_trace(final_raw)

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
    "BROAD_AUDIT_DEVELOPER_INSTRUCTIONS",
    "CX2ExecutionResult",
    "CX2Runtime",
    "CX2RuntimeError",
    "DEFAULT_TURN_HARD_TIMEOUTS",
    "DEFAULT_TURN_IDLE_TIMEOUTS",
    "DEFAULT_TURN_TIMEOUTS",
    "EXPECTED_ROUTER_VERSION",
    "MAX_TURN_HARD_TIMEOUT_SEC",
    "MAX_TURN_IDLE_TIMEOUT_SEC",
    "MAX_TURN_TIMEOUT_SEC",
    "MIN_TURN_HARD_TIMEOUT_SEC",
    "MIN_TURN_IDLE_TIMEOUT_SEC",
    "MIN_TURN_TIMEOUT_SEC",
    "ProductionResultView",
    "ProductionStatusView",
    "RUNTIME_VERSION",
    "TurnTimeoutError",
    "TurnTimeoutLimits",
    "StoredResponse",
    "TranscriptStore",
    "developer_instructions_for_route",
    "initialize_params",
    "is_broad_project_audit",
    "resolve_turn_timeout",
    "resolve_turn_timeouts",
]
