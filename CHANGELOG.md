# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.6] - 2026-08-20

### Added
- **Centralized Codex Compatibility Abstraction**: Consolidated Codex version parsing, capability resolution, and safe-degradation logic into `runtime/cx2/codex_compat.py`.
- **Validated Codex Baseline**: Established pinned `0.144.4` baseline (`openai-codex==0.144.4` and `openai-codex-cli-bin==0.144.4`) with strict `PACKAGE_VERSION_MISMATCH` detection and graceful `UNVERIFIED` state for newer runtime versions.
- **Safe Native Delete Guard**: Centralized SQLite state schema inspection with `PRAGMA query_only` protection and automatic fallback recommendation to `/archive` on migrated schemas (v42+).
- **Deterministic Risk Engine v2**: Upgraded routing (`ROUTER_VERSION = "1.2.0"`) from prompt-keyword matching to a multi-signal risk engine evaluating lexical complexity, repository characteristics, task scope, sensitive surfaces, and mutation risk without model inference.
- **Critical Concurrency Dominance**: Ensured concurrency bugs, data races, deadlocks, and consistency issues reliably classify into deep reasoning.
- **Sensitive Surface Mutation Protection**: Added bounded risk escalation for mutations targeting auth/tokens, database migrations, infrastructure/deployments, and secrets.
- **Rollback-Safe Installer Hardening**: Transactional installation and upgrade lifecycle with preflight validation, automatic rollback on failure, Python >= 3.10 enforcement, and `-NoPathUpdate` parameter support.
- **Hermetic Repository Test Isolation**: Isolated development tests from frozen production runtimes via custom meta-path finder.
- **Accurate Transport Protocol Framing**: Documented and verified newline-delimited JSON (JSONL) request/response transport over stdio with JSON-RPC-like correlation semantics without requiring a strict literal `"jsonrpc": "2.0"` field.

## [2.0.5] - 2026-08-20

### Added
- **Compact Single-Line Turn Header**: Streamlined per-turn metadata header displaying session state, model, reasoning tier, sandbox mode, remaining quota, and budget state in one clean line.
- **Semantic Shell Command Trace**: Automatic unwrapping of PowerShell wrappers to display clean, readable command lines in terminal traces.
- **Responsive History and Search**: Streamlined 1-line `/history` and `/search` listings with relative timestamps, path compaction, and width-aware truncation.
- **Grouped Interactive Help**: Structured `/help` output categorized into Temel (Core), Oturum (Session), and Geçmiş (History) sections.
- **Process-Local Numeric Thread Selection**: Sequential aliases (`[1]`, `[2]`, `[3]`) for `/history` and `/search` results allowing intuitive shorthand navigation (`/resume 1`, `/thread 2`).
- **Compact Verification Badges**: Single-line post-mutation verification badges (`[doğrulama] VERIFIED · 1 dosya · npm test · 0.1s`) with dynamic width fallback.
- **Clearer Interactive Approval UI**: Numbered Turkish action choices (`[1] Bu kez izin ver | [2] Oturum boyunca izin ver | [3] Reddet`) mapped to native protocol tokens.
- **Consistent Spinner & Spacing**: Clean `İşleniyor` activity spinner for TTY environments, zero ANSI leakage for `NO_COLOR`, and strict prevention of duplicate blank lines.

### Security & Invariants
- 100% deterministic test coverage across 722 automated regression cases.
- Zero-model inference overhead during orchestration, history navigation, and presentation rendering.
