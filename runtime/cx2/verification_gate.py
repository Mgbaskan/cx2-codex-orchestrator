from __future__ import annotations

"""
CX2 2.0.2 Verification Gate.

Pure deterministic, model-free verification assurance layer.
No subprocess calls, no App Server client instantiation, no DB operations.
"""

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import re
from typing import Any


DOCS_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
    ".pdf",
}

DOCS_FILENAMES = {
    "license",
    "licence",
    "notice",
    "copying",
    "authors",
    "contributing",
    "changelog",
    "readme",
}

CONFIG_BUILD_FILENAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "tsconfig.json",
    "jsconfig.json",
    "nest-cli.json",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "dockerfile",
    "containerfile",
    "pyproject.toml",
    "requirements.txt",
    "pipfile",
    "pipfile.lock",
    "setup.py",
    "setup.cfg",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "cmakelists.txt",
    "makefile",
    "gemfile",
    "gemfile.lock",
    ".env.example",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
}

CONFIG_BUILD_EXTENSIONS = {
    ".csproj",
    ".fsproj",
    ".sln",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".config",
    ".props",
    ".targets",
}

SOURCE_CODE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
    ".pyw",
    ".go",
    ".rs",
    ".cs",
    ".fs",
    ".cpp",
    ".c",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".java",
    ".kt",
    ".kts",
    ".php",
    ".rb",
    ".vue",
    ".astro",
    ".svelte",
    ".swift",
    ".dart",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".psm1",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
}

TEST_DIR_PATTERNS = (
    "/test/",
    "/tests/",
    "/__tests__/",
    "/spec/",
    "/specs/",
    "\\test\\",
    "\\tests\\",
    "\\__tests__\\",
    "\\spec\\",
    "\\specs\\",
)

TEST_FILE_PATTERNS = (
    r"[-_.]test\.[a-zA-Z0-9]+$",
    r"[-_.]spec\.[a-zA-Z0-9]+$",
    r"^test_.*\.py$",
    r"^.*_test\.py$",
    r"^.*_spec\.py$",
    r"^.*Test\.java$",
    r"^.*Spec\.scala$",
    r"^.*Tests?\.cs$",
)

TEST_COMMAND_PATTERNS = [
    re.compile(
        r"\b("
        r"npm\s+(?:run\s+)?test|"
        r"pnpm\s+(?:run\s+)?test|"
        r"yarn\s+test|"
        r"bun\s+test|"
        r"npx\s+(?:jest|vitest|playwright|cypress|mocha|ava)|"
        r"pytest|"
        r"python\s+-m\s+(?:pytest|unittest)|"
        r"tox|"
        r"go\s+test|"
        r"cargo\s+test|"
        r"dotnet\s+test|"
        r"mvn\s+test|"
        r"gradle\s+test|"
        r"\./gradlew\s+test|"
        r"phpunit|"
        r"composer\s+test|"
        r"swift\s+test|"
        r"flutter\s+test|"
        r"dart\s+test"
        r")\b",
        re.IGNORECASE,
    ),
]

TYPECHECK_COMMAND_PATTERNS = [
    re.compile(
        r"\b("
        r"tsc|"
        r"npx\s+tsc|"
        r"npm\s+run\s+typecheck|"
        r"pnpm\s+(?:run\s+)?typecheck|"
        r"yarn\s+typecheck|"
        r"bun\s+typecheck|"
        r"mypy|"
        r"pyright"
        r")\b",
        re.IGNORECASE,
    ),
]

LINT_COMMAND_PATTERNS = [
    re.compile(
        r"\b("
        r"eslint|"
        r"npx\s+eslint|"
        r"npm\s+run\s+lint|"
        r"pnpm\s+(?:run\s+)?lint|"
        r"yarn\s+lint|"
        r"flake8|"
        r"ruff|"
        r"pylint|"
        r"golangci-lint|"
        r"cargo\s+clippy"
        r")\b",
        re.IGNORECASE,
    ),
]

BUILD_COMMAND_PATTERNS = [
    re.compile(
        r"\b("
        r"npm\s+run\s+build|"
        r"pnpm\s+(?:run\s+)?build|"
        r"yarn\s+build|"
        r"bun\s+build|"
        r"cargo\s+build|"
        r"go\s+build|"
        r"dotnet\s+build|"
        r"mvn\s+package|"
        r"gradle\s+build|"
        r"\./gradlew\s+build|"
        r"make"
        r")\b",
        re.IGNORECASE,
    ),
]

SKIP_PROMPT_PATTERNS = [
    re.compile(r"\btest(?:leri)?\s+(?:çalıştırma|calistirma|yapma|etme)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+(?:run\s+)?tests?\b", re.IGNORECASE),
    re.compile(r"\bskip\s+tests?\b", re.IGNORECASE),
    re.compile(r"\bno\s+tests?\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+verify\b", re.IGNORECASE),
    re.compile(r"\btest\s+etmeden\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+(?:running\s+)?tests?\b", re.IGNORECASE),
]

EXIT_MASKING_PATTERN = re.compile(r"(\|\|\s*true\b|\|\|\s*exit\s+0\b|\|\|\s*:)", re.IGNORECASE)


EXECUTABLE_NOT_FOUND_PATTERNS = [
    re.compile(r"is not recognized\b", re.IGNORECASE),
    re.compile(r"cannot find the path specified", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
    re.compile(r"\bFileNotFoundError\b", re.IGNORECASE),
    re.compile(r"\bENOENT\b", re.IGNORECASE),
]

SANDBOX_PERMISSION_PATTERNS = [
    re.compile(r"access is denied", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"\bEACCES\b", re.IGNORECASE),
    re.compile(r"\bEPERM\b", re.IGNORECASE),
    re.compile(r"operation not permitted", re.IGNORECASE),
    re.compile(r"sandbox violation", re.IGNORECASE),
    re.compile(r"child process creation denied", re.IGNORECASE),
    re.compile(r"failed to spawn", re.IGNORECASE),
]

ENV_INIT_PATTERNS = [
    re.compile(r"failed to initialize build cache", re.IGNORECASE),
    re.compile(r"failed to create cache", re.IGNORECASE),
    re.compile(r"unable to create temporary file", re.IGNORECASE),
    re.compile(r"cannot create temp", re.IGNORECASE),
]

WORKSPACE_WRITE_PATTERNS = [
    re.compile(r"read-only file system", re.IGNORECASE),
    re.compile(r"\bEROFS\b", re.IGNORECASE),
    re.compile(r"cannot write to read-only", re.IGNORECASE),
]

TIMEOUT_PATTERNS = [
    re.compile(r"timed? ?out", re.IGNORECASE),
    re.compile(r"timeout exceeded", re.IGNORECASE),
]


@dataclass
class CommandExecutionSummary:
    command: str
    exit_code: int | None = None
    duration_ms: int | None = None
    sequence: int = 0
    categories: list[str] = field(default_factory=list)
    is_masked: bool = False
    output_snippet: str = ""
    display_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommandOutcome:
    command: str
    display_command: str
    category: str
    exit_code: int | None
    duration_ms: int | None
    sequence: int
    outcome: str  # PASSED | FAILED | BLOCKED | INTERRUPTED | INCONCLUSIVE
    reason_code: str  # EXIT_SUCCESS | TEST_FAILURE | LINT_FAILURE | BUILD_FAILURE | TYPECHECK_FAILURE |
                      # SANDBOX_DENIED | PERMISSION_DENIED | EXECUTABLE_NOT_FOUND |
                      # ENVIRONMENT_INIT_FAILED | TEMP_CACHE_UNAVAILABLE | TIMEOUT |
                      # UNSUPPORTED_CAPABILITY | WORKSPACE_WRITE_REQUIRED | MASKED_EXIT_CODE |
                      # NO_EXIT_CODE | TURN_INTERRUPTED | NON_ZERO_EXIT
    output_snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_command_outcome(summary: CommandExecutionSummary) -> CommandOutcome:
    """
    Deterministically classify the outcome of a single command execution.
    Distinguishes true project test/build failures from environment/sandbox blocks.
    """
    disp = summary.display_command or unwrap_display_command(summary.command)
    primary_cat = summary.categories[0] if summary.categories else "OTHER"
    snippet = summary.output_snippet or ""

    if summary.exit_code == 0:
        if summary.is_masked:
            return CommandOutcome(
                command=summary.command,
                display_command=disp,
                category=primary_cat,
                exit_code=summary.exit_code,
                duration_ms=summary.duration_ms,
                sequence=summary.sequence,
                outcome="INCONCLUSIVE",
                reason_code="MASKED_EXIT_CODE",
                output_snippet=snippet,
            )
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="PASSED",
            reason_code="EXIT_SUCCESS",
            output_snippet=snippet,
        )

    if summary.exit_code is None:
        if any(p.search(snippet) for p in TIMEOUT_PATTERNS):
            return CommandOutcome(
                command=summary.command,
                display_command=disp,
                category=primary_cat,
                exit_code=None,
                duration_ms=summary.duration_ms,
                sequence=summary.sequence,
                outcome="BLOCKED",
                reason_code="TIMEOUT",
                output_snippet=snippet,
            )
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=None,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="INCONCLUSIVE",
            reason_code="NO_EXIT_CODE",
            output_snippet=snippet,
        )

    # Exit code != 0: Check deterministic blocked signatures first
    if any(p.search(snippet) for p in ENV_INIT_PATTERNS):
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="BLOCKED",
            reason_code="ENVIRONMENT_INIT_FAILED",
            output_snippet=snippet,
        )

    if any(p.search(snippet) for p in WORKSPACE_WRITE_PATTERNS):
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="BLOCKED",
            reason_code="WORKSPACE_WRITE_REQUIRED",
            output_snippet=snippet,
        )

    if any(p.search(snippet) for p in EXECUTABLE_NOT_FOUND_PATTERNS):
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="BLOCKED",
            reason_code="EXECUTABLE_NOT_FOUND",
            output_snippet=snippet,
        )

    if any(p.search(snippet) for p in SANDBOX_PERMISSION_PATTERNS):
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="BLOCKED",
            reason_code="SANDBOX_DENIED",
            output_snippet=snippet,
        )

    if any(p.search(snippet) for p in TIMEOUT_PATTERNS):
        return CommandOutcome(
            command=summary.command,
            display_command=disp,
            category=primary_cat,
            exit_code=summary.exit_code,
            duration_ms=summary.duration_ms,
            sequence=summary.sequence,
            outcome="BLOCKED",
            reason_code="TIMEOUT",
            output_snippet=snippet,
        )

    # Actual test / check failure in project code
    if "TEST" in summary.categories:
        reason_code = "TEST_FAILURE"
    elif "TYPECHECK" in summary.categories:
        reason_code = "TYPECHECK_FAILURE"
    elif "LINT" in summary.categories:
        reason_code = "LINT_FAILURE"
    elif "BUILD" in summary.categories:
        reason_code = "BUILD_FAILURE"
    else:
        reason_code = "NON_ZERO_EXIT"

    return CommandOutcome(
        command=summary.command,
        display_command=disp,
        category=primary_cat,
        exit_code=summary.exit_code,
        duration_ms=summary.duration_ms,
        sequence=summary.sequence,
        outcome="FAILED",
        reason_code=reason_code,
        output_snippet=snippet,
    )


@dataclass
class AuditEvidenceAssessment:
    status: str  # COMPLETE | PARTIAL | UNVERIFIED | INTERRUPTED
    reason: str
    total_checks: int
    passed_count: int
    failed_count: int
    blocked_count: int
    inconclusive_count: int
    command_outcomes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_read_only_audit(
    *,
    command_executions: list[CommandExecutionSummary],
    is_interrupted: bool = False,
) -> AuditEvidenceAssessment:
    """
    Assess verification evidence completeness for read-only audit turns.

    Conclusive outcomes (PASSED, FAILED) count as conclusive evidence.
    Non-conclusive outcomes (BLOCKED, INCONCLUSIVE) degrade completeness to PARTIAL or UNVERIFIED.
    """
    if is_interrupted:
        return AuditEvidenceAssessment(
            status="INTERRUPTED",
            reason="TURN_INTERRUPTED",
            total_checks=0,
            passed_count=0,
            failed_count=0,
            blocked_count=0,
            inconclusive_count=0,
            command_outcomes=[],
        )

    # Filter for verification-relevant commands
    relevant_cmds = [
        cmd for cmd in command_executions
        if any(c in {"TEST", "TYPECHECK", "LINT", "BUILD"} for c in cmd.categories)
    ]

    outcomes = [classify_command_outcome(cmd) for cmd in relevant_cmds]
    outcome_dicts = [o.to_dict() for o in outcomes]

    passed_count = sum(1 for o in outcomes if o.outcome == "PASSED")
    failed_count = sum(1 for o in outcomes if o.outcome == "FAILED")
    blocked_count = sum(1 for o in outcomes if o.outcome == "BLOCKED")
    inconclusive_count = sum(1 for o in outcomes if o.outcome == "INCONCLUSIVE")
    total_checks = len(outcomes)

    conclusive_count = passed_count + failed_count
    non_conclusive_count = blocked_count + inconclusive_count

    if total_checks == 0:
        return AuditEvidenceAssessment(
            status="UNVERIFIED",
            reason="NO_CHECKS_ATTEMPTED",
            total_checks=0,
            passed_count=0,
            failed_count=0,
            blocked_count=0,
            inconclusive_count=0,
            command_outcomes=[],
        )

    if conclusive_count == 0:
        return AuditEvidenceAssessment(
            status="UNVERIFIED",
            reason="ALL_CHECKS_BLOCKED",
            total_checks=total_checks,
            passed_count=passed_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            inconclusive_count=inconclusive_count,
            command_outcomes=outcome_dicts,
        )

    if non_conclusive_count > 0:
        return AuditEvidenceAssessment(
            status="PARTIAL",
            reason="SOME_CHECKS_BLOCKED",
            total_checks=total_checks,
            passed_count=passed_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            inconclusive_count=inconclusive_count,
            command_outcomes=outcome_dicts,
        )

    return AuditEvidenceAssessment(
        status="COMPLETE",
        reason="ALL_CHECKS_CONCLUSIVE",
        total_checks=total_checks,
        passed_count=passed_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
        inconclusive_count=inconclusive_count,
        command_outcomes=outcome_dicts,
    )


@dataclass
class VerificationAssessment:
    status: str  # VERIFIED | PARTIALLY_VERIFIED | FAILED | UNVERIFIED | NOT_APPLICABLE | BLOCKED | INTERRUPTED
    reason: str
    evidence_level: str  # NONE | WEAK | RELEVANT | STRONG
    requires_continuation: bool
    mutation_detected: bool
    changed_files: list[str] = field(default_factory=list)
    file_categories: list[str] = field(default_factory=list)
    dominant_category: str = "OTHER"
    executed_commands: list[dict[str, Any]] = field(default_factory=list)
    valid_evidence_commands: list[dict[str, Any]] = field(default_factory=list)
    command_outcomes: list[dict[str, Any]] = field(default_factory=list)
    audit_assessment: AuditEvidenceAssessment | None = None
    last_mutation_sequence: int = 0
    turns_evaluated: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_file_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    return cleaned


def canonicalize_file_path(path: str, repo_root: str | Path | None = None) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    p_str = path.strip().replace("\\", "/")
    while p_str.startswith("./"):
        p_str = p_str[2:]
    if p_str.startswith("a/") or p_str.startswith("b/"):
        p_str = p_str[2:]

    parts = [part for part in p_str.split("/") if part and part != "."]
    p_clean = "/".join(parts)

    if repo_root:
        r_str = str(repo_root).strip().replace("\\", "/").rstrip("/")
        r_parts = [part for part in r_str.split("/") if part and part != "."]
        r_clean = "/".join(r_parts)

        if p_clean.lower().startswith(r_clean.lower() + "/"):
            return p_clean[len(r_clean) + 1:]
        elif p_clean.lower() == r_clean.lower():
            return parts[-1] if parts else p_clean

    return p_clean


def deduplicate_changed_files(files: list[str], repo_root: str | Path | None = None) -> list[str]:
    seen_keys: set[str] = set()
    result: list[str] = []
    for f in files:
        canonical = canonicalize_file_path(f, repo_root)
        if not canonical:
            continue
        key = canonical.lower()
        if key not in seen_keys:
            seen_keys.add(key)
            result.append(canonical)
    return result


def extract_changed_files_from_diff(diff_text: str, repo_root: str | Path | None = None) -> list[str]:
    if not isinstance(diff_text, str) or not diff_text.strip():
        return []

    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            target = line[6:].strip()
            if target and target != "/dev/null":
                files.append(target)
        elif line.startswith("diff --git a/"):
            parts = line.split()
            if len(parts) >= 4:
                p_a = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                if p_a and p_a != "/dev/null":
                    files.append(p_a)
                p_b = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                if p_b and p_b != "/dev/null":
                    files.append(p_b)
    return deduplicate_changed_files(files, repo_root)


def extract_changed_files_from_items(completed_items: list[dict[str, Any]], repo_root: str | Path | None = None) -> list[str]:
    files: list[str] = []
    for item in completed_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "fileChange":
            path = item.get("path") or item.get("filePath") or item.get("file")
            if isinstance(path, str) and path.strip():
                files.append(path)
            changes = item.get("changes")
            if isinstance(changes, list):
                for ch in changes:
                    if isinstance(ch, dict):
                        ch_path = ch.get("path") or ch.get("filePath")
                        if isinstance(ch_path, str) and ch_path.strip():
                            files.append(ch_path)
    return deduplicate_changed_files(files, repo_root)


def unwrap_display_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        return ""
    text = command.strip()

    # Match PowerShell / pwsh wrapper: ...powershell.exe ... -Command "..." or '...'
    ps_match = re.search(
        r"(?:powershell|pwsh)(?:\.exe)?\b.*?(?:-command|-c)\s+(['\"])(.*?)\1",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if ps_match:
        inner = ps_match.group(2).strip()
        if inner:
            return inner

    # Match cmd.exe wrapper: ...cmd(?:\.exe)? /c "..." or '...'
    cmd_match = re.search(
        r"cmd(?:\.exe)?\s+/c\s+(['\"])(.*?)\1",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if cmd_match:
        inner = cmd_match.group(2).strip()
        if inner:
            return inner

    # Match bash/sh/zsh wrapper: ...(bash|sh|zsh) -c "..." or '...'
    sh_match = re.search(
        r"(?:bash|sh|zsh)(?:\.exe)?\s+-c\s+(['\"])(.*?)\1",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if sh_match:
        inner = sh_match.group(2).strip()
        if inner:
            return inner

    return text


def classify_file(file_path: str) -> str:
    norm = normalize_file_path(file_path).lower()
    base = os.path.basename(norm)
    name_no_ext, ext = os.path.splitext(base)

    if any(p in norm for p in TEST_DIR_PATTERNS) or any(re.search(p, base, re.IGNORECASE) for p in TEST_FILE_PATTERNS):
        return "TEST_CODE"

    # Config / build files MUST be checked before generic doc extensions (.txt, etc.)
    if (
        base in CONFIG_BUILD_FILENAMES
        or ext in CONFIG_BUILD_EXTENSIONS
        or (base.startswith("requirements") and ext == ".txt")
        or (base.startswith("constraints") and ext == ".txt")
        or (base.startswith("docker-compose") and ext in {".yml", ".yaml"})
        or (base.startswith("compose") and ext in {".yml", ".yaml"})
        or base.startswith("dockerfile")
        or base.startswith("containerfile")
        or (base.startswith("tsconfig") and ext == ".json")
        or (base.startswith("jsconfig") and ext == ".json")
        or ".github/workflows/" in norm
        or ".gitlab-ci.yml" in norm
        or ".circleci/" in norm
    ):
        return "CONFIG_BUILD"

    if ext in DOCS_EXTENSIONS or base in DOCS_FILENAMES or name_no_ext in DOCS_FILENAMES:
        return "DOCS_ONLY"

    if ext in SOURCE_CODE_EXTENSIONS:
        return "SOURCE_CODE"

    return "OTHER"


def determine_dominant_category(categories: set[str]) -> str:
    if "SOURCE_CODE" in categories:
        return "SOURCE_CODE"
    if "TEST_CODE" in categories:
        return "TEST_CODE"
    if "CONFIG_BUILD" in categories:
        return "CONFIG_BUILD"
    if categories == {"DOCS_ONLY"}:
        return "DOCS_ONLY"
    if not categories:
        return "OTHER"
    return "OTHER"


def classify_command(command_str: str) -> list[str]:
    if not isinstance(command_str, str) or not command_str.strip():
        return ["OTHER"]

    cats: list[str] = []
    text = command_str.strip()

    for pattern in TEST_COMMAND_PATTERNS:
        if pattern.search(text):
            cats.append("TEST")
            break

    for pattern in TYPECHECK_COMMAND_PATTERNS:
        if pattern.search(text):
            cats.append("TYPECHECK")
            break

    for pattern in LINT_COMMAND_PATTERNS:
        if pattern.search(text):
            cats.append("LINT")
            break

    for pattern in BUILD_COMMAND_PATTERNS:
        if pattern.search(text):
            cats.append("BUILD")
            break

    if not cats:
        cats.append("OTHER")

    return cats


def scan_top_level_segments(command_str: str) -> list[tuple[str, str | None]]:
    """
    Scans a command string and splits it into top-level segments and the operator following each segment.
    Operators recognized: '&&', '||', ';', '&', '|', '\\n'.
    Operators inside quotes ('...' or "...") or escaped (^ in cmd, ` in powershell, \\ in sh) are ignored.
    """
    if not isinstance(command_str, str) or not command_str.strip():
        return []

    cmd = command_str.strip()
    segments: list[tuple[str, str | None]] = []
    current: list[str] = []

    i = 0
    n = len(cmd)
    in_single = False
    in_double = False

    while i < n:
        ch = cmd[i]

        # Escapes outside single quotes
        if not in_single:
            # PowerShell backtick escape: `
            if ch == "`" and i + 1 < n:
                current.append(cmd[i : i + 2])
                i += 2
                continue
            # cmd caret escape: ^
            if ch == "^" and i + 1 < n:
                current.append(cmd[i : i + 2])
                i += 2
                continue
            # backslash escape: \
            if ch == "\\" and i + 1 < n and (in_double or cmd[i + 1] in "\"\'\\;&|"):
                current.append(cmd[i : i + 2])
                i += 2
                continue

        # Single quote toggle
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue

        # Double quote toggle
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        # If inside quotes, character is literal
        if in_single or in_double:
            current.append(ch)
            i += 1
            continue

        # Outside quotes: check operators
        if cmd[i : i + 2] == "||":
            segments.append(("".join(current).strip(), "||"))
            current = []
            i += 2
            continue
        elif cmd[i : i + 2] == "&&":
            segments.append(("".join(current).strip(), "&&"))
            current = []
            i += 2
            continue
        elif ch == ";":
            segments.append(("".join(current).strip(), ";"))
            current = []
            i += 1
            continue
        elif ch in ("\n", "\r"):
            if "".join(current).strip():
                segments.append(("".join(current).strip(), ";"))
                current = []
            i += 1
            continue
        elif ch == "&":
            # Ignore redirection like 2>&1 or &>
            if (i > 0 and cmd[i - 1] in ">0123456789") or (
                i + 1 < n and cmd[i + 1] in ">0123456789"
            ):
                current.append(ch)
                i += 1
                continue
            segments.append(("".join(current).strip(), "&"))
            current = []
            i += 1
            continue
        elif ch == "|":
            segments.append(("".join(current).strip(), "|"))
            current = []
            i += 1
            continue
        else:
            current.append(ch)
            i += 1

    if current and "".join(current).strip():
        segments.append(("".join(current).strip(), None))

    return segments


def _is_single_command_masked(cmd_text: str) -> bool:
    if not isinstance(cmd_text, str) or not cmd_text.strip():
        return False

    segments = scan_top_level_segments(cmd_text)
    if not segments:
        return False

    if len(segments) == 1 and segments[0][1] is None:
        return False

    for idx, (seg, op) in enumerate(segments):
        # 1. Any top-level || is masking (e.g. pytest || true, cmd || exit 0)
        if op == "||":
            return True

        # 2. Sequential operators (; or &) following a command
        if op in (";", "&") and idx < len(segments) - 1:
            next_seg = segments[idx + 1][0].strip().lower()
            if next_seg in (
                "exit $lastexitcode",
                "exit ($lastexitcode)",
                "exit %errorlevel%",
            ):
                continue
            return True

        # 3. Pipe operator | where right side might mask left side
        if op == "|" and idx < len(segments) - 1:
            next_seg = segments[idx + 1][0].strip().lower()
            if any(
                next_seg.startswith(prefix)
                for prefix in (
                    "out-null",
                    "tee-object",
                    "head",
                    "tail",
                    "grep",
                    "find",
                    "true",
                    "exit",
                )
            ):
                return True

    return False


def is_command_masked(command_str: str) -> bool:
    if not isinstance(command_str, str) or not command_str.strip():
        return False

    if _is_single_command_masked(command_str):
        return True

    unwrapped = unwrap_display_command(command_str)
    if unwrapped != command_str and _is_single_command_masked(unwrapped):
        return True

    return False


def _normalize_tr(s: str) -> str:
    mapping = {
        "ı": "i", "İ": "i", "I": "i",
        "ş": "s", "Ş": "s",
        "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u",
        "ö": "o", "Ö": "o",
        "ç": "c", "Ç": "c",
    }
    return "".join(mapping.get(ch, ch.lower()) for ch in s)


NOUN_COMPLEMENT_ROOTS = (
    "hat", "script", "mant", "sur", "komut", "fonksiy", "kod",
    "sira", "durum", "asam", "secen", "flag", "ayar", "parametr",
    "ozel", "metot", "method", "class", "sinif", "dosy", "file",
    "option", "setting", "logic", "error", "fail", "step", "behavior",
    "runner", "adim", "kural", "yapi", "modul", "alan", "buton"
)


def is_explicit_verification_skip(prompt: str) -> bool:
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    text = prompt.strip()
    for pat in SKIP_PROMPT_PATTERNS:
        for match in pat.finditer(text):
            after_text = text[match.end():].strip()
            first_word_match = re.match(r"^[^\w\s]*\s*(\w+)", after_text, re.UNICODE)
            if first_word_match:
                next_word_raw = first_word_match.group(1)
                next_word_norm = _normalize_tr(next_word_raw)
                if any(next_word_norm.startswith(root) for root in NOUN_COMPLEMENT_ROOTS):
                    continue
            return True
    return False


def assess_turn(
    *,
    changed_files: list[str],
    command_executions: list[CommandExecutionSummary],
    last_mutation_seq: int,
    is_continuation: bool = False,
    user_skip: bool = False,
    quota_state: str = "normal",
    repo_root: str | Path | None = None,
) -> VerificationAssessment:
    """
    Core deterministic evaluation of a turn or combined turns.
    """
    deduped_files = deduplicate_changed_files(changed_files, repo_root=repo_root)
    mutation_detected = bool(deduped_files)
    file_cats = set(classify_file(f) for f in deduped_files)
    dominant_cat = determine_dominant_category(file_cats)

    executed_cmd_dicts = [cmd.to_dict() for cmd in command_executions]

    all_outcomes = [classify_command_outcome(cmd) for cmd in command_executions]
    all_outcome_dicts = [o.to_dict() for o in all_outcomes]

    if user_skip:
        return VerificationAssessment(
            status="UNVERIFIED",
            reason="USER_REQUESTED_SKIP",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=mutation_detected,
            changed_files=deduped_files,
            file_categories=sorted(file_cats),
            dominant_category=dominant_cat,
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=[],
            command_outcomes=all_outcome_dicts,
            audit_assessment=None,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=2 if is_continuation else 1,
        )

    if not mutation_detected:
        audit = assess_read_only_audit(
            command_executions=command_executions,
            is_interrupted=False,
        )
        return VerificationAssessment(
            status="NOT_APPLICABLE",
            reason="NO_MUTATION",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=False,
            changed_files=[],
            file_categories=[],
            dominant_category="OTHER",
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=[],
            command_outcomes=all_outcome_dicts,
            audit_assessment=audit,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=2 if is_continuation else 1,
        )

    if dominant_cat == "DOCS_ONLY":
        return VerificationAssessment(
            status="NOT_APPLICABLE",
            reason="DOCS_ONLY_MUTATION",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=True,
            changed_files=deduped_files,
            file_categories=sorted(file_cats),
            dominant_category="DOCS_ONLY",
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=[],
            command_outcomes=all_outcome_dicts,
            audit_assessment=None,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=2 if is_continuation else 1,
        )

    # Valid evidence commands must occur strictly after the last mutation sequence
    post_mutation_cmds = [cmd for cmd in command_executions if cmd.sequence > last_mutation_seq]
    valid_evidence_dicts = [cmd.to_dict() for cmd in post_mutation_cmds]
    post_outcomes = [classify_command_outcome(cmd) for cmd in post_mutation_cmds]

    has_strong_test = any(
        o.category == "TEST" and o.outcome == "PASSED"
        for o in post_outcomes
    )

    has_relevant_typecheck_or_build = any(
        o.category in {"TYPECHECK", "BUILD"} and o.outcome == "PASSED"
        for o in post_outcomes
    )

    has_failed_cmd = any(
        o.outcome == "FAILED"
        for o in post_outcomes
    )

    has_blocked_cmd = any(
        o.outcome == "BLOCKED"
        for o in post_outcomes
    )

    if has_strong_test:
        return VerificationAssessment(
            status="VERIFIED",
            reason="VERIFIED_TEST_PASSED",
            evidence_level="STRONG",
            requires_continuation=False,
            mutation_detected=True,
            changed_files=deduped_files,
            file_categories=sorted(file_cats),
            dominant_category=dominant_cat,
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=valid_evidence_dicts,
            command_outcomes=all_outcome_dicts,
            audit_assessment=None,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=2 if is_continuation else 1,
        )

    if has_relevant_typecheck_or_build:
        if dominant_cat == "CONFIG_BUILD":
            return VerificationAssessment(
                status="PARTIALLY_VERIFIED",
                reason="PARTIALLY_VERIFIED_BUILD_PASSED",
                evidence_level="RELEVANT",
                requires_continuation=False,
                mutation_detected=True,
                changed_files=deduped_files,
                file_categories=sorted(file_cats),
                dominant_category=dominant_cat,
                executed_commands=executed_cmd_dicts,
                valid_evidence_commands=valid_evidence_dicts,
                command_outcomes=all_outcome_dicts,
                audit_assessment=None,
                last_mutation_sequence=last_mutation_seq,
                turns_evaluated=2 if is_continuation else 1,
            )
        if is_continuation:
            return VerificationAssessment(
                status="PARTIALLY_VERIFIED",
                reason="PARTIALLY_VERIFIED_STATIC_PASSED",
                evidence_level="RELEVANT",
                requires_continuation=False,
                mutation_detected=True,
                changed_files=deduped_files,
                file_categories=sorted(file_cats),
                dominant_category=dominant_cat,
                executed_commands=executed_cmd_dicts,
                valid_evidence_commands=valid_evidence_dicts,
                command_outcomes=all_outcome_dicts,
                audit_assessment=None,
                last_mutation_sequence=last_mutation_seq,
                turns_evaluated=2,
            )

    # Failed commands or no valid test
    if is_continuation:
        if has_failed_cmd:
            return VerificationAssessment(
                status="FAILED",
                reason="TEST_FAILED_AFTER_CONTINUATION",
                evidence_level="WEAK",
                requires_continuation=False,
                mutation_detected=True,
                changed_files=deduped_files,
                file_categories=sorted(file_cats),
                dominant_category=dominant_cat,
                executed_commands=executed_cmd_dicts,
                valid_evidence_commands=valid_evidence_dicts,
                command_outcomes=all_outcome_dicts,
                audit_assessment=None,
                last_mutation_sequence=last_mutation_seq,
                turns_evaluated=2,
            )
        if has_blocked_cmd:
            return VerificationAssessment(
                status="BLOCKED",
                reason="VERIFICATION_BLOCKED_AFTER_CONTINUATION",
                evidence_level="NONE",
                requires_continuation=False,
                mutation_detected=True,
                changed_files=deduped_files,
                file_categories=sorted(file_cats),
                dominant_category=dominant_cat,
                executed_commands=executed_cmd_dicts,
                valid_evidence_commands=valid_evidence_dicts,
                command_outcomes=all_outcome_dicts,
                audit_assessment=None,
                last_mutation_sequence=last_mutation_seq,
                turns_evaluated=2,
            )
        return VerificationAssessment(
            status="UNVERIFIED",
            reason="NO_VALIDATION_IN_CONTINUATION",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=True,
            changed_files=deduped_files,
            file_categories=sorted(file_cats),
            dominant_category=dominant_cat,
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=valid_evidence_dicts,
            command_outcomes=all_outcome_dicts,
            audit_assessment=None,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=2,
        )

    # First turn - check quota constraint for continuation
    if quota_state in {"reached", "hard_stop", "stop"}:
        return VerificationAssessment(
            status="UNVERIFIED",
            reason="QUOTA_HARD_STOP",
            evidence_level="NONE",
            requires_continuation=False,
            mutation_detected=True,
            changed_files=deduped_files,
            file_categories=sorted(file_cats),
            dominant_category=dominant_cat,
            executed_commands=executed_cmd_dicts,
            valid_evidence_commands=valid_evidence_dicts,
            command_outcomes=all_outcome_dicts,
            audit_assessment=None,
            last_mutation_sequence=last_mutation_seq,
            turns_evaluated=1,
        )

    # First turn requires continuation
    evidence_lvl = "RELEVANT" if has_relevant_typecheck_or_build else ("WEAK" if post_mutation_cmds else "NONE")
    return VerificationAssessment(
        status="UNVERIFIED",
        reason="CONTINUATION_REQUIRED",
        evidence_level=evidence_lvl,
        requires_continuation=True,
        mutation_detected=True,
        changed_files=deduped_files,
        file_categories=sorted(file_cats),
        dominant_category=dominant_cat,
        executed_commands=executed_cmd_dicts,
        valid_evidence_commands=valid_evidence_dicts,
        command_outcomes=all_outcome_dicts,
        audit_assessment=None,
        last_mutation_sequence=last_mutation_seq,
        turns_evaluated=1,
    )
