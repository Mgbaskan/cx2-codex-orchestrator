from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
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


class InvalidUnicodeInputError(ValueError):
    """Raised when an external input string contains unrecoverable or invalid Unicode."""
    pass


def normalize_external_text(text: str, *, source_encoding: str | None = None) -> str:
    """
    Validate and return exact Unicode text from external boundaries.

    Fast path:
        If text contains no surrogate code points (U+D800..U+DFFF), it is returned
        byte-for-byte and character-for-character intact without modification.

    Surrogateescape recovery:
        If text contains surrogateescape code points (U+DC80..U+DCFF) from an
        underlying byte stream (e.g. piped UTF-8 decoded with Windows ANSI),
        it attempts lossless recovery using the channel's source encoding and
        strict UTF-8 decoding.

    Invalid input:
        If text contains malformed/lone surrogates that cannot be losslessly
        decoded as valid UTF-8, raises InvalidUnicodeInputError.
        No silent character replacement or encoding guessing is performed.
    """
    if not isinstance(text, str):
        return text

    if not any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return text

    enc = source_encoding or getattr(sys.stdin, "encoding", None) or "utf-8"
    try:
        raw_bytes = text.encode(enc, errors="surrogateescape")
        recovered = raw_bytes.decode("utf-8")
        if not any(0xD800 <= ord(c) <= 0xDFFF for c in recovered):
            return recovered
    except Exception:
        pass

    if enc.lower() not in ("utf-8", "utf8"):
        try:
            raw_bytes = text.encode("utf-8", errors="surrogateescape")
            recovered = raw_bytes.decode("utf-8")
            if not any(0xD800 <= ord(c) <= 0xDFFF for c in recovered):
                return recovered
        except Exception:
            pass

    raise InvalidUnicodeInputError(
        "Girdi kodlaması geçersiz; metin kayıpsız çözülemedi."
    )


ensure_valid_unicode = normalize_external_text


from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox


CX_HOME = Path.home() / ".cx"
POLICY_FILE = CX_HOME / "policy.json"
DB_FILE = CX_HOME / "data" / "usage.sqlite3"
LOG_FILE = CX_HOME / "logs" / "cx.log"
MODEL_CACHE_FILE = CX_HOME / "data" / "models-visible.json"
SHIM_DIR = CX_HOME / "shims"
SHIM_CONFIG_FILE = CX_HOME / "data" / "rtk-shims.json"
QUOTA_FILE = CX_HOME / "data" / "quota-current.json"

ROUTER_VERSION = "1.2.0"


CX_CONFIG_OVERRIDES = (
    # CX coding runtime: unrelated plugin capability descriptions
    # should not occupy the model context.
    "features.plugins=false",

    # Do not inject app/plugin guidance into the coding-only CX runtime.
    "include_apps_instructions=false",

    # Multi-agent is disabled below, so its collaboration prompt is useless.
    "include_collaboration_mode_instructions=false",

    # Subagents are expensive and should not be available by default.
    # A later router layer will enable them only when justified.
    "features.multi_agent=false",

    # These MCPs remain untouched in Codex Desktop.
    # CX does not need them for normal repository coding.
    "mcp_servers.context7.enabled=false",
    "mcp_servers.filesystem.enabled=false",
    "mcp_servers.node_repl.enabled=false",

    # Keep agent final responses compact by default.
    'model_verbosity="low"',
)


def cx_runtime_env() -> dict[str, str]:
    """
    Build a process-local environment for CX.

    ~/.cx/shims is prepended only to the Codex App Server
    launched by CX. The user's global PATH and Codex Desktop
    configuration are not modified.
    """
    env = dict(os.environ)

    path_key = next(
        (
            key
            for key in env
            if key.upper() == "PATH"
        ),
        "PATH",
    )

    current = env.get(
        path_key,
        "",
    )

    shim = str(SHIM_DIR)

    parts = [
        item
        for item in current.split(
            os.pathsep
        )
        if item
        and os.path.normcase(
            os.path.abspath(
                item.strip('"')
            )
        )
        != os.path.normcase(
            os.path.abspath(shim)
        )
    ]

    env[path_key] = os.pathsep.join(
        [
            shim,
            *parts,
        ]
    )

    env["CX_RTK_ENFORCED"] = "1"

    # CX does not need RTK remote analytics.
    # Local RTK command filtering/tracking remains RTK-owned.
    env["RTK_TELEMETRY_DISABLED"] = "1"

    return env


CCE_EXE = (
    Path.home()
    / ".cx"
    / "bin"
    / "cce.exe"
)


def toml_literal(
    value: Any,
) -> str:
    """
    JSON strings/arrays are valid TOML basic values for
    the simple values CX emits here.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
    )


CCE_DEVELOPER_INSTRUCTIONS = """
CCE semantic retrieval is enabled for this turn.

For repository code discovery or questions about where behavior,
logic, configuration, dependencies, flows, or implementations live:

1. Use the CCE `context_search` MCP tool before shell search,
   grep/rg, globbing, or opening files for exploratory discovery.
2. Use `expand_chunk` when the returned compressed chunk needs
   more source context.
3. Use `related_context` when call/import relationships matter.
4. Use `index_status` only when index freshness is relevant.
5. After CCE identifies precise paths or symbols, normal targeted
   file/shell tools may be used for verification or editing.
6. Do not call CCE merely to read a file whose exact path and
   relevant location are already known.

Prefer the smallest amount of repository context required.
""".strip()


def cce_config_overrides(
    repo_root: Path,
    policy: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """
    Build process-local Codex MCP overrides for CCE.

    Nothing is written to ~/.codex/config.toml.
    """
    repo_root = repo_root.resolve()

    tools = [
        "context_search",
        "expand_chunk",
        "related_context",
        "index_status",
    ]

    startup_timeout = 20
    tool_timeout = 60

    if isinstance(
        policy,
        dict,
    ):
        cce_policy = policy.get(
            "cce",
            {},
        )

        configured_tools = cce_policy.get(
            "enabled_tools"
        )

        if isinstance(
            configured_tools,
            list,
        ) and configured_tools:
            tools = [
                str(item)
                for item in configured_tools
            ]

        startup_timeout = int(
            cce_policy.get(
                "startup_timeout_sec",
                startup_timeout,
            )
        )

        tool_timeout = int(
            cce_policy.get(
                "tool_timeout_sec",
                tool_timeout,
            )
        )

    return (
        (
            "mcp_servers.cce.command="
            + toml_literal(
                str(CCE_EXE)
            )
        ),
        (
            "mcp_servers.cce.args="
            + toml_literal(
                [
                    "serve",
                    "--project-dir",
                    str(repo_root),
                ]
            )
        ),
        "mcp_servers.cce.enabled=true",
        (
            "mcp_servers.cce.enabled_tools="
            + toml_literal(
                tools
            )
        ),
        (
            "mcp_servers.cce.startup_timeout_sec="
            + str(startup_timeout)
        ),
        (
            "mcp_servers.cce.tool_timeout_sec="
            + str(tool_timeout)
        ),
    )


def create_codex(
    *,
    cce_enabled: bool = False,
    repo_root: Path | None = None,
    policy: dict[str, Any] | None = None,
) -> Codex:
    """
    Create a CX Codex runtime.

    Default behavior remains the existing LEAN runtime.
    CCE is added only to this App Server process when
    explicitly requested.
    """
    overrides = list(
        CX_CONFIG_OVERRIDES
    )

    if cce_enabled:
        if repo_root is None:
            raise ValueError(
                "repo_root is required when CCE is enabled."
            )

        if not CCE_EXE.exists():
            raise FileNotFoundError(
                f"CCE executable not found: {CCE_EXE}"
            )

        overrides.extend(
            cce_config_overrides(
                repo_root,
                policy,
            )
        )

        overrides.append(
            "developer_instructions="
            + toml_literal(
                CCE_DEVELOPER_INSTRUCTIONS
            )
        )

    return Codex(
        CodexConfig(
            config_overrides=tuple(
                overrides
            ),
            env=cx_runtime_env(),
        )
    )


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{timestamp} {message}\n")


def should_use_cce(
    prompt: str,
    repo: dict[str, Any],
    route: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """
    Decide whether repository semantic retrieval is worth
    adding to this turn.

    Routine tasks stay LEAN by design.
    """
    cce_policy = policy.get(
        "cce",
        {},
    )

    if not bool(
        cce_policy.get(
            "enabled",
            False,
        )
    ):
        return False, "disabled"

    if not repo.get(
        "git",
        False,
    ):
        return False, "not-git"

    tier = str(
        route.get(
            "tier",
            "routine",
        )
    )

    if tier == "routine":
        return False, "routine-lean"

    if (
        tier == "deep"
        and bool(
            cce_policy.get(
                "deep_always",
                True,
            )
        )
    ):
        return True, "deep"

    if repo.get(
        "monorepo",
        False,
    ):
        return True, "monorepo"

    text = normalize(
        prompt
    )

    patterns = cce_policy.get(
        "standard_patterns",
        [],
    )

    for pattern in patterns:
        try:
            if re.search(
                str(pattern),
                text,
                re.IGNORECASE,
            ):
                return (
                    True,
                    "semantic-navigation",
                )
        except re.error:
            continue

    return False, "standard-lean"


def ensure_cce_index(
    repo: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """
    Incrementally refresh the external CCE index.

    `cce index` stores index state outside the repository
    and does not require `cce init`.
    """
    if not repo.get(
        "git",
        False,
    ):
        return False, "not-git"

    if not CCE_EXE.exists():
        return False, "cce-not-installed"

    root = Path(
        str(
            repo["root"]
        )
    ).resolve()

    timeout = int(
        policy.get(
            "cce",
            {},
        ).get(
            "index_timeout_sec",
            600,
        )
    )

    try:
        completed = subprocess.run(
            [
                str(CCE_EXE),
                "index",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, "index-timeout"
    except Exception as exc:
        log(
            "CCE INDEX ERROR "
            f"repo={root} "
            f"error={exc!r}"
        )
        return False, "index-error"

    if completed.returncode != 0:
        log(
            "CCE INDEX FAILED "
            f"repo={root} "
            f"exit={completed.returncode} "
            f"stderr={completed.stderr[-1000:]!r}"
        )
        return (
            False,
            f"index-exit-{completed.returncode}",
        )

    return True, "indexed"


def load_policy() -> dict[str, Any]:
    return json.loads(POLICY_FILE.read_text(encoding="utf-8-sig"))


def normalize(text: str) -> str:
    """
    Promptlari routing icin tek ASCII-benzeri forma getirir.

    Ornek:
        düzelt   -> duzelt
        değiştir -> degistir
        güvenlik -> guvenlik
        tutarlılığı -> tutarliligi

    Boylece Turkce karakterli ve karaktersiz promptlar
    ayni routing kurallarina girer.
    """
    value = text.casefold()

    # NFKD'nin ayristirmadigi Turkce dotless-i.
    value = value.replace("ı", "i")

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        ch
        for ch in value
        if not unicodedata.combining(ch)
    )

    return value


def regex_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def run_local(
    args: list[str],
    cwd: Path,
    timeout: float = 2.0,
) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return completed.stdout.strip()
    except Exception:
        return ""


def detect_repo(cwd: Path) -> dict[str, Any]:
    repo: dict[str, Any] = {
        "cwd": str(cwd),
        "git": False,
        "root": str(cwd),
        "stacks": [],
        "monorepo": False,
        "dirty_files": 0,
    }

    root_text = run_local(
        ["git", "rev-parse", "--show-toplevel"],
        cwd,
    )

    if root_text:
        root = Path(root_text)
        repo["git"] = True
        repo["root"] = str(root)
    else:
        root = cwd

    markers = {
        "node": ["package.json"],
        "typescript": ["tsconfig.json"],
        "go": ["go.mod"],
        "php": ["composer.json"],
        "python": ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py"],
        "rust": ["Cargo.toml"],
        "dotnet": [],
        "docker": [
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
            "Dockerfile",
            "Containerfile",
        ],
        "astro": [
            "astro.config.mjs",
            "astro.config.ts",
            "astro.config.js",
            "astro.config.cjs",
        ],
        "nestjs": ["nest-cli.json"],
    }

    for stack, files in markers.items():
        if any((root / file).exists() for file in files):
            repo["stacks"].append(stack)

    if (root / "docker").is_dir() and "docker" not in repo["stacks"]:
        repo["stacks"].append("docker")

    if list(root.glob("*.sln")) or list(root.glob("*.csproj")) or list(root.glob("*.fsproj")):
        if "dotnet" not in repo["stacks"]:
            repo["stacks"].append("dotnet")

    # Detect frameworks from package.json if present
    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            deps: dict[str, Any] = {}
            for dep_field in ("dependencies", "devDependencies", "peerDependencies"):
                val = pkg_data.get(dep_field)
                if isinstance(val, dict):
                    deps.update(val)
            if any(k.startswith("react") or k in {"react", "react-native", "react-dom", "next"} for k in deps):
                if "react" not in repo["stacks"]:
                    repo["stacks"].append("react")
            if any(k.startswith("@nestjs/") or k == "nestjs" for k in deps):
                if "nestjs" not in repo["stacks"]:
                    repo["stacks"].append("nestjs")
            if "astro" in deps:
                if "astro" not in repo["stacks"]:
                    repo["stacks"].append("astro")
        except Exception:
            pass

    monorepo_markers = [
        "pnpm-workspace.yaml",
        "turbo.json",
        "nx.json",
        "lerna.json",
    ]

    repo["monorepo"] = any(
        (root / marker).exists()
        for marker in monorepo_markers
    )

    if repo["git"]:
        status = run_local(
            ["git", "status", "--porcelain"],
            root,
        )

        if status:
            repo["dirty_files"] = len(status.splitlines())

        ls_files = run_local(
            ["git", "ls-files"],
            root,
            timeout=2.0,
        )

        if ls_files:
            tracked_count = len(ls_files.splitlines())
            repo["tracked_files"] = tracked_count
            if tracked_count >= 2000:
                repo["tracked_files_bucket"] = "large"
            elif tracked_count >= 200:
                repo["tracked_files_bucket"] = "medium"
            else:
                repo["tracked_files_bucket"] = "small"
        else:
            repo["tracked_files"] = 0
            repo["tracked_files_bucket"] = "small"

    return repo


# ==============================================================================
# RISK ENGINE V2: DETERMINISTIC RULE & SIGNAL DEFINITIONS
# ==============================================================================

# Critical Concurrency / Deadlock / Thread Safety (Weight: +4, Dominance >= deep_min)
CRITICAL_CONCURRENCY_RULES: list[str] = [
    r"\brace condition\b|\bdata race\b|\bthread race\b|\byaris durum\w*",
    r"\bconcurren\w*|\bdeadlock\b|\block contention\b|\bthread safety\b|\bthread[- ]safe\b|\beszamanli\w*|\bkilitlen\w*",
    r"\bdistributed transaction\w*|\bdata consistency\b|\bveri tutarl\w*",
]

# Structural / Architecture / Major Refactor (Weight: +3)
STRUCTURAL_RULES: list[str] = [
    r"\barchitecture\w*|\bmimari\w*|\bsystem design\b",
    r"\blarge refactor\b|\bbuyuk refactor\b|\bmajor refactor\b|\bmonorepo[- ]wide refactor\b",
    r"\bdistributed system\w*|\bdistributed architecture\b",
]

# Analysis & Flow Inspection (Weight: +2)
ANALYSIS_RULES: list[str] = [
    r"\b(?:explain|trace|clarify|acikla|incele|analiz)\w*\s+.*\b(?:flow|architecture|pipeline|lifecycle|akisi|mimarisi)\b",
    r"\b(?:security\s+review|security\s+audit|guvenlik\s+incelemesi|guvenlik\s+denetimi)\b",
    r"\b(?:review|inspect|audit|incele|denetle)\w*\s+.*\b(?:module|service|system|architecture|security|auth|modulu|servisi|sistemi)\b",
    r"\b(?:guvenlik|kimlik\s+dogrulama|auth|security|mimari|sistem|modul\w*|servis\w*)\s+.*\b(?:incele\w*|denetle\w*|analiz\s+et\w*|acikla\w*)\b",
    r"\b(?:authentication\s+flow|auth\s+flow|kimlik\s+dogrulama\s+akisi|oauth\s+flow|token\s+flow)\b",
    r"\b(?:analyze|analyse|analiz\s+et)\w*\b",
]

# Root Cause / Deep Diagnostics (Weight: +2)
ROOT_CAUSE_RULES: list[str] = [
    r"\broot cause\b|\bkok neden\w*|\bnedenini bul\b",
]

# Domain Context Keywords (Weight: +1 each, bounded to max +3 total)
DOMAIN_RULES: list[tuple[str, str]] = [
    (r"\bauth\b|\bauthentication\w*|\bauthorization\w*|\brbac\b|\bjwt\b|\byetki\w*|\boauth\b|\btoken\b|\bsession\b|\bkimlik dogrulama\w*", "auth"),
    (r"\bsecurity\w*|\bguvenlik\w*|\bvulnerab\w*|\bacik\w*|\bcrypto\w*|\bsecret\w*|\bgizli anahtar\w*", "security"),
    (r"\bmigration\w*|\bmigrasyon\w*|\bschema\w*|\bdatabase\b|\bveritabani\w*|\bsql\b|\bprisma\b", "db-schema"),
    (r"\bproduction\b|\bprod\b|\bcanli ortam\b|\bcanli dagitim\w*", "production"),
    (r"\bdeployment\w*|\bdeploy\w*|\bci/?cd\b|\bkubernetes\b|\bk8s\b|\bdocker\w*", "deployment"),
    (r"\bmicroservice\w*|\bmulti[- ]service\b|\bdistributed\b", "distributed"),
    (r"\btransaction\w*|\brollback\b|\bgeri alma\b|\bislem yonetim\w*", "transaction"),
]

# High-Risk / Broad Task Scope (Weight: +3)
BROAD_SCOPE_RULES: list[str] = [
    r"\b(?:whole|entire|all)\s+(?:repo|repository|project|codebase)\b|\btum\s+(?:proje|repo|kod|servisler)\w*|\bbutun\s+(?:proje|repo|kod|servisler)\w*|\bkomple\s+(?:proje|repo)\w*",
    r"\b(?:all|across\s+all)\s+(?:services|packages|modules|microservices)\b|\bcross[- ]service\b|\bsystem[- ]wide\b|\bmonorepo[- ]wide\b|\btum\s+servisler\s+genelinde\b",
    r"\bfull[- ]stack\s+refactor\b|\bdatabase\s*\+\s*backend\s*\+\s*(?:deployment|frontend)\b",
]

# Sensitive Surface & Mutation Risk (Evaluated when mutating is True)
SENSITIVE_MUTATION_RULES: list[tuple[str, int, str]] = [
    (r"\b(?:refresh\s+token|token\s+rotation|token\s+refresh|session\s+handling|auth\s+logic|jwt\s+signing|password\s+hash|oauth\s+flow)\b|\btoken\s+yenileme\w*", 2, "auth-token-mutation"),
    (r"\b(?:production\s+(?:database\s+)?migration|database\s+migration\s+.*rollback|migration\s+rollback|veritabani\s+migrasyon\w*\s+.*geri\s+alma|canli\s+ortam\s+veritabani\s+migrasyon\w*)\b", 5, "production-db-migration-mutation"),
    (r"\b(?:database\s+migration|db\s+migration|schema\s+migration|alter\s+table)\b|\bveritabani\s+migrasyon\w*", 3, "db-migration-mutation"),
    (r"\b(?:kubernetes\s+(?:production\s+)?deployment|production\s+deployment|deployment\s+config|ci/cd\s+workflow|canli\s+dagitim\w*)\b", 3, "infra-deployment-mutation"),
    (r"\b(?:secret\s+handling|secret\s+rotation|credential\s+handling|credential\s+rotation|api\s+key\s+rotation|gizli\s+anahtar\w*)\b", 2, "secret-credential-mutation"),
    (r"\b(?:upgrade\s+dependencies|rewrite\s+lockfile|package-lock\.json|pnpm-lock\.yaml|requirements\.txt)\b", 1, "dependency-lockfile-mutation"),
]

# Routine / Low-Risk Keywords (Reductions: -1 per hit, capped at -3)
ROUTINE_RULES: list[str] = [
    r"\bcss\b",
    r"\btailwind\b",
    r"\bpadding\b",
    r"\bmargin\b",
    r"\bcolor\b|\brenk\w*",
    r"\bbutton\b|\bbuton\w*",
    r"\bicon\b|\bikon\w*",
    r"\btypo\b|\byazim\w*",
    r"\brename\b|\byeniden adlandir\w*",
    r"\blabel\b",
    r"\btext\b|\bmetin\w*",
    r"\bcomment\b|\byorum\w*",
    r"\bformatting\b",
    r"\breadme(?:\.md)?\b",
    r"\bdocs?\b|\bdokuman\w*",
]

# Backward compatibility alias
DEEP_RULES: list[tuple[str, int]] = [
    (r"\brace condition\b|\bdata race\b|\bthread race\b|\byaris durum\w*", 4),
    (r"\bconcurren\w*|\bdeadlock\b|\block contention\b|\bthread safety\b|\bthread[- ]safe\b|\beszamanli\w*|\bkilitlen\w*", 4),
    (r"\barchitecture\w*|\bmimari\w*|\bsystem design\b", 3),
    (r"\bdistributed transaction\w*|\bdata consistency\b|\bveri tutarl\w*", 4),
    (r"\blarge refactor\b|\bbuyuk refactor\b|\bmajor refactor\b|\bmonorepo[- ]wide refactor\b", 3),
    (r"\broot cause\b|\bkok neden\w*|\bnedenini bul\b", 2),
]


# "write/yaz" can describe the answer format instead of a
# repository mutation. These cases are removed before
# WRITE_RULES scanning.
OUTPUT_ONLY_WRITE_RULES = (
    r"\bsadece\s+(?:evet|hayir)(?:\s+veya\s+(?:evet|hayir))?\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:cevabi|yaniti|sonucu)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:cevap\w*|yanit\w*|sonuc\w*)\s+(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:tek|bir)\s+kelime(?:yle)?\s+yaz(?:\b|in\b|iniz\b)",
    r"\bwrite\s+(?:only\s+)?(?:yes|no)(?:\s+or\s+(?:yes|no))?\b",
    r"\bwrite\s+(?:the\s+)?(?:answer|result)\s+(?:here|in\s+(?:the\s+)?chat)\b",
    # CX113_OUTPUT_REPEAT_RULES
    # Explicit references to a PREVIOUS CHAT ANSWER only.
    r"\b(?:onceki|son)\s+(?:cevabi|yaniti|sonucu)\s+(?:aynen\s+)?tekrar\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:bir\s+)?onceki\s+kullanici\s+sorusuna\s+verdigin(?:\s+\w+){0,5}\s+(?:cevabi|yaniti|sonucu)\s+(?:aynen\s+)?tekrar\s+yaz(?:\b|in\b|iniz\b)",
)


# Common Turkish write-verb forms.
#
# Deliberately does NOT match nouns such as:
#   yazim
#   yazilim
WRITE_VERB_RULE = (
    r"\bwrite\b|"
    r"\byaz\b|"
    r"\byazar\s+misin\b|"
    r"\byazabilir\s+misin\b|"
    r"\byaz(?:in|iniz|sin|siniz|alim)\b|"
    r"\byaz(?:iyor|iyorum|iyoruz|iyorsun|iyorlar)\b|"
    r"\byazdir\w*|"
    r"\byazacak\w*|"
    r"\byazmali\w*|"
    r"\byazip\b|"
    r"\byazarak\b"
)


WRITE_RULES: list[str] = [
    r"\bfix\b|\bduzelt\w*",
    r"\bimplement\w*|\buygula\w*",
    r"\badd\b|\bekle\w*",
    r"\bcreate\b|\bolustur\w*",
    WRITE_VERB_RULE,
    r"\brefactor\w*",
    r"\bupdate\b|\bguncelle\w*",
    r"\bchange\b|\bdegistir\w*",
    r"\bdelete\b|\bsil\w*",
    r"\bremove\b|\bkaldir\w*",
    r"\bmigrate\b|\btasi\w*",
    r"\bconvert\b|\bdonustur\w*",
    r"\binstall\b|\bkur\b|\bkurulum\b",
    r"\bresolve\b|\bcoz\w*",
    r"\brename\b|\byeniden\s+adlandir\w*",
]


NEGATED_WRITE_RULES: list[str] = [
    # Turkish
    r"\bhicbir\s+dosya(?:yi|lari)?\s+degistirme\b",
    r"\bdosya(?:yi|lari)?\s+degistirme\b",
    r"\bdosyalarda\s+degisiklik\s+yapma\b",
    r"\bdegisiklik\s+yapma\b",
    r"\bdosya(?:ya|lara)?\s+yazma\b",
    r"\bsadece\s+oku\b",
    r"\byalnizca\s+oku\b",

    # English
    r"\bdo\s+not\s+(?:modify|change|edit|write)"
    r"(?:\s+(?:any\s+)?files?)?\b",

    r"\bdon['’]?t\s+(?:modify|change|edit|write)"
    r"(?:\s+(?:any\s+)?files?)?\b",

    r"\bno\s+(?:file\s+)?changes?\b",
    r"\bread[- ]only\b",
]


def strip_negated_write_phrases(
    text: str,
) -> str:
    """
    Remove phrases that must not imply repository mutation
    before WRITE_RULES scanning.

    Explicit negated mutation instructions and explicit
    chat/output-only write instructions are removed. Any other
    positive mutation instruction remains visible.
    """
    result = text

    for pattern in NEGATED_WRITE_RULES:
        result = re.sub(
            pattern,
            " ",
            result,
            flags=re.IGNORECASE,
        )

    for pattern in OUTPUT_ONLY_WRITE_RULES:
        result = re.sub(
            pattern,
            " ",
            result,
            flags=re.IGNORECASE,
        )

    return result


def extract_referenced_paths(text: str) -> list[str]:
    """
    Conservatively extract explicit file or directory path references from prompt text.
    """
    candidates = re.findall(
        r"(?:[a-zA-Z0-9_.-]+[/\\])+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]+|\b[a-zA-Z0-9_.-]+\.(?:ts|tsx|js|jsx|py|go|rs|php|cs|java|cpp|c|h|md|json|yml|yaml|toml|sql|prisma|sh|ps1|html|css|scss)\b|\bDockerfile\b|\bContainerfile\b",
        text,
    )
    return list(dict.fromkeys(candidates))


def evaluate_lexical_signals(
    text: str,
) -> tuple[int, list[str], list[str], bool]:
    """
    Evaluate lexical complexity signals.
    Returns (points, reasons, signal_tags, has_critical_concurrency).
    """
    points = 0
    reasons: list[str] = []
    tags: list[str] = []
    has_critical = False

    for pat in CRITICAL_CONCURRENCY_RULES:
        if re.search(pat, text, re.IGNORECASE):
            has_critical = True
            tags.append(f"critical-concurrency:{pat[:25]}")
            reasons.append("+4:critical-concurrency")
            points += 4

    for pat in STRUCTURAL_RULES:
        if re.search(pat, text, re.IGNORECASE):
            tags.append(f"structural:{pat[:25]}")
            reasons.append("+3:structural")
            points += 3

    if re.search(r"\brefactor\w*", text, re.IGNORECASE) and not any("structural" in t for t in tags):
        tags.append("refactor")
        reasons.append("+2:refactor")
        points += 2

    for pat in ROOT_CAUSE_RULES:
        if re.search(pat, text, re.IGNORECASE):
            tags.append("root-cause")
            reasons.append("+2:root-cause")
            points += 2

    for pat in ANALYSIS_RULES:
        if re.search(pat, text, re.IGNORECASE):
            tags.append("analysis-flow")
            reasons.append("+2:analysis-flow")
            points += 2
            break

    domain_hits = 0
    for pat, label in DOMAIN_RULES:
        if re.search(pat, text, re.IGNORECASE):
            domain_hits += 1
            tags.append(f"domain:{label}")

    if domain_hits > 0:
        domain_points = min(3, domain_hits)
        reasons.append(f"+{domain_points}:domain-context")
        points += domain_points

    return points, reasons, tags, has_critical


def evaluate_scope_signals(
    text: str,
) -> tuple[int, list[str], list[str]]:
    """
    Evaluate task scope signals.
    Returns (points, reasons, signal_tags).
    """
    points = 0
    reasons: list[str] = []
    tags: list[str] = []

    for pat in BROAD_SCOPE_RULES:
        if re.search(pat, text, re.IGNORECASE):
            tags.append(f"broad-scope:{pat[:25]}")
            reasons.append("+3:broad-scope")
            points += 3

    return points, reasons, tags


def evaluate_sensitive_mutation_signals(
    text: str,
    mutating: bool,
) -> tuple[int, list[str], list[str]]:
    """
    Evaluate sensitive surface mutation risk (only when mutating is True).
    Returns (points, reasons, signal_tags).
    """
    points = 0
    reasons: list[str] = []
    tags: list[str] = []

    if not mutating:
        return points, reasons, tags

    matched_labels: set[str] = set()
    for pat, weight, label in SENSITIVE_MUTATION_RULES:
        if re.search(pat, text, re.IGNORECASE):
            if label == "db-migration-mutation" and "production-db-migration-mutation" in matched_labels:
                continue
            matched_labels.add(label)
            tags.append(label)
            reasons.append(f"+{weight}:{label}")
            points += weight

    return points, reasons, tags


def evaluate_repository_signals(
    repo: dict[str, Any],
) -> tuple[int, list[str], list[str]]:
    """
    Evaluate repository contextual risk.
    Returns (points, reasons, signal_tags).
    """
    points = 0
    reasons: list[str] = []
    tags: list[str] = []

    if repo.get("monorepo"):
        tags.append("monorepo")
        reasons.append("+1:monorepo")
        points += 1

    tracked_files = int(repo.get("tracked_files", 0))
    bucket = repo.get("tracked_files_bucket")
    if tracked_files >= 2000 or bucket == "large":
        tags.append("large-repo")
        reasons.append("+2:large-repo")
        points += 2
    elif tracked_files >= 200 or bucket == "medium":
        tags.append("medium-repo")
        reasons.append("+1:medium-repo")
        points += 1

    dirty_files = int(repo.get("dirty_files", 0))
    if dirty_files >= 20:
        tags.append("large-dirty-tree")
        reasons.append("+1:large-dirty-tree")
        points += 1

    return points, reasons, tags


def evaluate_routine_reductions(
    text: str,
) -> tuple[int, list[str], list[str]]:
    """
    Evaluate routine/low-risk task reductions.
    Returns (reduction_points, reasons, signal_tags).
    """
    tags: list[str] = []
    reasons: list[str] = []

    routine_hits = sum(
        1 for pat in ROUTINE_RULES
        if re.search(pat, text, re.IGNORECASE)
    )

    reduction = 0
    if routine_hits > 0:
        reduction = min(3, routine_hits)
        tags.append(f"routine-hits:{routine_hits}")
        reasons.append(f"-{reduction}:routine")

    return reduction, reasons, tags


def classify(
    prompt: str,
    repo: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """
    CX2 Deterministic Risk Engine v2 Classifier.

    Evaluates prompt lexical complexity, task scope, sensitive surfaces,
    mutation risk, and repository context without model inference.
    """
    text = normalize(prompt)
    reasons: list[str] = []
    signals: dict[str, list[str]] = {
        "lexical": [],
        "repository": [],
        "scope": [],
        "mutation": [],
        "sensitive": [],
        "reductions": [],
    }
    score_breakdown: dict[str, int] = {
        "lexical": 0,
        "repository": 0,
        "scope": 0,
        "mutation": 0,
        "sensitive": 0,
        "reductions": 0,
    }

    # 1. Write intent / sandbox determination (strictly separated)
    write_scan_text = strip_negated_write_phrases(text)
    mutating = regex_any(write_scan_text, WRITE_RULES)
    sandbox = "workspace-write" if mutating else "read-only"
    if mutating:
        signals["mutation"].append("workspace-write")
    else:
        signals["mutation"].append("read-only")

    # 2. Lexical complexity signals
    lex_points, lex_reasons, lex_tags, has_critical_concurrency = evaluate_lexical_signals(text)
    score_breakdown["lexical"] += lex_points
    reasons.extend(lex_reasons)
    signals["lexical"].extend(lex_tags)

    # 3. Task Scope signals
    scope_points, scope_reasons, scope_tags = evaluate_scope_signals(text)
    score_breakdown["scope"] += scope_points
    reasons.extend(scope_reasons)
    signals["scope"].extend(scope_tags)

    # 4. Sensitive Surface & Mutation Risk
    sens_points, sens_reasons, sens_tags = evaluate_sensitive_mutation_signals(text, mutating)
    score_breakdown["sensitive"] += sens_points
    reasons.extend(sens_reasons)
    signals["sensitive"].extend(sens_tags)

    # 5. Repository signals
    repo_points, repo_reasons, repo_tags = evaluate_repository_signals(repo)
    score_breakdown["repository"] += repo_points
    reasons.extend(repo_reasons)
    signals["repository"].extend(repo_tags)

    # 6. Prompt length signals
    if len(prompt) > 1500:
        reasons.append("+1:long-prompt")
        score_breakdown["lexical"] += 1
    if len(prompt) > 5000:
        reasons.append("+1:very-long-prompt")
        score_breakdown["lexical"] += 1

    # 7. Routine reductions
    red_points, red_reasons, red_tags = evaluate_routine_reductions(text)
    score_breakdown["reductions"] -= red_points
    reasons.extend(red_reasons)
    signals["reductions"].extend(red_tags)

    raw_score = sum(score_breakdown.values())
    final_score = max(0, raw_score)

    routine_max = int(policy["thresholds"]["routine_max"])
    deep_min = int(policy["thresholds"]["deep_min"])

    # 8. Dominance & Capping rules
    if has_critical_concurrency:
        if final_score < deep_min:
            final_score = deep_min
            reasons.append("dominate-deep:critical-concurrency")

    # 9. Tier resolution
    if final_score <= routine_max:
        tier = "routine"
    elif final_score >= deep_min:
        tier = "deep"
    else:
        tier = "standard"

    return {
        "score": final_score,
        "tier": tier,
        "reasoning": policy["reasoning"][tier],
        "sandbox": sandbox,
        "mutating": mutating,
        "reasons": reasons,
        "risk_signals": signals,
        "score_breakdown": score_breakdown,
        "router_version": ROUTER_VERSION,
    }


def dump_model(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    return obj


def extract_models(payload: Any) -> list[dict[str, Any]]:
    payload = dump_model(payload)

    if isinstance(payload, list):
        return [
            item for item in payload
            if isinstance(item, dict)
        ]

    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    item for item in value
                    if isinstance(item, dict)
                ]

    return []


def cached_visible_models() -> list[dict[str, Any]]:
    """
    --route gibi local diagnostic islemlerinde yeni Codex
    baglantisi acmadan son model discovery sonucunu kullanir.

    Gercek turn execution her zaman codex.models() ile
    canli katalog kullanmaya devam eder.
    """
    try:
        if not MODEL_CACHE_FILE.exists():
            return []

        payload = json.loads(
            MODEL_CACHE_FILE.read_text(
                encoding="utf-8-sig"
            )
        )

        return extract_models(payload)

    except Exception:
        return []


def model_slug(model: dict[str, Any]) -> str | None:
    for key in ("slug", "model", "id", "name"):
        value = model.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def choose_model(
    tier: str,
    visible_models: list[dict[str, Any]],
    policy: dict[str, Any],
) -> str:
    visible = {
        slug
        for model in visible_models
        if (slug := model_slug(model))
    }

    for candidate in policy["models"][tier]:
        if candidate in visible:
            return candidate

    if visible:
        return sorted(visible)[0]

    raise RuntimeError("Codex model listesi boş.")


def sandbox_value(name: str) -> Sandbox:
    if name == "read-only":
        return Sandbox.read_only

    return Sandbox.workspace_write


def init_db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DB_FILE)

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cwd TEXT NOT NULL,
            thread_id TEXT,
            prompt_hash TEXT NOT NULL,
            prompt_chars INTEGER NOT NULL,
            route TEXT NOT NULL,
            score INTEGER NOT NULL,
            model TEXT NOT NULL,
            effort TEXT NOT NULL,
            sandbox TEXT NOT NULL,
            status TEXT,
            duration_ms INTEGER,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_output_tokens INTEGER,
            usage_json TEXT
        )
        """
    )

    db.commit()
    return db


def usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}

    if hasattr(usage, "model_dump"):
        return usage.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )

    if isinstance(usage, dict):
        return usage

    return {}


def value_from(
    payload: dict[str, Any],
    *names: str,
) -> int:
    """
    Read an integer token counter.

    ThreadTokenUsage has this shape:

        {
            "last": {...},
            "total": {...},
            "modelContextWindow": ...
        }

    Turn telemetry must use `last`, not cumulative `total`,
    otherwise resumed threads would double-count earlier turns.
    """
    if not isinstance(
        payload,
        dict,
    ):
        return 0

    # Backwards compatibility with any old flat usage payload.
    for name in names:
        value = payload.get(
            name
        )

        if (
            isinstance(value, int)
            and not isinstance(value, bool)
        ):
            return value

    last = payload.get(
        "last"
    )

    if isinstance(
        last,
        dict,
    ):
        for name in names:
            value = last.get(
                name
            )

            if (
                isinstance(value, int)
                and not isinstance(value, bool)
            ):
                return value

    return 0

def record_turn(
    db: sqlite3.Connection,
    *,
    cwd: Path,
    thread_id: str,
    prompt: str,
    route: dict[str, Any],
    model: str,
    result: Any,
) -> None:
    try:
        clean_prompt = normalize_external_text(prompt)
        prompt_hash = hashlib.sha256(
            clean_prompt.encode("utf-8")
        ).hexdigest()

        usage = usage_dict(getattr(result, "usage", None))

        status_obj = getattr(result, "status", None)

        status = (
            getattr(status_obj, "value", None)
            or str(status_obj or "")
        )

        db.execute(
            """
            INSERT INTO turns (
                timestamp,
                cwd,
                thread_id,
                prompt_hash,
                prompt_chars,
                route,
                score,
                model,
                effort,
                sandbox,
                status,
                duration_ms,
                input_tokens,
                cached_input_tokens,
                output_tokens,
                reasoning_output_tokens,
                usage_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(cwd),
                thread_id,
                prompt_hash,
                len(clean_prompt),
                route["tier"],
                route["score"],
                model,
                route["reasoning"],
                route["sandbox"],
                status,
                getattr(result, "duration_ms", None),
                value_from(
                    usage,
                    "inputTokens",
                    "input_tokens",
                ),
                value_from(
                    usage,
                    "cachedInputTokens",
                    "cached_input_tokens",
                ),
                value_from(
                    usage,
                    "outputTokens",
                    "output_tokens",
                ),
                value_from(
                    usage,
                    "reasoningOutputTokens",
                    "reasoning_output_tokens",
                ),
                json.dumps(
                    usage,
                    ensure_ascii=False,
                ),
            ),
        )

        db.commit()
    except (sqlite3.Error, UnicodeEncodeError, OSError, InvalidUnicodeInputError) as exc:
        log(
            f"TELEMETRY ERROR: {exc}"
        )


INFRA_FAILURE_RULES: list[str] = [
    r"\bpermission denied\b",
    r"\baccess denied\b",
    r"\beperm\b",
    r"\bsandbox\b",
    r"\brate limit\w*",
    r"\busage limit\w*",
    r"\bquota\b",
    r"\bauthentication\b",
    r"\bunauthorized\b",
    r"\bnot installed\b",
    r"\bcommand not found\b",
    r"\bis not recognized\b",
    r"\bdocker daemon\b",
    r"\bnetwork\b",
    r"\bdns\b",
    r"\blocked file\b",
    r"\bfile is locked\b",
    r"\bdubious repository ownership\b",
]


UNRESOLVED_RULES: list[str] = [
    r"\bcould not (complete|finish|resolve|fix|determine|identify)\b",
    r"\bcouldn't (complete|finish|resolve|fix|determine|identify)\b",
    r"\bunable to (complete|finish|resolve|fix|determine|identify)\b",
    r"\bwas not able to (complete|finish|resolve|fix)\b",
    r"\bstill (unresolved|failing|broken)\b",
    r"\bremains? unresolved\b",

    # Turkish after normalize()
    r"\btamamlayamadim\b",
    r"\bcozemedim\b",
    r"\bduzeltemedim\b",
    r"\btespit edemedim\b",
    r"\bhala cozulmedi\b",
    r"\bhala duzelmedi\b",
]


def enum_text(value: Any) -> str:
    if value is None:
        return ""

    raw = getattr(
        value,
        "value",
        value,
    )

    return str(raw).casefold()


def turn_error_text(result: Any) -> str:
    error = getattr(
        result,
        "error",
        None,
    )

    if error is None:
        return ""

    message = getattr(
        error,
        "message",
        "",
    )

    details = getattr(
        error,
        "additional_details",
        "",
    )

    return "\n".join(
        str(value)
        for value in (
            message,
            details,
        )
        if value
    )


def is_infrastructure_failure(text: str) -> bool:
    normalized = normalize(text)

    return regex_any(
        normalized,
        INFRA_FAILURE_RULES,
    )


def escalation_reason(
    result: Any,
    policy: dict[str, Any],
) -> str | None:
    """
    Return a capability-escalation reason only when another
    model can plausibly help.

    Environment/tool/sandbox failures deliberately do not
    trigger a stronger model.
    """
    config = policy.get(
        "escalation",
        {},
    )

    if not config.get(
        "enabled",
        False,
    ):
        return None

    status = enum_text(
        getattr(
            result,
            "status",
            None,
        )
    )

    final = (
        getattr(
            result,
            "final_response",
            None,
        )
        or ""
    ).strip()

    error_text = turn_error_text(
        result
    )

    evidence = "\n".join(
        value
        for value in (
            error_text,
            final,
        )
        if value
    )

    if is_infrastructure_failure(
        evidence
    ):
        return None

    if (
        config.get(
            "escalate_failed_turn",
            True,
        )
        and status.endswith("failed")
    ):
        return "turn-failed"

    if (
        config.get(
            "escalate_missing_final",
            True,
        )
        and not final
    ):
        return "missing-final-response"

    if (
        config.get(
            "escalate_explicit_unresolved",
            True,
        )
        and regex_any(
            normalize(final),
            UNRESOLVED_RULES,
        )
    ):
        return "explicit-unresolved"

    return None


def dict_value(
    payload: Any,
    *keys: str,
) -> Any:
    if not isinstance(
        payload,
        dict,
    ):
        return None

    for key in keys:
        if key in payload:
            return payload[key]

    return None


def quota_budget_state(
    used_percent: float | None,
    *,
    reached_type: Any,
    spend_control_reached: Any,
    policy: dict[str, Any],
) -> str:
    config = policy.get(
        "budget",
        {},
    )

    if not config.get(
        "enabled",
        False,
    ):
        return "normal"

    reached_text = str(
        reached_type
        or ""
    ).strip().casefold()

    reached = (
        reached_text
        not in {
            "",
            "none",
            "null",
        }
    )

    if (
        reached
        or spend_control_reached is True
    ):
        return "reached"

    if used_percent is None:
        return "unknown"

    thresholds = config.get(
        "thresholds",
        {},
    )

    hard_stop = float(
        thresholds.get(
            "hard_stop_at",
            100,
        )
    )

    emergency = float(
        thresholds.get(
            "emergency_at",
            95,
        )
    )

    critical = float(
        thresholds.get(
            "critical_at",
            85,
        )
    )

    conserve = float(
        thresholds.get(
            "conserve_at",
            70,
        )
    )

    if used_percent >= hard_stop:
        return "reached"

    if used_percent >= emergency:
        return "emergency"

    if used_percent >= critical:
        return "critical"

    if used_percent >= conserve:
        return "conserve"

    return "normal"


def read_quota_snapshot(
    codex: Codex,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """
    Read current account quota without starting a model turn.

    Prefer the explicit `codex` limit bucket when the backend
    supplies it, otherwise use the backwards-compatible
    top-level rateLimits snapshot.
    """
    try:
        response = (
            codex._client._request_raw(
                "account/rateLimits/read",
                {},
            )
        )

    except Exception as exc:
        return {
            "available": False,
            "state": "unknown",
            "error": repr(exc),
        }

    buckets = dict_value(
        response,
        "rateLimitsByLimitId",
        "rate_limits_by_limit_id",
    )

    bucket = None

    if isinstance(
        buckets,
        dict,
    ):
        candidate = buckets.get(
            "codex"
        )

        if isinstance(
            candidate,
            dict,
        ):
            bucket = candidate

    if bucket is None:
        bucket = dict_value(
            response,
            "rateLimits",
            "rate_limits",
        )

    if not isinstance(
        bucket,
        dict,
    ):
        return {
            "available": False,
            "state": "unknown",
            "error": "No Codex rate-limit bucket.",
        }

    windows = []

    for window_name in (
        "primary",
        "secondary",
    ):
        window = bucket.get(
            window_name
        )

        if not isinstance(
            window,
            dict,
        ):
            continue

        used = dict_value(
            window,
            "usedPercent",
            "used_percent",
        )

        resets = dict_value(
            window,
            "resetsAt",
            "resets_at",
        )

        duration = dict_value(
            window,
            "windowDurationMins",
            "window_duration_mins",
        )

        if isinstance(
            used,
            (int, float),
        ):
            windows.append({
                "name": window_name,
                "usedPercent": float(used),
                "resetsAt": resets,
                "windowDurationMins": duration,
            })

    dominant = None

    if windows:
        dominant = max(
            windows,
            key=lambda item: item[
                "usedPercent"
            ],
        )

    used_percent = (
        dominant[
            "usedPercent"
        ]
        if dominant
        else None
    )

    reached_type = dict_value(
        bucket,
        "rateLimitReachedType",
        "rate_limit_reached_type",
    )

    spend_control = dict_value(
        response,
        "spendControlReached",
        "spend_control_reached",
    )

    if spend_control is None:
        spend_control = dict_value(
            bucket,
            "spendControlReached",
            "spend_control_reached",
        )

    state = quota_budget_state(
        used_percent,
        reached_type=reached_type,
        spend_control_reached=spend_control,
        policy=policy,
    )

    remaining = None

    if used_percent is not None:
        remaining = max(
            0.0,
            100.0 - used_percent,
        )

    snapshot = {
        "available": True,
        "state": state,
        "limitId": dict_value(
            bucket,
            "limitId",
            "limit_id",
        ),
        "planType": dict_value(
            bucket,
            "planType",
            "plan_type",
        ),
        "usedPercent": used_percent,
        "remainingPercent": remaining,
        "reachedType": reached_type,
        "spendControlReached": spend_control,
        "dominantWindow": dominant,
        "capturedAt": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    try:
        QUOTA_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        QUOTA_FILE.write_text(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    except Exception:
        pass

    return snapshot


def quota_reset_text(
    quota: dict[str, Any],
) -> str:
    window = quota.get(
        "dominantWindow"
    )

    if not isinstance(
        window,
        dict,
    ):
        return "-"

    resets = window.get(
        "resetsAt"
    )

    if not isinstance(
        resets,
        (int, float),
    ):
        return "-"

    try:
        return (
            datetime.fromtimestamp(
                resets,
                tz=timezone.utc,
            )
            .astimezone()
            .isoformat(
                timespec="minutes"
            )
        )

    except Exception:
        return str(resets)


def print_quota(
    quota: dict[str, Any],
) -> None:
    if not quota.get(
        "available"
    ):
        print(
            "[cx] quota=UNKNOWN | "
            "budget guard fail-open"
        )
        return

    state = str(
        quota.get(
            "state",
            "unknown",
        )
    ).upper()

    used = quota.get(
        "usedPercent"
    )

    remaining = quota.get(
        "remainingPercent"
    )

    used_text = (
        f"{used:.1f}%"
        if isinstance(
            used,
            (int, float),
        )
        else "?"
    )

    remaining_text = (
        f"{remaining:.1f}%"
        if isinstance(
            remaining,
            (int, float),
        )
        else "?"
    )

    print(
        f"[cx] quota={used_text} used | "
        f"{remaining_text} left | "
        f"budget={state} | "
        f"reset={quota_reset_text(quota)}"
    )


def budget_guard_chain(
    start_tier: str,
    base_chain: list[str],
    quota: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    state = str(
        quota.get(
            "state",
            "unknown",
        )
    )

    if state in {
        "unknown",
        "reached",
    }:
        return list(
            base_chain
        )

    config = policy.get(
        "budget",
        {},
    )

    state_chains = (
        config.get(
            "chains",
            {},
        )
        .get(
            state,
            {},
        )
    )

    configured = state_chains.get(
        start_tier
    )

    if not isinstance(
        configured,
        list,
    ):
        return list(
            base_chain
        )

    allowed = set(
        base_chain
    )

    result = [
        str(tier)
        for tier in configured
        if str(tier) in allowed
    ]

    if not result:
        return [
            base_chain[0]
        ]

    return result


def escalation_chain(
    start_tier: str,
    policy: dict[str, Any],
) -> list[str]:
    config = policy.get(
        "escalation",
        {},
    )

    chains = config.get(
        "chains",
        {},
    )

    chain = chains.get(
        start_tier,
        [start_tier],
    )

    if not isinstance(
        chain,
        list,
    ):
        return [start_tier]

    allowed = {
        "routine",
        "standard",
        "deep",
    }

    result = [
        str(tier)
        for tier in chain
        if str(tier) in allowed
    ]

    if not result:
        result = [
            start_tier
        ]

    max_attempts = int(
        config.get(
            "max_attempts",
            3,
        )
    )

    return result[
        :max(
            1,
            max_attempts,
        )
    ]


def escalation_prompt(
    reason: str,
) -> str:
    """
    The same Thread already contains the original task and previous
    attempt, so do not resend the user's potentially huge prompt.
    """
    return (
        "Continue the same task from the previous turn. "
        f"The previous attempt was incomplete ({reason}). "
        "Re-evaluate the existing work, preserve valid changes, "
        "finish the task, and perform the narrowest relevant "
        "verification. Do not restart from scratch unless necessary."
    )


def init_escalation_table(
    db: sqlite3.Connection,
) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS escalation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            from_tier TEXT NOT NULL,
            to_tier TEXT NOT NULL,
            from_model TEXT NOT NULL,
            to_model TEXT NOT NULL
        )
        """
    )

    db.commit()


def record_escalation(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    reason: str,
    from_tier: str,
    to_tier: str,
    from_model: str,
    to_model: str,
) -> None:
    init_escalation_table(
        db
    )

    db.execute(
        """
        INSERT INTO escalation_events (
            timestamp,
            thread_id,
            reason,
            from_tier,
            to_tier,
            from_model,
            to_model
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(
                timezone.utc
            ).isoformat(),
            thread_id,
            reason,
            from_tier,
            to_tier,
            from_model,
            to_model,
        ),
    )

    db.commit()


def print_route(
    route: dict[str, Any],
    model: str | None = None,
) -> None:
    selected = model or "<model discovery bekleniyor>"

    print(
        f"[cx] {route['tier']} | "
        f"score={route['score']} | "
        f"{selected} | "
        f"{route['reasoning']} | "
        f"{route['sandbox']}"
    )


def print_stats() -> None:
    db = init_db()

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

    print("=== CX STATS ===")
    print(f"Turns            : {row[0]}")
    print(f"Input tokens     : {row[1]}")
    print(f"Cached input     : {row[2]}")
    print(f"Output tokens    : {row[3]}")
    print(f"Reasoning output : {row[4]}")
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
        print(f"{route:10} : {count}")

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

    db.close()


def configured_shim_names() -> list[str]:
    try:
        payload = json.loads(
            SHIM_CONFIG_FILE.read_text(
                encoding="utf-8-sig"
            )
        )

        tools = payload.get(
            "tools",
            {},
        )

        if isinstance(tools, dict):
            return sorted(
                str(name)
                for name in tools
            )

    except Exception:
        pass

    return []


def doctor() -> int:
    print("=== CX DOCTOR ===")
    print(f"Router version : {ROUTER_VERSION}")
    print(f"CX_HOME        : {CX_HOME}")
    print(f"Policy         : {POLICY_FILE}")
    print(f"Usage DB       : {DB_FILE}")
    print(f"Python         : {sys.executable}")
    print(f"CWD            : {Path.cwd()}")
    print("Runtime mode   : LEAN")
    print("Plugins        : OFF (CX only)")
    print("Apps prompt    : OFF (CX only)")
    print("Collab prompt  : OFF (CX only)")
    print("Multi-agent    : OFF (CX only)")
    print("Context7 MCP   : OFF (CX only)")
    print("Filesystem MCP : OFF (CX only)")
    print("Node REPL MCP  : OFF (CX only)")
    print("Verbosity      : LOW")
    print("Escalation     : DENY_ALL")
    print("RTK enforcement: PATH SHIM (CX only)")
    print("RTK telemetry  : REMOTE OFF (CX only)")
    print("Auto escalation: ON")
    print("Budget guard    : ON (70/85/95/100)")
    print("Session resume  : ON (45 min, same branch)")
    print("Compaction      : NATIVE CODEX")
    print("Token telemetry : NESTED SDK USAGE")
    print("Write negation  : AWARE")
    print("CCE runtime     : EXPERIMENTAL / POLICY-GATED")

    shim_names = configured_shim_names()

    print(
        "RTK shims      :",
        ", ".join(shim_names)
        if shim_names
        else "NONE",
    )

    try:
        sys_path_runtime = str(Path(__file__).resolve().parent.parent / "runtime" / "cx2")
        if sys_path_runtime not in sys.path:
            sys.path.insert(0, sys_path_runtime)
        from codex_compat import generate_doctor_compatibility_summary
        compat_summary = generate_doctor_compatibility_summary()
        print(f"Codex package  : {compat_summary['codex_package']}")
        print(f"Codex CLI      : {compat_summary['codex_cli_version']}")
        print(f"Validated base : {compat_summary['validated_baseline']}")
        print(f"Core compat    : {compat_summary['core_compatibility']}")
        print(f"Native delete  : {compat_summary['native_delete']}")
    except Exception:
        pass

    print()

    with create_codex() as codex:
        account = codex.account()
        print(
            "Auth           :",
            "OK"
            if getattr(account, "account", None) is not None
            else "YOK",
        )

        models = extract_models(codex.models())

        print(f"Visible models : {len(models)}")

        for model in models:
            slug = model_slug(model)
            if slug:
                print(f"  - {slug}")

    return 0


def canonical_repo_path(
    path: str | Path | None,
) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(
            os.path.normpath(
                os.path.abspath(
                    str(path)
                )
            )
        ).rstrip("\\/")
    except Exception:
        return str(path).lower().replace("/", "\\").rstrip("\\")


def _normalize_branch(
    value: str | None,
) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if s in ("HEAD", "DETACHED"):
        return "DETACHED"
    return s


def current_repo_branch(
    repo: dict[str, Any],
) -> str | None:
    if not repo.get("git"):
        return None

    root = Path(
        str(repo["root"])
    )

    branch = run_local(
        [
            "git",
            "symbolic-ref",
            "--short",
            "-q",
            "HEAD",
        ],
        root,
    )

    if branch and branch.strip():
        return branch.strip()

    abbrev = run_local(
        [
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ],
        root,
    )

    if abbrev and abbrev.strip():
        if abbrev.strip() == "HEAD":
            return "DETACHED"
        return abbrev.strip()

    return "DETACHED"


def repo_session_key(
    repo: dict[str, Any],
) -> str:
    root = canonical_repo_path(
        repo.get("root")
    )

    return hashlib.sha256(
        root.encode(
            "utf-8",
            errors="replace",
        )
    ).hexdigest()


def init_session_table(
    db: sqlite3.Connection,
) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            repo_key TEXT PRIMARY KEY,
            repo_root TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            branch TEXT,
            last_used_at TEXT NOT NULL,
            user_turns INTEGER NOT NULL DEFAULT 0,
            context_tokens INTEGER,
            context_window INTEGER,
            context_percent REAL
        )
        """
    )

    db.commit()


def parse_utc_datetime(
    value: str,
) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(
            value
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def session_age_minutes(
    session: dict[str, Any],
    now: datetime | None = None,
) -> float | None:
    last_used = parse_utc_datetime(
        str(
            session.get(
                "last_used_at",
                "",
            )
        )
    )

    if last_used is None:
        return None

    current = (
        now
        if now is not None
        else datetime.now(
            timezone.utc
        )
    )

    return max(
        0.0,
        (
            current
            - last_used
        ).total_seconds()
        / 60.0,
    )


def load_repo_session(
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> dict[str, Any] | None:
    if not repo.get("git"):
        return None

    init_session_table(
        db
    )

    row = db.execute(
        """
        SELECT
            repo_key,
            repo_root,
            thread_id,
            branch,
            last_used_at,
            user_turns,
            context_tokens,
            context_window,
            context_percent
        FROM sessions
        WHERE repo_key = ?
        """,
        (
            repo_session_key(repo),
        ),
    ).fetchone()

    if row is None:
        return None

    keys = (
        "repo_key",
        "repo_root",
        "thread_id",
        "branch",
        "last_used_at",
        "user_turns",
        "context_tokens",
        "context_window",
        "context_percent",
    )

    return dict(
        zip(
            keys,
            row,
        )
    )


def session_reusable(
    session: dict[str, Any] | None,
    repo: dict[str, Any],
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    config = policy.get(
        "session",
        {},
    )

    if not config.get(
        "enabled",
        False,
    ):
        return False, "disabled"

    if not repo.get("git"):
        return False, "not-git"

    if not session:
        return False, "no-session"

    age = session_age_minutes(
        session,
        now=now,
    )

    if age is None:
        return False, "invalid-age"

    ttl = float(
        config.get(
            "resume_ttl_minutes",
            45,
        )
    )

    if age > ttl:
        return False, "expired"

    if not config.get(
        "resume_across_branch_change",
        False,
    ):
        current_branch = _normalize_branch(
            current_repo_branch(repo)
        )

        saved_branch = _normalize_branch(
            session.get(
                "branch"
            )
        )

        if (
            current_branch
            and saved_branch
            and current_branch
            != saved_branch
        ):
            return False, "branch-changed"

    return True, "recent"


def clear_repo_session(
    db: sqlite3.Connection,
    repo: dict[str, Any],
) -> None:
    if not repo.get("git"):
        return

    init_session_table(
        db
    )

    db.execute(
        """
        DELETE FROM sessions
        WHERE repo_key = ?
        """,
        (
            repo_session_key(repo),
        ),
    )

    db.commit()


def usage_context_info(
    result: Any,
) -> dict[str, Any]:
    usage = usage_dict(
        getattr(
            result,
            "usage",
            None,
        )
    )

    last = dict_value(
        usage,
        "last",
    )

    if not isinstance(
        last,
        dict,
    ):
        last = {}

    tokens = dict_value(
        last,
        "totalTokens",
        "total_tokens",
    )

    window = dict_value(
        usage,
        "modelContextWindow",
        "model_context_window",
    )

    percent = None

    if (
        isinstance(
            tokens,
            (int, float),
        )
        and isinstance(
            window,
            (int, float),
        )
        and window > 0
    ):
        percent = (
            float(tokens)
            / float(window)
            * 100.0
        )

    return {
        "tokens": (
            int(tokens)
            if isinstance(
                tokens,
                (int, float),
            )
            else None
        ),
        "window": (
            int(window)
            if isinstance(
                window,
                (int, float),
            )
            else None
        ),
        "percent": percent,
    }


def print_context_info(
    info: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    percent = info.get(
        "percent"
    )

    tokens = info.get(
        "tokens"
    )

    window = info.get(
        "window"
    )

    if not isinstance(
        percent,
        (int, float),
    ):
        return

    print(
        f"[cx] context={percent:.1f}% "
        f"({tokens}/{window}) | "
        "compaction=native"
    )

    warn = float(
        policy.get(
            "session",
            {},
        ).get(
            "context_warn_percent",
            75,
        )
    )

    if percent >= warn:
        print(
            "[cx] Context is getting large; "
            "native Codex compaction remains enabled."
        )


def save_repo_session(
    db: sqlite3.Connection,
    repo: dict[str, Any],
    thread_id: str,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    if not repo.get("git"):
        return

    init_session_table(
        db
    )

    existing = load_repo_session(
        db,
        repo,
    )

    user_turns = (
        int(
            existing.get(
                "user_turns",
                0,
            )
        )
        + 1
        if existing
        else 1
    )

    context = (
        context
        if isinstance(
            context,
            dict,
        )
        else {}
    )

    db.execute(
        """
        INSERT INTO sessions (
            repo_key,
            repo_root,
            thread_id,
            branch,
            last_used_at,
            user_turns,
            context_tokens,
            context_window,
            context_percent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(repo_key)
        DO UPDATE SET
            repo_root = excluded.repo_root,
            thread_id = excluded.thread_id,
            branch = excluded.branch,
            last_used_at = excluded.last_used_at,
            user_turns = excluded.user_turns,
            context_tokens = excluded.context_tokens,
            context_window = excluded.context_window,
            context_percent = excluded.context_percent
        """,
        (
            repo_session_key(repo),
            str(repo["root"]),
            thread_id,
            current_repo_branch(
                repo
            ),
            datetime.now(
                timezone.utc
            ).isoformat(),
            user_turns,
            context.get(
                "tokens"
            ),
            context.get(
                "window"
            ),
            context.get(
                "percent"
            ),
        ),
    )

    db.commit()


def resume_repo_thread(
    codex: Codex,
    db: sqlite3.Connection,
    repo: dict[str, Any],
    cwd: Path,
    policy: dict[str, Any],
    *,
    model: str,
    sandbox: Sandbox,
) -> Any | None:
    session = load_repo_session(
        db,
        repo,
    )

    reusable, reason = (
        session_reusable(
            session,
            repo,
            policy,
        )
    )

    if not reusable:
        if session:
            age = session_age_minutes(
                session
            )

            age_text = (
                f"{age:.0f}m"
                if isinstance(
                    age,
                    (int, float),
                )
                else "?"
            )

            print(
                f"[cx] session=NEW "
                f"({reason}, age={age_text})"
            )

        return None

    thread_id = str(
        session["thread_id"]
    )

    age = session_age_minutes(
        session
    )

    try:
        thread = codex.thread_resume(
            thread_id,
            approval_mode=ApprovalMode.deny_all,
            cwd=str(cwd),
            model=model,
            sandbox=sandbox,
        )

    except Exception as exc:
        log(
            "SESSION RESUME ERROR "
            f"thread={thread_id} "
            f"error={exc!r}"
        )

        clear_repo_session(
            db,
            repo,
        )

        print(
            "[cx] session=NEW "
            "(stored thread unavailable)"
        )

        return None

    age_text = (
        f"{age:.0f}m"
        if isinstance(
            age,
            (int, float),
        )
        else "?"
    )

    print(
        f"[cx] session=RESUME | "
        f"age={age_text} | "
        f"thread={thread_id}"
    )

    previous_percent = session.get(
        "context_percent"
    )

    if isinstance(
        previous_percent,
        (int, float),
    ):
        print(
            f"[cx] previous context="
            f"{previous_percent:.1f}%"
        )

    return thread


def execute_prompt(
    codex: Codex,
    thread: Any,
    prompt: str,
    cwd: Path,
    repo: dict[str, Any],
    policy: dict[str, Any],
    visible_models: list[dict[str, Any]],
    db: sqlite3.Connection,
) -> Any:
    base_route = classify(
        prompt,
        repo,
        policy,
    )

    base_tiers = escalation_chain(
        base_route["tier"],
        policy,
    )

    quota = read_quota_snapshot(
        codex,
        policy,
    )

    print_quota(
        quota
    )

    if quota.get("state") == "reached":
        print()
        print(
            "[cx] BLOCKED: Codex quota/spend limit reached. "
            "No model turn was started."
        )
        print()

        return thread

    tiers = budget_guard_chain(
        base_route["tier"],
        base_tiers,
        quota,
        policy,
    )

    if tiers != base_tiers:
        print(
            "[cx] Budget guard escalation chain: "
            + " -> ".join(tiers)
        )

    sandbox = sandbox_value(
        base_route["sandbox"]
    )

    if thread is None:
        first_model = choose_model(
            tiers[0],
            visible_models,
            policy,
        )

        thread = resume_repo_thread(
            codex,
            db,
            repo,
            cwd,
            policy,
            model=first_model,
            sandbox=sandbox,
        )

        if thread is None:
            thread = codex.thread_start(
                cwd=str(cwd),
                model=first_model,
                sandbox=sandbox,
                approval_mode=ApprovalMode.deny_all,
            )

            print(
                f"[cx] session=NEW | "
                f"thread={thread.id}"
            )

    attempt_input = prompt

    previous_tier = None
    previous_model = None
    previous_reason = None

    final_result = None

    for attempt_index, tier in enumerate(
        tiers,
        start=1,
    ):
        attempt_route = dict(
            base_route
        )

        attempt_route["tier"] = tier
        attempt_route["reasoning"] = (
            policy["reasoning"][tier]
        )

        model = choose_model(
            tier,
            visible_models,
            policy,
        )

        if (
            previous_tier is not None
            and previous_model is not None
            and previous_reason is not None
        ):
            record_escalation(
                db,
                thread_id=thread.id,
                reason=previous_reason,
                from_tier=previous_tier,
                to_tier=tier,
                from_model=previous_model,
                to_model=model,
            )

            print(
                f"[cx] ESCALATE "
                f"{previous_model} -> {model} "
                f"({previous_reason})"
            )

        print_route(
            attempt_route,
            model,
        )

        started = time.perf_counter()

        try:
            result = thread.run(
                attempt_input,
                cwd=str(cwd),
                model=model,
                effort=attempt_route[
                    "reasoning"
                ],
                sandbox=sandbox,
                approval_mode=ApprovalMode.deny_all,
            )

        except Exception as exc:
            # Transport, auth, overload and SDK errors are not evidence
            # that a more capable model is required.
            log(
                f"TURN ERROR "
                f"model={model} "
                f"route={tier} "
                f"attempt={attempt_index} "
                f"error={exc!r}"
            )
            raise

        elapsed_ms = int(
            (
                time.perf_counter()
                - started
            )
            * 1000
        )

        if getattr(
            result,
            "duration_ms",
            None,
        ) is None:
            try:
                result.duration_ms = (
                    elapsed_ms
                )
            except Exception:
                pass

        record_turn(
            db,
            cwd=cwd,
            thread_id=thread.id,
            prompt=attempt_input,
            route=attempt_route,
            model=model,
            result=result,
        )

        final_result = result

        reason = escalation_reason(
            result,
            policy,
        )

        is_last = (
            attempt_index
            >= len(tiers)
        )

        if (
            reason is None
            or is_last
        ):
            break

        previous_tier = tier
        previous_model = model
        previous_reason = reason

        attempt_input = escalation_prompt(
            reason
        )

    if final_result is not None:
        context_info = usage_context_info(
            final_result
        )

        save_repo_session(
            db,
            repo,
            thread.id,
            context=context_info,
        )

        print_context_info(
            context_info,
            policy,
        )

    print()

    if (
        final_result is not None
        and final_result.final_response
    ):
        print(
            final_result.final_response
        )
    else:
        print(
            "[cx] Turn tamamlandi; "
            "final_response bos."
        )

    print()

    return thread

def prepare_cce_for_prompt(
    prompt: str,
    repo: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[
    dict[str, Any],
    bool,
    str,
    str,
]:
    """
    Resolve CCE usage before starting the Codex App Server.

    CCE failures are fail-open: the model turn can continue
    with the normal LEAN CX runtime.
    """
    route = classify(
        prompt,
        repo,
        policy,
    )

    cce_enabled, cce_reason = (
        should_use_cce(
            prompt,
            repo,
            route,
            policy,
        )
    )

    index_reason = "not-needed"

    if cce_enabled:
        auto_index = bool(
            policy.get(
                "cce",
                {},
            ).get(
                "auto_index",
                False,
            )
        )

        if auto_index:
            index_ok, index_reason = (
                ensure_cce_index(
                    repo,
                    policy,
                )
            )

            if not index_ok:
                log(
                    "CCE FALLBACK "
                    f"reason={cce_reason} "
                    f"index={index_reason} "
                    f"repo={repo.get('root')}"
                )

                cce_enabled = False

                cce_reason = (
                    f"{cce_reason};"
                    f"fallback:{index_reason}"
                )
        else:
            # Production turns never wait for repository indexing.
            #
            # CCE may use a previously built external index. Index
            # bootstrap/freshness is handled outside the foreground
            # model-turn path.
            index_reason = "prebuilt"

    return (
        route,
        cce_enabled,
        cce_reason,
        index_reason,
    )


def execute_prompt_managed(
    prompt: str,
    cwd: Path,
    repo: dict[str, Any],
    policy: dict[str, Any],
    db: sqlite3.Connection,
) -> Any:
    """
    Execute one CX prompt with a process-local runtime chosen
    before App Server startup.

    A new App Server process is created for the selected runtime.
    Repository session persistence handles thread resume across
    successive CLI/interactive turns.
    """
    (
        route,
        cce_enabled,
        cce_reason,
        index_reason,
    ) = prepare_cce_for_prompt(
        prompt,
        repo,
        policy,
    )

    print(
        "[cx] CCE="
        + (
            "ON"
            if cce_enabled
            else "OFF"
        )
        + f" | reason={cce_reason}"
        + f" | index={index_reason}"
    )

    repo_root = Path(
        str(
            repo["root"]
        )
    ).resolve()

    with create_codex(
        cce_enabled=cce_enabled,
        repo_root=(
            repo_root
            if cce_enabled
            else None
        ),
        policy=policy,
    ) as codex:

        visible_models = extract_models(
            codex.models()
        )

        return execute_prompt(
            codex,
            None,
            prompt,
            cwd,
            repo,
            policy,
            visible_models,
            db,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cx",
        description="Automatic Codex router",
    )

    parser.add_argument(
        "prompt",
        nargs="*",
        help="Codex görevi",
    )

    parser.add_argument(
        "--doctor",
        action="store_true",
        help="CX runtime ve model discovery kontrolü",
    )

    parser.add_argument(
        "--route",
        metavar="TEXT",
        help="Sadece lokal routing sonucunu göster; Codex turn başlatmaz",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Yerel token telemetri özetini göster",
    )

    parser.add_argument(
        "--quota",
        action="store_true",
        help="Codex kota durumunu model turn başlatmadan göster",
    )

    parser.add_argument(
        "--session",
        action="store_true",
        help="Aktif repo session bilgisini lokal olarak göster",
    )

    args = parser.parse_args()

    policy = load_policy()
    cwd = Path.cwd().resolve()
    repo = detect_repo(cwd)

    if args.doctor:
        return doctor()

    if args.stats:
        print_stats()
        return 0

    if args.quota:
        with create_codex() as codex:
            quota = read_quota_snapshot(
                codex,
                policy,
            )

        print("=== CX QUOTA ===")
        print_quota(quota)

        return 0

    if args.session:
        db = init_db()

        try:
            session = load_repo_session(
                db,
                repo,
            )

            print("=== CX SESSION ===")
            print(f"Repo     : {repo['root']}")

            if not repo.get("git"):
                print("Status   : disabled (not a git repo)")
                return 0

            if not session:
                print("Status   : none")
                return 0

            reusable, reason = session_reusable(
                session,
                repo,
                policy,
            )

            print(
                "Status   :",
                "reusable"
                if reusable
                else "stale",
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

            age = session_age_minutes(
                session
            )

            print(
                "Age      :",
                f"{age:.1f} min"
                if isinstance(
                    age,
                    (int, float),
                )
                else "?",
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

            return 0

        finally:
            db.close()

    if args.route is not None:
        route = classify(
            args.route,
            repo,
            policy,
        )

        print("=== CX LOCAL ROUTE ===")
        print(f"CWD      : {cwd}")
        print(f"Git      : {repo['git']}")
        print(f"Root     : {repo['root']}")
        print(
            "Stacks   :",
            ", ".join(repo["stacks"])
            if repo["stacks"]
            else "-",
        )
        print(f"Monorepo : {repo['monorepo']}")
        print(f"Dirty    : {repo['dirty_files']}")
        cached_models = cached_visible_models()

        selected_model = None

        if cached_models:
            selected_model = choose_model(
                route["tier"],
                cached_models,
                policy,
            )

        print()
        print_route(
            route,
            selected_model,
        )

        cce_enabled, cce_reason = (
            should_use_cce(
                args.route,
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

        return 0

    one_shot_prompt = " ".join(args.prompt).strip()

    db = init_db()

    cce_policy_enabled = bool(
        policy.get(
            "cce",
            {},
        ).get(
            "enabled",
            False,
        )
    )

    # =========================================================
    # Interactive command helpers
    # =========================================================

    def print_interactive_help() -> None:
        print("=== CX COMMANDS ===")
        print()
        print("/help")
        print("  Bu yardımı göster.")
        print()
        print("/quota")
        print("  Canlı Codex kota durumunu göster.")
        print()
        print("/stats")
        print("  Yerel token telemetri özetini göster.")
        print()
        print("/session")
        print("  Aktif repo session bilgisini göster.")
        print()
        print("/route <görev>")
        print("  Model turn başlatmadan routing sonucunu göster.")
        print()
        print("/doctor")
        print("  CX runtime sağlık kontrolünü çalıştır.")
        print()
        print("/clear")
        print("  Terminal ekranını temizle. /cls alias'ı da vardır.")
        print()
        print("/new")
        print("  Persisted repo session/thread bağlantısını sıfırla.")
        print()
        print("/exit")
        print("  CX interaktif moddan çık.")
        print()
        print(
            "--quota, --stats, --session, --doctor, "
            "--route de alias olarak desteklenir."
        )

    def print_interactive_session() -> None:
        session = load_repo_session(
            db,
            repo,
        )

        print("=== CX SESSION ===")
        print(
            f"Repo     : {repo['root']}"
        )

        if not repo.get(
            "git"
        ):
            print(
                "Status   : disabled (not a git repo)"
            )
            return

        if not session:
            print(
                "Status   : none"
            )
            return

        reusable, reason = (
            session_reusable(
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

        age = session_age_minutes(
            session
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

    def print_interactive_route(
        route_prompt: str,
    ) -> None:

        route_prompt = (
            route_prompt.strip()
        )

        if not route_prompt:
            print(
                "Kullanım: /route <görev>"
            )
            return

        route = classify(
            route_prompt,
            repo,
            policy,
        )

        cached_models = (
            cached_visible_models()
        )

        selected_model = None

        if cached_models:
            selected_model = (
                choose_model(
                    route["tier"],
                    cached_models,
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

        print_route(
            route,
            selected_model,
        )

        cce_enabled, cce_reason = (
            should_use_cce(
                route_prompt,
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

        print()
        print(
            "[cx] /route sadece ÖNİZLEME yapar; "
            "model turnü başlatmaz."
        )
        print(
            "[cx] Görevi çalıştırmak için "
            "komutsuz olarak yaz."
        )

    def handle_interactive_command(
        prompt: str,
        runtime_codex=None,
    ) -> tuple[bool, bool]:
        """
        Returns:
            handled, should_exit
        """
        value = prompt.strip()
        folded = value.casefold()

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
            "/stats",
            "--stats",
        }:
            print_stats()
            return True, False

        if folded in {
            "/quota",
            "--quota",
        }:
            if runtime_codex is not None:
                quota = read_quota_snapshot(
                    runtime_codex,
                    policy,
                )
            else:
                with create_codex() as quota_codex:
                    quota = read_quota_snapshot(
                        quota_codex,
                        policy,
                    )

            print(
                "=== CX QUOTA ==="
            )

            print_quota(
                quota
            )

            return True, False

        if folded in {
            "/session",
            "--session",
        }:
            print_interactive_session()
            return True, False

        if folded in {
            "/doctor",
            "--doctor",
        }:
            doctor()
            return True, False

        if folded in {
            "/clear",
            "/cls",
        }:
            import os

            os.system(
                "cls"
                if os.name == "nt"
                else "clear"
            )

            return True, False

        if folded in {
            "/new",
            "--new",
        }:
            clear_repo_session(
                db,
                repo,
            )

            print(
                "[cx] Persisted session cleared. "
                "Yeni thread bir sonraki turn'de başlayacak."
            )

            return True, False

        route_prefixes = (
            "/route",
            "--route",
        )

        for prefix in route_prefixes:
            if folded == prefix:
                print(
                    "Kullanım: "
                    f"{prefix} <görev>"
                )
                return True, False

            if folded.startswith(
                prefix + " "
            ):
                route_prompt = (
                    value[
                        len(prefix):
                    ]
                    .strip()
                )

                print_interactive_route(
                    route_prompt
                )

                return True, False

        # Slash/double-dash input is treated as a CX command,
        # never silently forwarded to the coding model.
        if (
            value.startswith("/")
            or value.startswith("--")
        ):
            print(
                f"[cx] Bilinmeyen komut: {value}"
            )

            print(
                "[cx] Komutlar için /help"
            )

            return True, False

        return False, False

    def interactive_loop(
        run_turn,
        runtime_codex=None,
    ) -> int:
        """
        Shared interactive shell.

        LEAN production mode keeps one Codex App Server alive
        for the entire terminal session.
        """
        print(
            "=== CX ==="
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

        while True:
            try:
                prompt = input(
                    "cx> "
                ).strip()

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
                    runtime_codex,
                )
            )

            if should_exit:
                break

            if handled:
                continue

            run_turn(
                prompt
            )

        return 0

    try:
        # =====================================================
        # One-shot
        # =====================================================

        if one_shot_prompt:
            if cce_policy_enabled:
                execute_prompt_managed(
                    one_shot_prompt,
                    cwd,
                    repo,
                    policy,
                    db,
                )

            else:
                with create_codex() as codex:
                    visible_models = (
                        extract_models(
                            codex.models()
                        )
                    )

                    print(
                        "[cx] CCE=OFF | "
                        "reason=disabled | "
                        "index=not-needed"
                    )

                    execute_prompt(
                        codex,
                        None,
                        one_shot_prompt,
                        cwd,
                        repo,
                        policy,
                        visible_models,
                        db,
                    )

            return 0

        # =====================================================
        # Interactive
        # =====================================================

        if cce_policy_enabled:
            def run_managed_turn(
                prompt: str,
            ) -> None:

                execute_prompt_managed(
                    prompt,
                    cwd,
                    repo,
                    policy,
                    db,
                )

            return interactive_loop(
                run_managed_turn,
                None,
            )

        # Production LEAN:
        # one App Server for the whole interactive session.
        with create_codex() as codex:
            visible_models = (
                extract_models(
                    codex.models()
                )
            )

            def run_lean_turn(
                prompt: str,
            ) -> None:

                print(
                    "[cx] CCE=OFF | "
                    "reason=disabled | "
                    "index=not-needed"
                )

                execute_prompt(
                    codex,
                    None,
                    prompt,
                    cwd,
                    repo,
                    policy,
                    visible_models,
                    db,
                )

            return interactive_loop(
                run_lean_turn,
                codex,
            )

    finally:
        db.close()



if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL {exc!r}")
        print(
            f"[cx] HATA: {exc}",
            file=sys.stderr,
        )
        raise
