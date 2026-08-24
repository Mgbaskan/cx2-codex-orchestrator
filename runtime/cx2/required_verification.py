from __future__ import annotations

"""
CX2 2.0.9 Required Verification Module.

Deterministic, pure model-free extraction, normalization, and coverage tracking
for explicit user-requested quality and verification gates.
"""

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Tuple

from verification_gate import (
    CommandExecutionSummary,
    CommandOutcome,
    classify_command_outcome,
    unwrap_display_command,
)


# =============================================================================
# DATA CONTRACTS
# =============================================================================

@dataclass(frozen=True)
class RequiredVerificationGate:
    """
    A single required quality/verification gate extracted from user prompt.
    """
    id: str
    surface: str  # e.g. 'mobile/root', 'backend', 'web', 'root'
    category: str  # 'TEST' | 'LINT' | 'TYPECHECK' | 'BUILD' | 'UNSUPPORTED'
    raw_command: str
    normalized_command: str
    cwd_hint: str | None = None
    required_by: str = "explicit-user-prompt"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RequiredVerificationPlan:
    """
    Collection of required quality gates extracted from explicit user prompt.
    """
    gates: tuple[RequiredVerificationGate, ...] = field(default_factory=tuple)
    source: str = "explicit-user-prompt"
    strict: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "strict": self.strict,
            "gates": [g.to_dict() for g in self.gates],
        }


@dataclass
class GateCoverage:
    """
    Coverage status of a single required verification gate against observed executions.
    """
    gate: RequiredVerificationGate
    observed_command: dict[str, Any] | None = None
    outcome: str = "MISSING"  # PASSED | FAILED | BLOCKED | INCONCLUSIVE | INTERRUPTED | MISSING
    matched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.to_dict(),
            "observed_command": self.observed_command,
            "outcome": self.outcome,
            "matched": self.matched,
        }


@dataclass
class VerificationCoverageAssessment:
    """
    Aggregated coverage assessment comparing required gates vs observed command ledger.
    """
    required_total: int
    passed_count: int
    failed_count: int
    blocked_count: int
    inconclusive_count: int
    interrupted_count: int
    missing_count: int
    status: str  # ALL_PASSED | PARTIALLY_PASSED | FAILED | BLOCKED | INCONCLUSIVE | INTERRUPTED | UNVERIFIED
    coverages: list[GateCoverage] = field(default_factory=list)
    missing_gates: list[RequiredVerificationGate] = field(default_factory=list)
    failed_gates: list[RequiredVerificationGate] = field(default_factory=list)
    blocked_gates: list[RequiredVerificationGate] = field(default_factory=list)
    inconclusive_gates: list[RequiredVerificationGate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_total": self.required_total,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "inconclusive_count": self.inconclusive_count,
            "interrupted_count": self.interrupted_count,
            "missing_count": self.missing_count,
            "status": self.status,
            "coverages": [c.to_dict() for c in self.coverages],
            "missing_gates": [g.to_dict() for g in self.missing_gates],
            "failed_gates": [g.to_dict() for g in self.failed_gates],
            "blocked_gates": [g.to_dict() for g in self.blocked_gates],
            "inconclusive_gates": [g.to_dict() for g in self.inconclusive_gates],
        }


# =============================================================================
# EXTRACTION & FILTERING PATTERNS
# =============================================================================

UNSAFE_COMMAND_PATTERNS = [
    re.compile(r"\b(?:deploy|publish|push)\b", re.IGNORECASE),
    re.compile(r"\b(?:migrate\s+deploy|migration\s+apply|database\s+reset|drop\s+database)\b", re.IGNORECASE),
    re.compile(r"\b(?:rm|del|rmdir|git\s+reset|git\s+clean|docker\s+system\s+prune)\b", re.IGNORECASE),
]

DISQUALIFYING_LINE_PATTERNS = [
    re.compile(r"\b(?:readme|dokuman|doc|ornek|example)\b.*\b(?:kaldir\w*|sil\w*|remove\b|delete\b)", re.IGNORECASE),
    re.compile(r"\b(?:kaldir\w*|sil\w*|remove\b|delete\b).*\b(?:readme|dokuman|doc|ornek|example)\b", re.IGNORECASE),
    re.compile(r"\b(?:acikla\w*|explain\b|anlat\w*|ne\s+yaptigini\s+acikla)\b", re.IGNORECASE),
    re.compile(r"\b(?:calistirma|do\s+not\s+run|without\s+running|skip\s+tests?|test\s+etmeden)\b", re.IGNORECASE),
    re.compile(r"\b(?:ornegin|for\s+example)\s*:", re.IGNORECASE),
]

SECTION_HEADER_RE = re.compile(
    r"^(?:#+\s*)?(?:QUALITY\s+GATES?|VERIFICATION\s+GATES?|REQUIRED\s+VERIFICATION|TESTS?\s+TO\s+RUN|CHECKS?\s+TO\s+RUN|VALIDATION|DO[GĞ]RULAMA|ZORUNLU\s+DO[GĞ]RULAMA|KAL[Iİ]TE\s+KAPILAR?I|QUALITY\s+KAPILAR?I|RUN)\s*:?$",
    re.IGNORECASE,
)

SURFACE_HEADER_RE = re.compile(
    r"^(?:###*\s*)?([a-zA-Z0-9_\-\/]+)\s*:\s*$",
    re.IGNORECASE,
)

KNOWN_COMMAND_PREFIXES = (
    "npm", "pnpm", "yarn", "bun", "npx",
    "pytest", "python -m pytest", "python -m unittest",
    "go test", "go build",
    "cargo test", "cargo clippy", "cargo build",
    "tsc", "eslint", "flake8", "ruff",
    "dotnet test", "dotnet build",
    "mvn test", "mvn package",
    "gradle test", "gradle build", "./gradlew test", "./gradlew build",
)


def is_unsafe_command(cmd: str) -> bool:
    """Return True if command performs unsafe/deployment/destructive actions."""
    return any(p.search(cmd) for p in UNSAFE_COMMAND_PATTERNS)


def categorize_command(cmd: str) -> str:
    """Classify verification category into TEST, LINT, TYPECHECK, BUILD, or UNSUPPORTED/OTHER."""
    c = cmd.strip().lower()
    if is_unsafe_command(c):
        return "UNSUPPORTED"
    if re.search(r"\b(?:type-?check|typecheck|tsc)\b", c):
        return "TYPECHECK"
    if re.search(r"\b(?:lint|eslint|flake8|ruff|clippy|pylint)\b", c):
        return "LINT"
    if re.search(r"\b(?:test|jest|vitest|pytest|unittest|phpunit)\b", c):
        return "TEST"
    if re.search(r"\b(?:build|package)\b", c):
        return "BUILD"
    return "OTHER"


def normalize_canonical_command(cmd: str) -> str:
    """
    Produce conservative canonical command identity.
    Preserves specific flags (--runInBand, --noEmit) while mapping aliases.
    """
    raw = cmd.strip()
    raw = re.sub(r"^[\$>`\s]+", "", raw)
    raw = re.sub(r"[`\"'\s]+$", "", raw)
    raw = re.sub(r"^(?:powershell|pwsh|cmd|bash|sh)\s+(?:-Command|-c|\/c)\s+[\"']?", "", raw, flags=re.IGNORECASE)
    raw = raw.rstrip("\"' ")

    tokens = [t for t in raw.split() if t]
    if not tokens:
        return ""

    joined = " ".join(tokens).lower()

    # Match npm scripts
    m_npm = re.match(r"^npm\s+(?:run\s+)?([a-zA-Z0-9_\-:]+)(.*)$", joined)
    if m_npm:
        script = m_npm.group(1)
        extra = m_npm.group(2).strip()
        if script in ("type-check", "typecheck", "type_check", "type:check"):
            return "npm-script:typecheck"
        if script in ("lint", "lint:fix", "lint:check"):
            return f"npm-script:{script}"
        if script in ("build", "build:prod"):
            return f"npm-script:{script}"
        if script in ("test", "test:unit", "test:e2e"):
            return f"npm-script:{script}"
        return f"npm-script:{script}" + (f":{extra}" if extra else "")

    # Match npx / standalone jest
    m_jest = re.match(r"^(?:npx\s+)?jest(?:\.js)?(.*)$", joined)
    if m_jest:
        args = m_jest.group(1).strip()
        if "--runinband" in args or "-i" in args:
            return "jest:--runinband"
        return "jest" + (f":{args}" if args else "")

    # Match npx / standalone vitest
    m_vitest = re.match(r"^(?:npx\s+)?vitest(?:\.js)?(.*)$", joined)
    if m_vitest:
        args = m_vitest.group(1).strip()
        if "run" in args:
            return "vitest:run"
        return "vitest" + (f":{args}" if args else "")

    # Match npx / standalone tsc
    m_tsc = re.match(r"^(?:npx\s+)?tsc(?:\.js)?(.*)$", joined)
    if m_tsc:
        args = m_tsc.group(1).strip()
        if "--noemit" in args:
            return "tsc:--noemit"
        return "tsc" + (f":{args}" if args else "")

    # Match npx / standalone eslint
    m_eslint = re.match(r"^(?:npx\s+)?eslint(?:\.js)?(.*)$", joined)
    if m_eslint:
        args = m_eslint.group(1).strip()
        return "eslint" + (f":{args}" if args else "")

    # Match pytest
    if joined.startswith("pytest") or joined.startswith("python -m pytest"):
        return "pytest"

    # Match go test
    if joined.startswith("go test"):
        args = joined[len("go test"):].strip()
        return "go-test" + (f":{args}" if args else "")

    # Match cargo test
    if joined.startswith("cargo test"):
        return "cargo-test"

    # Match cargo clippy
    if joined.startswith("cargo clippy"):
        return "cargo-clippy"

    return joined


def infer_surface_cwd_hint(surface: str) -> str | None:
    """Infer the default working directory hint for a surface name."""
    s = surface.strip().lower()
    if s in ("mobile/root", "root", ".", "all"):
        return "."
    if "/" in s:
        parts = s.split("/")
        return parts[0]
    return s


def extract_required_verification_plan(prompt: str) -> RequiredVerificationPlan:
    """
    Deterministically extract required quality/verification gates from explicit user prompt.
    Filters out examples, explanations, and unsafe commands.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return RequiredVerificationPlan(gates=(), source="explicit-user-prompt", strict=True)

    text = prompt.strip()
    lines = text.splitlines()
    extracted_gates: list[RequiredVerificationGate] = []

    in_quality_section = False
    current_surface = "root"
    gate_counter = 0

    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue

        # Check section header
        if SECTION_HEADER_RE.match(trimmed):
            in_quality_section = True
            current_surface = "root"
            continue

        # If another major markdown header starts (e.g. ## Implementation Notes), quality section ends
        if in_quality_section and re.match(r"^#{1,2}\s+[A-Za-z]", trimmed):
            if not SECTION_HEADER_RE.match(trimmed):
                in_quality_section = False

        if in_quality_section:
            # Check for surface heading (e.g. "Mobile/root:", "Backend:", "Web:")
            m_surf = SURFACE_HEADER_RE.match(trimmed)
            if m_surf:
                surf_name = m_surf.group(1).strip()
                if surf_name.lower() not in ("run", "tests", "commands", "gate", "gates", "checks"):
                    current_surface = surf_name
                    continue

            # Check for command bullet or line
            cmd_line = trimmed
            m_bullet = re.match(r"^[-*+•\d.]+\s*(?:`([^`]+)`|([^\n]+))$", trimmed)
            if m_bullet:
                cmd_line = (m_bullet.group(1) or m_bullet.group(2) or "").strip()

            cleaned_cmd = cmd_line.strip("`\"' ")
            if any(cleaned_cmd.lower().startswith(prefix) for prefix in KNOWN_COMMAND_PREFIXES):
                if any(p.search(cleaned_cmd) for p in DISQUALIFYING_LINE_PATTERNS):
                    continue

                if is_unsafe_command(cleaned_cmd):
                    continue

                cat = categorize_command(cleaned_cmd)
                if cat in ("TEST", "LINT", "TYPECHECK", "BUILD"):
                    gate_counter += 1
                    norm_cmd = normalize_canonical_command(cleaned_cmd)
                    cwd_hint = infer_surface_cwd_hint(current_surface)
                    gate_id = f"gate:{current_surface.lower()}:{norm_cmd}:{gate_counter}"
                    extracted_gates.append(
                        RequiredVerificationGate(
                            id=gate_id,
                            surface=current_surface,
                            category=cat,
                            raw_command=cleaned_cmd,
                            normalized_command=norm_cmd,
                            cwd_hint=cwd_hint,
                            required_by="explicit-user-prompt",
                        )
                    )
        else:
            # Handle compact imperative single triggers (e.g. "Tests to run:\n- pytest", "Zorunlu doğrulama:\n- npm run lint")
            m_imp = re.match(r"^(?:run|tests?\s+to\s+run|zorunlu\s+do[gğ]rulama)\s*:\s*([^\n]+)$", trimmed, re.IGNORECASE)
            if m_imp:
                target_cmd = m_imp.group(1).strip("`\"' ")
                if any(target_cmd.lower().startswith(prefix) for prefix in KNOWN_COMMAND_PREFIXES):
                    if not any(p.search(target_cmd) for p in DISQUALIFYING_LINE_PATTERNS) and not is_unsafe_command(target_cmd):
                        cat = categorize_command(target_cmd)
                        if cat in ("TEST", "LINT", "TYPECHECK", "BUILD"):
                            gate_counter += 1
                            norm_cmd = normalize_canonical_command(target_cmd)
                            extracted_gates.append(
                                RequiredVerificationGate(
                                    id=f"gate:root:{norm_cmd}:{gate_counter}",
                                    surface="root",
                                    category=cat,
                                    raw_command=target_cmd,
                                    normalized_command=norm_cmd,
                                    cwd_hint=".",
                                    required_by="explicit-user-prompt",
                                )
                            )

    return RequiredVerificationPlan(
        gates=tuple(extracted_gates),
        source="explicit-user-prompt",
        strict=True,
    )


# =============================================================================
# COMMAND & SURFACE UNWRAPPING & MATCHING
# =============================================================================

def unwrap_command_and_surface(
    raw_cmd: str,
    cwd: str | None = None,
    repo_root: str | Path | None = None,
) -> Tuple[str, str]:
    """
    Unwraps wrapper syntax (cd <dir> && ..., Set-Location <dir>; ..., npm --prefix <dir> ...).
    Returns (cleaned_command, inferred_surface).
    """
    unwrapped_outer = unwrap_display_command(str(raw_cmd or "")).strip()
    text = unwrapped_outer

    inferred_surface = "root"

    # 1. Check npm --prefix <dir> run <script>
    m_pref = re.match(r"^npm\s+--prefix\s+['\"]?([a-zA-Z0-9_\-\/\\.]+)['\"]?\s+(run\s+.*|test.*|build.*)$", text, re.IGNORECASE)
    if m_pref:
        target_dir = m_pref.group(1).replace("\\", "/").rstrip("/")
        parts = [p for p in target_dir.split("/") if p and p != "."]
        if parts:
            inferred_surface = parts[-1].lower()
        sub_cmd = "npm " + m_pref.group(2)
        return sub_cmd, inferred_surface

    # 2. Check cd <dir> && <cmd> or Set-Location <dir>; <cmd> or pushd <dir> && <cmd>
    m_cd = re.match(r"^(?:cd|Set-Location|Push-Location|pushd)\s+['\"]?([a-zA-Z0-9_\-\/\\.]+)['\"]?\s*(?:&&|;)\s*(.+)$", text, re.IGNORECASE)
    if m_cd:
        target_dir = m_cd.group(1).replace("\\", "/").rstrip("/")
        parts = [p for p in target_dir.split("/") if p and p != "."]
        if parts:
            inferred_surface = parts[-1].lower()
        sub_cmd = m_cd.group(2).strip()
        return sub_cmd, inferred_surface

    # 3. Check cwd if provided
    if cwd:
        c_str = str(cwd).replace("\\", "/").rstrip("/")
        if repo_root:
            r_str = str(repo_root).replace("\\", "/").rstrip("/")
            if c_str.lower().startswith(r_str.lower()):
                rel = c_str[len(r_str):].strip("/")
                if rel:
                    parts = [p for p in rel.split("/") if p]
                    if parts:
                        inferred_surface = parts[0].lower()
        else:
            parts = [p for p in c_str.split("/") if p]
            if parts and parts[-1].lower() in ("backend", "web", "mobile", "frontend", "api", "server", "client"):
                inferred_surface = parts[-1].lower()

    return text, inferred_surface


def surface_matches(required_surface: str, observed_surface: str) -> bool:
    """Determine whether an observed execution surface satisfies a required surface."""
    req = required_surface.strip().lower()
    obs = observed_surface.strip().lower()

    if req == obs:
        return True

    if req in ("mobile/root", "root", ".", "all") and obs in ("root", "mobile", "mobile/root", ".", ""):
        return True

    if "/" in req:
        sub = req.split("/")[0]
        if sub == obs:
            return True

    return False


def evaluate_required_coverage(
    plan: RequiredVerificationPlan,
    command_executions: list[CommandExecutionSummary | dict[str, Any]],
    repo_root: str | Path | None = None,
    is_interrupted: bool = False,
) -> VerificationCoverageAssessment:
    """
    Deterministically compare required quality gates against actual observed command executions.
    """
    if not plan.gates:
        return VerificationCoverageAssessment(
            required_total=0,
            passed_count=0,
            failed_count=0,
            blocked_count=0,
            inconclusive_count=0,
            interrupted_count=0,
            missing_count=0,
            status="ALL_PASSED",
            coverages=[],
        )

    if is_interrupted:
        coverages = [
            GateCoverage(gate=g, outcome="INTERRUPTED", matched=False)
            for g in plan.gates
        ]
        return VerificationCoverageAssessment(
            required_total=len(plan.gates),
            passed_count=0,
            failed_count=0,
            blocked_count=0,
            inconclusive_count=0,
            interrupted_count=len(plan.gates),
            missing_count=0,
            status="INTERRUPTED",
            coverages=coverages,
        )

    classified_executions: list[dict[str, Any]] = []
    for cmd in command_executions:
        if isinstance(cmd, CommandExecutionSummary):
            summary = cmd
            cwd_val = getattr(cmd, "cwd", None)
        elif isinstance(cmd, dict):
            summary = CommandExecutionSummary(
                command=str(cmd.get("command") or ""),
                exit_code=cmd.get("exit_code"),
                duration_ms=cmd.get("duration_ms"),
                sequence=int(cmd.get("sequence", 0)),
                categories=list(cmd.get("categories", [])),
                is_masked=bool(cmd.get("is_masked", False)),
                output_snippet=str(cmd.get("output_snippet") or ""),
                display_command=str(cmd.get("display_command") or ""),
                classification_text=str(cmd.get("classification_text") or cmd.get("output_snippet") or ""),
            )
            cwd_val = cmd.get("cwd")
        else:
            continue

        outcome_obj = classify_command_outcome(summary)
        cleaned_cmd, obs_surface = unwrap_command_and_surface(summary.command, cwd_val, repo_root)
        norm_cmd = normalize_canonical_command(cleaned_cmd)

        classified_executions.append({
            "summary": summary,
            "outcome": outcome_obj,
            "norm_cmd": norm_cmd,
            "surface": obs_surface,
            "sequence": summary.sequence,
        })

    # Chronological sort
    classified_executions.sort(key=lambda x: x["sequence"])

    coverages: list[GateCoverage] = []
    missing_gates: list[RequiredVerificationGate] = []
    failed_gates: list[RequiredVerificationGate] = []
    blocked_gates: list[RequiredVerificationGate] = []
    inconclusive_gates: list[RequiredVerificationGate] = []

    for gate in plan.gates:
        matching_runs = [
            ex for ex in classified_executions
            if ex["norm_cmd"] == gate.normalized_command and surface_matches(gate.surface, ex["surface"])
        ]

        if not matching_runs:
            coverages.append(GateCoverage(gate=gate, observed_command=None, outcome="MISSING", matched=False))
            missing_gates.append(gate)
        else:
            # Latest execution outcome is authoritative
            latest_run = matching_runs[-1]
            out_str = latest_run["outcome"].outcome
            cov = GateCoverage(
                gate=gate,
                observed_command=latest_run["outcome"].to_dict(),
                outcome=out_str,
                matched=True,
            )
            coverages.append(cov)

            if out_str == "FAILED":
                failed_gates.append(gate)
            elif out_str == "BLOCKED":
                blocked_gates.append(gate)
            elif out_str == "INCONCLUSIVE":
                inconclusive_gates.append(gate)

    passed_count = sum(1 for c in coverages if c.outcome == "PASSED")
    failed_count = sum(1 for c in coverages if c.outcome == "FAILED")
    blocked_count = sum(1 for c in coverages if c.outcome == "BLOCKED")
    inconclusive_count = sum(1 for c in coverages if c.outcome == "INCONCLUSIVE")
    interrupted_count = sum(1 for c in coverages if c.outcome == "INTERRUPTED")
    missing_count = sum(1 for c in coverages if c.outcome == "MISSING")
    required_total = len(plan.gates)

    # Coverage status precedence
    if interrupted_count > 0:
        status = "INTERRUPTED"
    elif failed_count > 0:
        status = "FAILED"
    elif blocked_count > 0:
        status = "BLOCKED"
    elif inconclusive_count > 0:
        status = "INCONCLUSIVE"
    elif missing_count == required_total:
        status = "UNVERIFIED"
    elif missing_count > 0:
        status = "PARTIALLY_PASSED"
    else:
        status = "ALL_PASSED"

    return VerificationCoverageAssessment(
        required_total=required_total,
        passed_count=passed_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        inconclusive_count=inconclusive_count,
        interrupted_count=interrupted_count,
        missing_count=missing_count,
        status=status,
        coverages=coverages,
        missing_gates=missing_gates,
        failed_gates=failed_gates,
        blocked_gates=blocked_gates,
        inconclusive_gates=inconclusive_gates,
    )
