# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.9] - 2026-08-24

### Fixed
- Interactive CX shell no longer terminates on ordinary turn-level runtime errors such as turn timeouts and App Server transport failures.
- App Server process death is detected promptly instead of waiting for the full turn timeout.
- Final completion events buffered during App Server exit are reconciled before declaring infrastructure failure.
- ripgrep exit code 1/no-match is presented neutrally (`[no-match]`) instead of as a generic command failure.
- `npm run type-check` is recognized correctly by verification classification.
- Unquoted `cmd /c ...` display-command unwrapping is handled safely.
- Repeated approval requests are bounded and explicit declines can be remembered safely within a turn.
- Human time spent at approval prompts no longer consumes the model/turn computation deadline.
- Codex CLI 0.144.4 Windows `workspaceWrite` degradation is mitigated through a safe read-only compatibility execution mode with explicit mutation approval.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.9"`) and runtime version (`RUNTIME_VERSION = "2.0.9"`).
- Preserved Router version (`ROUTER_VERSION = "1.2.2"`) and validated Codex baseline (`VALIDATED_CODEX_VERSION = "0.144.4"`).

### Security / Reliability
- No global host execution fallback.
- No automatic mutation approval.
- Compatibility fallback is fail-closed.
- Requested router sandbox remains truthful (`workspace-write`) while runtime effective sandbox is separately represented (`read-only`).
- Approval escalation attempts are bounded per turn and reset on each new turn.

### Qualification
- Validated with 336 deterministic regression tests across discovery and isolated profile environments.
- Qualified with Windows adversarial runtime soak and six controlled live model canaries on disposable fixtures.

## [2.0.8] - 2026-08-23

### Added
- **Long Prompt Transport Layer**: Complete multi-source prompt transport supporting `--prompt-file PATH`, `--stdin` (PowerShell piping), `--route-file PATH` (deterministic zero-model preview), and interactive `/paste ... .send` multiline mode. Includes strict UTF-8 with BOM handling, a 1 MiB (1,048,576 bytes) safety guard, and full multiline structure preservation. Disambiguates `--file` (attachment mention) from `--prompt-file` (full turn prompt).
- **Task-Shape Risk Routing (Router 1.2.2)**: Enhanced deterministic routing engine detecting composite task-shape patterns including cross-surface implementations, plan/code reconciliation directives, and explicit verification matrices. Short but structurally complex tasks route reliably to `deep` tier and `high` reasoning independent of raw prompt length. Hardened mutation classification with improved negated-write boundary filtering.
- **Required Verification Contract**: Conservative deterministic extraction of explicit user-specified quality gates under verification section headings (e.g. `QUALITY GATES`, `REQUIRED VERIFICATION`, `DOĞRULAMA`). Matches observed App Server command executions by canonical command identity and explicit surface/cwd context. Missing, failed, or blocked required gates prevent false `VERIFIED` status, keeping `FAILED`, `BLOCKED`, `INCONCLUSIVE`, and `INTERRUPTED` states strictly distinct. Model prose is treated as non-authoritative.
- **Command Execution CWD Provenance**: Preserves explicit command working directory metadata from Codex App Server notifications through `TurnRunResult` and `CommandExecutionSummary` into the required verification matching engine, preventing false 0/N gate coverage for subproject/monorepo commands.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.8"`) and runtime version (`RUNTIME_VERSION = "2.0.8"`).

### Qualification
- Validated with 262 deterministic local regression tests across normal and isolated profile environments.
- Live qualification performed via disposable real-model canary (`gpt-5.6-sol`, `high` reasoning, `workspace-write`) achieving 8/8 observed required gates passed and final assurance `VERIFIED` with zero approval interruptions and zero production mutation.

### Added
- **Whole-Project Audit Routing Calibration**: Composite multi-signal detection (`broad-project-audit`) in the deterministic risk engine (`ROUTER_VERSION = "1.2.1"`), ensuring broad repository security, architecture, and defect audits escalate reliably to `deep` tier and `high` reasoning across English and Turkish phrasing.
- **Isolated Read-Only Test Execution Environment**: Disposable per-process execution environment (`test_env.py`) wiring isolated writable cache roots (`TEMP`, `TMP`, `GOCACHE`, `GOTMPDIR`, `npm_config_cache`, `PYTHONPYCACHEPREFIX`) into App Server child processes, enabling test runners to initialize without mutating the source repository.
- **Granular Command Outcome Classification**: Determinist outcome categorization separating true project-level failures (`FAILED` / `TEST_FAILURE`, `LINT_FAILURE`, `BUILD_FAILURE`, `TYPECHECK_FAILURE`) from sandbox/environment denials (`BLOCKED` / `SANDBOX_DENIED`, `ENV_CACHE_DENIED`, `EXECUTABLE_NOT_FOUND`).
- **Conservative Inconclusive Verification**: Safe fallback classifying unverified non-zero exits or masked commands as `INCONCLUSIVE` instead of falsely reporting project test failures.
- **Deterministic Read-Only Audit Assurance**: Aggregation engine evaluating evidence completeness (`COMPLETE`, `PARTIAL`, `UNVERIFIED`, `INTERRUPTED`) and rendering compact terminal badges (`[audit] · COMPLETE · 3 checks · 3 passed`).
- **Route-Aware Dynamic Turn Timeouts**: Dynamic execution deadlines calibrated by tier (`routine: 300s`, `standard: 450s`, `deep: 600s`) with bounded custom policy override support `[30s, 1800s]`.
- **Safe Timeout Turn Interruption**: Automated best-effort `turn/interrupt` dispatch to Codex App Server upon deadline expiration, reducing the risk of orphaned background work while preserving `TimeoutError` semantics.
- **Bounded Broad-Audit Execution Guidance**: Process-local developer instructions for whole-project audits promoting risk-prioritized inspection, early verification execution, and synthesis budgeting without mutating the original user prompt.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.7"`) and runtime version (`RUNTIME_VERSION = "2.0.7"`).

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
