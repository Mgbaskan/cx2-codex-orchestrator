from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import importlib.metadata
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any


# ==============================================================================
# 1. CENTRAL VALIDATED BASELINE CONSTANTS
# ==============================================================================

VALIDATED_CODEX_VERSION = "0.144.4"
VALIDATED_CODEX_PACKAGE = "openai-codex"
VALIDATED_CLI_BIN_PACKAGE = "openai-codex-cli-bin"

# Canonical reason markers (strictly preserved for backward compatibility)
REASON_PRE42_COMPATIBLE = "PINNED_01444_PRE42_STATE_COMPATIBLE"
REASON_POST42_INCOMPATIBLE = "PINNED_01444_POST42_STATE_INCOMPATIBLE"
REASON_VERSION_UNAVAILABLE = "CODEX_VERSION_UNAVAILABLE"
REASON_UNVALIDATED_VERSION = "UNVALIDATED_CODEX_VERSION"
REASON_STATE_DB_MISSING = "STATE_DB_MISSING"
REASON_STATE_SCHEMA_UNVALIDATED = "STATE_SCHEMA_UNVALIDATED"
REASON_PACKAGE_VERSION_MISMATCH = "PACKAGE_VERSION_MISMATCH"


# ==============================================================================
# 2. COMPATIBILITY STATES
# ==============================================================================

class CompatibilityState(str, Enum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_WITH_DEGRADATION = "SUPPORTED_WITH_DEGRADATION"
    UNVERIFIED = "UNVERIFIED"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


# ==============================================================================
# 3. SEMANTIC VERSION MODEL
# ==============================================================================

@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    raw: str = ""

    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            return f"{base}-{self.prerelease}"
        return base

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if self.tuple() != other.tuple():
            return self.tuple() < other.tuple()
        if self.prerelease and not other.prerelease:
            return True
        if not self.prerelease and other.prerelease:
            return False
        return (self.prerelease or "") < (other.prerelease or "")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return False
        return self.tuple() == other.tuple() and self.prerelease == other.prerelease


def parse_codex_version(text: str | None) -> SemVer | None:
    """
    Parse a Codex version string into a SemVer instance.

    Handles formats such as:
      - '0.144.4'
      - 'codex-cli 0.144.4'
      - '0.148.0-alpha.9'
      - 'codex-cli 0.148.0-alpha.9'
      - 'v0.144.4'

    Returns None for invalid, empty, or unparseable input without raising exceptions.
    """
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()
    match = re.search(
        r"(?:codex(?:-cli)?\s+)?v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None

    try:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3))
        prerelease = match.group(4)
        return SemVer(
            major=major,
            minor=minor,
            patch=patch,
            prerelease=prerelease,
            raw=cleaned,
        )
    except (ValueError, IndexError):
        return None


# ==============================================================================
# 4. CAPABILITY MODEL & PROTOCOL CONTRACT
# ==============================================================================

# CX2 communicates with Codex App Server using newline-delimited JSON (JSONL)
# request/response framing over stdio with JSON-RPC-like correlation semantics.
# Outbound messages do NOT require or emit a literal 'jsonrpc': '2.0' field.

CORE_REQUIRED_METHODS: frozenset[str] = frozenset({
    "initialize",
    "initialized",
    "thread/start",
    "thread/resume",
    "turn/start",
})

THREAD_MANAGEMENT_METHODS: frozenset[str] = frozenset({
    "thread/list",
    "thread/search",
    "thread/read",
    "thread/turns/list",
    "thread/loaded/list",
    "thread/name/set",
    "thread/archive",
    "thread/unarchive",
    "thread/delete",
})

INTERACTIVE_REQUEST_METHODS: frozenset[str] = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
    "turn/interrupt",
})

STREAMED_NOTIFICATION_METHODS: frozenset[str] = frozenset({
    "turn/started",
    "turn/completed",
    "turn/diff/updated",
    "thread/tokenUsage/updated",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "item/commandExecution/outputDelta",
})


# ==============================================================================
# 5. RUNTIME & PACKAGE VERSION DETECTION
# ==============================================================================

_VERSION_CACHE: dict[str, tuple[str | None, str | None]] = {}


def detect_codex_binary_version(
    executable: Path | str | None = None,
    *,
    timeout: float = 2.0,
    use_cache: bool = True,
) -> tuple[str | None, str | None]:
    """
    Safely detect the Codex CLI version from the given executable path.
    Runs '<binary> --version' locally with a short timeout and no model inference.

    Returns:
        (raw_version_string, error_message_or_None)
    """
    if executable is None:
        from client import CODEX_EXE
        exe_path = Path(CODEX_EXE).resolve()
    else:
        exe_path = Path(executable).expanduser().resolve()

    cache_key = str(exe_path)
    if use_cache and cache_key in _VERSION_CACHE:
        return _VERSION_CACHE[cache_key]

    if not exe_path.exists():
        res = (None, f"Executable not found: {exe_path}")
        if use_cache:
            _VERSION_CACHE[cache_key] = res
        return res

    try:
        proc = subprocess.run(
            [str(exe_path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        raw_output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0:
            res = (raw_output if raw_output else None, f"exit={proc.returncode}")
        else:
            res = (raw_output, None)
    except subprocess.TimeoutExpired:
        res = (None, f"Timeout after {timeout}s")
    except Exception as exc:
        res = (None, f"{type(exc).__name__}: {exc}")

    if use_cache:
        _VERSION_CACHE[cache_key] = res
    return res


def clear_version_cache() -> None:
    """Clear in-memory binary version cache."""
    _VERSION_CACHE.clear()


def detect_installed_package_versions() -> dict[str, str | None]:
    """
    Safely detect installed Python package versions via importlib.metadata.
    """
    results: dict[str, str | None] = {}
    for pkg in (VALIDATED_CODEX_PACKAGE, VALIDATED_CLI_BIN_PACKAGE):
        try:
            results[pkg] = importlib.metadata.version(pkg)
        except Exception:
            results[pkg] = None
    return results


def check_requirements_consistency(
    repo_root: Path | str | None = None,
) -> tuple[bool, str | None]:
    """
    Verify that requirements.txt pins match VALIDATED_CODEX_VERSION.
    """
    if repo_root is None:
        root_path = Path(__file__).resolve().parent.parent.parent
    else:
        root_path = Path(repo_root).resolve()

    req_file = root_path / "requirements.txt"
    if not req_file.is_file():
        return False, f"requirements.txt not found at {req_file}"

    try:
        content = req_file.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"Cannot read requirements.txt: {exc}"

    m_codex = re.search(r"^\s*openai-codex==([^\s]+)", content, re.MULTILINE)
    m_bin = re.search(r"^\s*openai-codex-cli-bin==([^\s]+)", content, re.MULTILINE)

    if not m_codex or not m_bin:
        return False, "openai-codex or openai-codex-cli-bin pin not found in requirements.txt"

    pinned_codex = m_codex.group(1).strip()
    pinned_bin = m_bin.group(1).strip()

    if pinned_codex != VALIDATED_CODEX_VERSION or pinned_bin != VALIDATED_CODEX_VERSION:
        return False, (
            f"requirements.txt mismatch: openai-codex={pinned_codex}, "
            f"openai-codex-cli-bin={pinned_bin} vs validated {VALIDATED_CODEX_VERSION}"
        )

    return True, None


# ==============================================================================
# 6. NATIVE DELETE SAFETY EVALUATION
# ==============================================================================

def evaluate_native_delete_safety(
    *,
    codex_home: Path | str | None = None,
    codex_exe: Path | str | None = None,
    binary_version: str | None = None,
    version_error: str | None = None,
) -> dict[str, Any]:
    """
    Evaluate whether native thread deletion is safe in the current environment
    against the local Codex SQLite state store.

    Strict safety invariants:
      - Never performs schema mutations or writes.
      - Uses read-only SQLite PRAGMA query_only = ON.
      - Preserves all canonical reason codes.
    """
    if codex_home is None:
        configured_home = os.environ.get("CODEX_HOME")
        home = (
            Path(configured_home).expanduser().resolve()
            if configured_home
            else (Path.home() / ".codex").resolve()
        )
    else:
        home = Path(codex_home).expanduser().resolve()

    if codex_exe is None:
        from client import CODEX_EXE
        executable = Path(CODEX_EXE).resolve()
    else:
        executable = Path(codex_exe).expanduser().resolve()

    if binary_version is None and version_error is None:
        raw_version, v_err = detect_codex_binary_version(executable)
    else:
        raw_version = binary_version
        v_err = version_error

    state = home / "state_5.sqlite"

    result: dict[str, Any] = {
        "supported": False,
        "reason": None,
        "binary_version": raw_version,
        "binary_path": str(executable),
        "codex_home": str(home),
        "state_path": str(state),
        "state_exists": state.is_file(),
        "has_agent_jobs": None,
        "migration_42": None,
        "version_error": v_err,
    }

    if v_err:
        result["reason"] = REASON_VERSION_UNAVAILABLE
        return result

    parsed = parse_codex_version(raw_version)
    validated = parse_codex_version(VALIDATED_CODEX_VERSION)

    if not parsed or (validated and parsed.tuple() != validated.tuple()):
        result["reason"] = REASON_UNVALIDATED_VERSION
        return result

    if not state.is_file():
        result["reason"] = REASON_STATE_DB_MISSING
        return result

    try:
        db = sqlite3.connect(
            "file:" + state.as_posix() + "?mode=ro",
            uri=True,
            timeout=2.0,
        )
    except Exception as exc:
        result["reason"] = f"STATE_DB_ERROR: {type(exc).__name__}"
        return result

    try:
        db.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        has_agent_jobs = "agent_jobs" in tables
        result["has_agent_jobs"] = has_agent_jobs

        migration_42 = None
        if "_sqlx_migrations" in tables:
            row = db.execute(
                """
                SELECT description
                FROM _sqlx_migrations
                WHERE version = 42
                  AND success = 1
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                migration_42 = str(row[0])

        result["migration_42"] = migration_42
    except Exception as exc:
        result["reason"] = f"STATE_QUERY_ERROR: {type(exc).__name__}"
        return result
    finally:
        try:
            db.close()
        except Exception:
            pass

    if result["has_agent_jobs"] is True:
        result["supported"] = True
        result["reason"] = REASON_PRE42_COMPATIBLE
        return result

    if (
        result["has_agent_jobs"] is False
        and str(result["migration_42"] or "").strip().casefold() == "drop agent jobs"
    ):
        result["reason"] = REASON_POST42_INCOMPATIBLE
        return result

    result["reason"] = REASON_STATE_SCHEMA_UNVALIDATED
    return result


# ==============================================================================
# 7. COMPREHENSIVE COMPATIBILITY ASSESSMENT
# ==============================================================================

@dataclass
class CodexCompatibilityReport:
    overall_state: CompatibilityState
    core_state: CompatibilityState
    binary_path: str | None
    binary_raw_version: str | None
    parsed_version: SemVer | None
    validated_version: str
    package_versions: dict[str, str | None]
    capabilities: dict[str, CompatibilityState]
    native_delete_safety: dict[str, Any]
    package_mismatch: bool = False
    package_mismatch_reason: str | None = None
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_fatal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_state": str(self.overall_state),
            "core_state": str(self.core_state),
            "binary_path": self.binary_path,
            "binary_raw_version": self.binary_raw_version,
            "parsed_version": str(self.parsed_version) if self.parsed_version else None,
            "validated_version": self.validated_version,
            "package_versions": self.package_versions,
            "package_mismatch": self.package_mismatch,
            "package_mismatch_reason": self.package_mismatch_reason,
            "capabilities": {k: str(v) for k, v in self.capabilities.items()},
            "native_delete_safety": self.native_delete_safety,
            "issues": self.issues,
            "warnings": self.warnings,
            "is_fatal": self.is_fatal,
        }


def assess_codex_compatibility(
    executable: Path | str | None = None,
    *,
    codex_home: Path | str | None = None,
    timeout: float = 2.0,
    use_cache: bool = True,
) -> CodexCompatibilityReport:
    """
    Perform a complete, deterministic, model-free Codex compatibility assessment.
    """
    raw_version, v_err = detect_codex_binary_version(
        executable,
        timeout=timeout,
        use_cache=use_cache,
    )
    parsed_ver = parse_codex_version(raw_version)
    validated_ver = parse_codex_version(VALIDATED_CODEX_VERSION)
    pkg_versions = detect_installed_package_versions()

    issues: list[str] = []
    warnings: list[str] = []

    # 1. Binary presence check
    if v_err and "Executable not found" in v_err:
        issues.append(f"Codex executable not found ({v_err})")
        return CodexCompatibilityReport(
            overall_state=CompatibilityState.INCOMPATIBLE,
            core_state=CompatibilityState.INCOMPATIBLE,
            binary_path=str(executable) if executable else None,
            binary_raw_version=None,
            parsed_version=None,
            validated_version=VALIDATED_CODEX_VERSION,
            package_versions=pkg_versions,
            capabilities={
                "core_app_server": CompatibilityState.INCOMPATIBLE,
                "native_delete": CompatibilityState.INCOMPATIBLE,
                "thread_archive": CompatibilityState.INCOMPATIBLE,
            },
            native_delete_safety={"supported": False, "reason": "BINARY_MISSING"},
            issues=issues,
            warnings=warnings,
            is_fatal=True,
        )

    # 2. Evaluate version compatibility
    if not parsed_ver or v_err:
        core_state = CompatibilityState.UNKNOWN
        warnings.append(f"Codex CLI version could not be parsed: {v_err or raw_version}")
    elif validated_ver and parsed_ver.tuple() == validated_ver.tuple():
        core_state = CompatibilityState.SUPPORTED
    elif validated_ver and parsed_ver.major == validated_ver.major and parsed_ver > validated_ver:
        core_state = CompatibilityState.UNVERIFIED
        warnings.append(
            f"Detected Codex CLI {parsed_ver} is newer than validated baseline {VALIDATED_CODEX_VERSION}"
        )
    elif validated_ver and parsed_ver.major != validated_ver.major:
        core_state = CompatibilityState.INCOMPATIBLE
        issues.append(
            f"Major version mismatch: detected {parsed_ver} vs validated {VALIDATED_CODEX_VERSION}"
        )
    else:
        core_state = CompatibilityState.UNVERIFIED
        warnings.append(
            f"Detected Codex CLI {parsed_ver} differs from validated baseline {VALIDATED_CODEX_VERSION}"
        )

    # 3. Package version consistency check
    pkg_codex = pkg_versions.get(VALIDATED_CODEX_PACKAGE)
    pkg_bin = pkg_versions.get(VALIDATED_CLI_BIN_PACKAGE)
    package_mismatch = False
    package_mismatch_reason = None

    if pkg_codex is not None and pkg_bin is not None:
        if pkg_codex != pkg_bin:
            package_mismatch = True
            package_mismatch_reason = REASON_PACKAGE_VERSION_MISMATCH
            warnings.append(
                f"Package version mismatch: {VALIDATED_CODEX_PACKAGE} ({pkg_codex}) != "
                f"{VALIDATED_CLI_BIN_PACKAGE} ({pkg_bin}) [{REASON_PACKAGE_VERSION_MISMATCH}]"
            )
            # Invariant: package mismatch must NEVER return SUPPORTED
            if core_state == CompatibilityState.SUPPORTED:
                core_state = CompatibilityState.UNVERIFIED
        elif pkg_codex != VALIDATED_CODEX_VERSION:
            if core_state == CompatibilityState.SUPPORTED:
                core_state = CompatibilityState.UNVERIFIED
            warnings.append(
                f"Installed packages ({pkg_codex}) differ from validated baseline ({VALIDATED_CODEX_VERSION})"
            )

    # 4. Native delete safety
    del_safety = evaluate_native_delete_safety(
        codex_home=codex_home,
        codex_exe=executable,
        binary_version=raw_version,
        version_error=v_err,
    )

    capabilities: dict[str, CompatibilityState] = {
        "core_app_server": core_state,
        "thread_history": core_state,
        "thread_archive": core_state,
    }

    if del_safety["supported"]:
        capabilities["native_delete"] = CompatibilityState.SUPPORTED
    else:
        capabilities["native_delete"] = CompatibilityState.SUPPORTED_WITH_DEGRADATION
        if del_safety.get("reason") == REASON_POST42_INCOMPATIBLE:
            warnings.append(
                "Native thread deletion safely degraded (newer Codex state schema detected; use /archive)"
            )
        elif del_safety.get("reason") == REASON_STATE_DB_MISSING:
            warnings.append("Native thread deletion safely degraded (Codex state DB missing)")
        elif del_safety.get("reason") == REASON_UNVALIDATED_VERSION:
            warnings.append("Native thread deletion safely degraded (unvalidated Codex CLI version)")

    # 5. Overall state resolution
    is_fatal = core_state == CompatibilityState.INCOMPATIBLE
    if is_fatal:
        overall_state = CompatibilityState.INCOMPATIBLE
    elif package_mismatch:
        # Package mismatch must never return SUPPORTED or SUPPORTED_WITH_DEGRADATION
        overall_state = CompatibilityState.UNVERIFIED
    elif core_state == CompatibilityState.SUPPORTED and not del_safety["supported"]:
        overall_state = CompatibilityState.SUPPORTED_WITH_DEGRADATION
    elif core_state == CompatibilityState.SUPPORTED:
        overall_state = CompatibilityState.SUPPORTED
    elif core_state == CompatibilityState.UNVERIFIED:
        overall_state = CompatibilityState.UNVERIFIED
    else:
        overall_state = CompatibilityState.UNKNOWN

    return CodexCompatibilityReport(
        overall_state=overall_state,
        core_state=core_state,
        binary_path=str(executable) if executable else del_safety.get("binary_path"),
        binary_raw_version=raw_version,
        parsed_version=parsed_ver,
        validated_version=VALIDATED_CODEX_VERSION,
        package_versions=pkg_versions,
        capabilities=capabilities,
        native_delete_safety=del_safety,
        package_mismatch=package_mismatch,
        package_mismatch_reason=package_mismatch_reason,
        issues=issues,
        warnings=warnings,
        is_fatal=is_fatal,
    )


# ==============================================================================
# 8. DOCTOR COMPATIBILITY FORMATTER
# ==============================================================================

def generate_doctor_compatibility_summary(
    report: CodexCompatibilityReport | None = None,
) -> dict[str, Any]:
    """
    Format compatibility information for doctor diagnostics.
    Does not expose sensitive credentials, tokens, or PII.
    """
    if report is None:
        report = assess_codex_compatibility()

    delete_status = "SAFE" if report.native_delete_safety.get("supported") else "DEGRADED"
    if report.native_delete_safety.get("reason") == REASON_VERSION_UNAVAILABLE:
        delete_status = "UNAVAILABLE"

    return {
        "validated_baseline": report.validated_version,
        "codex_package": report.package_versions.get(VALIDATED_CODEX_PACKAGE) or "NOT_INSTALLED",
        "cli_bin_package": report.package_versions.get(VALIDATED_CLI_BIN_PACKAGE) or "NOT_INSTALLED",
        "codex_cli_version": str(report.parsed_version) if report.parsed_version else (report.binary_raw_version or "UNKNOWN"),
        "package_mismatch": report.package_mismatch,
        "package_mismatch_reason": report.package_mismatch_reason,
        "core_compatibility": str(report.core_state),
        "overall_compatibility": str(report.overall_state),
        "native_delete": delete_status,
        "native_delete_reason": report.native_delete_safety.get("reason"),
        "is_fatal": report.is_fatal,
        "warnings": report.warnings,
        "issues": report.issues,
    }
