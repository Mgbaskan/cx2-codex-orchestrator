# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.15] - 2026-08-29

### Terminal UX and Reliability
- Added deterministic visual separation between the sticky status line and the interactive `CX> ` prompt, ensuring exactly one blank visual row between status and prompt with the prompt starting at column 0.
- Implemented status redraw deferral while blocking interactive prompt input owns the terminal, guaranteeing partially typed user input cannot be clobbered or corrupted by asynchronous status refreshes.
- Explicitly separated cursor-colocated status semantics from parked scrollback status, preventing `suspend_status()` or subsequent turn starts from erasing active prompt lines.
- Bounded narrow/tiny terminal status text strictly by visible cell width before applying ANSI formatting, preventing line-wrapping into the prompt surface on narrow displays while preserving valid ANSI reset behavior.
- Preserved deterministic plain output behavior without ANSI sequences or decorative lines for non-TTY environments.

## [2.0.14] - 2026-08-28

### Security
- Added presentation-only escaping for model and command terminal controls while preserving canonical transcript text.
- Hardened Windows ordinary-file grants against DOS devices, ADS, device namespaces, ambiguous drive-relative paths, trailing normalization and reparse escapes; modern and legacy session decisions now require locally proven scope and server-advertised token support.

### Reliability and performance
- Added explicit sticky-row ownership, command lifecycle idempotency, bounded transport/turn collections, incremental paste and Markdown bounds, near-linear lazy pager wrapping, and truthful process cleanup evidence.
- Added bounded asynchronous transcript flushing, direct command-output indexing, CX-owned `/trace` timing fields, canonical quota-state/freshness projection and stable aggregate-diff offsets.
- Centralized the 2.0.14 release version and added lightweight `--version`/`--help` paths that avoid runtime and App Server initialization.

### Installer and CI
- Added an offline hashed managed-file manifest, obsolete managed-module reconciliation, bounded truthful cleanup reporting and separation of structural doctor from online account/model diagnostics.
- Updated GitHub-hosted Windows CI to `actions/checkout@v7`, `actions/setup-python@v7`, and explicit `contents: read` permissions.

### Qualification
- Validated with 585 deterministic tests, including explicit terminal, approval, Windows path, final-answer authority, large-response, process-cleanup, transcript, installer and isolation gates.
- Upgraded a disposable installation from the annotated `v2.0.13` source while preserving policy, usage, transcript, session and user-owned sentinel bytes; obsolete managed modules were reconciled and both SQLite databases retained clean integrity checks.
- An induced offline structural-health failure after managed mutation completed exact source and virtual-environment rollback with all preserved state hashes unchanged. Candidate installation itself requires no authenticated account/model access.

## [2.0.13] - 2026-08-27

### Added
- Durable, locally stored visible assistant transcripts with `/last`, optional built-in paging through `/last --page`, and explicitly confirmed workspace-scoped deletion through `/transcript clear`.
- A session-scoped ordinary workspace file-write grant keyed to the runtime instance, App Server thread and canonical workspace root. The grant covers only ordinary create/edit/patch operations inside that workspace.
- Compact bounded tool activity and the memory-only `/trace` / `/trace last` view for the previous completed turn, including command, working directory, status, exit code, duration, classification, host-execution provenance and explicit truncation evidence.
- Lightweight presentation-only terminal Markdown for headings, emphasis, inline and fenced code, simple lists, blockquotes, links and separators.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.13"`) and runtime version (`RUNTIME_VERSION = "2.0.13"`). Router remains `1.2.2` and the validated Codex baseline remains `0.144.4`.
- The `CODEX RESPONSE` lifecycle now presents a single verified semantic outcome: success exits `0`, failure exits `1`, blocked exits `2`, and interruption exits `130`.
- Terminal output now keeps visible assistant text durable across transient rendering, uses compact tool activity, and presents approvals and errors without making model prose authoritative.
- `/quota` explicitly refreshes the last-known quota snapshot and status reports `capturedAt` freshness age. No background quota polling occurs, unavailable values are not fabricated, and context is updated only from matching token-usage events.
- `/paste` reports accepted line and character counts without logging the pasted content while preserving `.send`, `.cancel`, escaped sentinels, strict UTF-8 and the 1 MiB input limit.
- TTY and non-TTY paths now have bounded Windows-first fallbacks for narrow terminals, redirected streams, `NO_COLOR`, `TERM=dumb`, static/screen-reader output, pager failures and cursor-control failures.

### Reliability
- Replaced the progress-insensitive absolute turn deadline with distinct monotonic idle and hard timeouts. Meaningful turn events extend idle time, active commands suppress idle expiry while remaining hard-bounded, and human approval wait remains excluded from charged runtime.
- Timeout handling performs a final completion-winning drain, requests `turn/interrupt` at most once, and retains bounded partial command diagnostics in a typed idle/hard timeout failure.
- Ordinary interactive prompts and `/paste` share one exception boundary, so expected timeouts render cleanly without escaping to the CLI traceback handler.
- Added explicit `execution.turn_idle_timeout_sec` and `execution.turn_hard_timeout_sec` tier maps while retaining `execution.turn_timeout_sec` as a backward-compatible idle override.
- Visible transcript retention is bounded to 16 MiB per response, 200 completed responses, 64 MiB of logical retained payload and 30 days. Trace and tool-activity state are also bounded.
- The 16 MiB transcript boundary no longer interrupts live responses: CX streams all visible output, tracks canonical equality with bounded UTF-8 digest/length state, and marks only durable retention as truncated.

### Security and Privacy
- Model turns remain effectively `read-only` on the qualified Windows/Codex 0.144.4 compatibility path; native Windows `workspaceWrite` degradation remains in effect.
- Host execution remains separate, explicit, bounded and one-shot. File-write grants do not authorize shell or host execution, destructive operations, privilege escalation, `dangerFullAccess`, unresolved or outside-workspace paths, or another thread/workspace/runtime/process.
- The transcript database stores canonical visible assistant response text and approved lifecycle metadata only. It does not retain raw reasoning, hidden chain-of-thought, commentary, raw protocol payloads, command output or approval secrets.
- Multiple-final ambiguity, notification FIFO ordering, failure and timeout precedence, approval decline fail-closed behavior, `SANDBOX_DENIED` classification, `TEST_FAILURE` precedence and ripgrep exit-1 neutrality remain preserved.

### Known Limits
- Quota is a last-known snapshot rather than a live-polled feed, and trace state does not survive runtime replacement or CLI exit.
- Markdown tables, nested block parsing, and malformed or unfinished delimiters remain literal text.
- Transcript data is local plaintext under `CX_HOME/data/visible-transcript.sqlite3`; no persistence mechanism provides a zero-risk security guarantee.

### Qualification
- Validated with 540 deterministic tests, including targeted version, terminal UX, transcript, pager, grant, trace, status, paste, lifecycle, installer rollback and security/failure-classification coverage.
- A disposable Windows installation was upgraded from the immutable `v2.0.12` tag to the prepared 2.0.13 tree with policy, usage database, arbitrary transcript bytes and user-state sentinels preserved. An induced dependency-install failure then completed transactional rollback with managed files, virtual environment and user state restored byte-for-byte.

## [2.0.12] - 2026-08-25

### Fixed
- Replaced the progress-insensitive absolute turn deadline with distinct monotonic idle and hard timeouts. Meaningful turn events extend idle time, active commands suppress idle expiry while remaining hard-bounded, and human approval wait remains excluded from charged runtime.
- Timeout handling now performs a final completion-winning drain, requests `turn/interrupt` at most once, and retains bounded partial command diagnostics in a typed idle/hard timeout failure.
- Ordinary interactive prompts and `/paste` now share one exception boundary, so expected timeouts render cleanly without escaping to the CLI traceback handler.

### Changed
- Development CLI and runtime versions are `2.0.12`; Router remains `1.2.2` and the validated Codex baseline remains `0.144.4`.
- Added explicit `execution.turn_idle_timeout_sec` and `execution.turn_hard_timeout_sec` tier maps while retaining `execution.turn_timeout_sec` as a backward-compatible idle override.

## [2.0.11] - 2026-08-25

### Fixed
- Live Codex App Server command failures whose stderr/stdout arrives through `item/commandExecution/outputDelta` are now correctly included in verification classification when `item/completed` lacks inline output.
- Sandbox-write failures such as `EPERM` on legitimate verification temporary directory and cache writes can now reach the explicit bounded-verification approval path in the real streamed App Server lifecycle.
- Genuine test, typecheck, lint, and build failures take precedence over permission noise and are not offered bounded-host retry.
- Custom -TargetDir installations now resolve CX runtime state and the bundled Codex binary from their own installed runtime instead of falling back to the default user .cx directory.
- Upgrades and installations that encounter a mid-flight failure now execute exhaustive transactional rollback with per-operation error handling and explicit virtual environment provenance tracking, ensuring newly created files (such as `cx_home.py`) and newly provisioned virtual environments are completely removed, pre-existing files and virtual environments are fully restored to baseline, and rollback status (complete vs incomplete) is truthfully reported without masking errors.

### Reliability
- Command diagnostic retention uses a bounded per-command head+tail window: first 64 KiB + most recent 448 KiB, retaining at most 512 KiB of diagnostic bytes per command. Retained command-diagnostic memory is bounded per command and independent of total streamed byte volume for that command.
- Late diagnostic events received after `item/completed` may update audit classification records but cannot reopen bounded-host authorization.
- Interleaved command items retain strictly isolated diagnostic and authorization state with zero cross-command contamination.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.11"`) and runtime version (`RUNTIME_VERSION = "2.0.11"`).
- Preserved Router version (`ROUTER_VERSION = "1.2.2"`) and validated Codex baseline (`VALIDATED_CODEX_VERSION = "0.144.4"`).

### Security
- Bounded host execution remains explicit one-shot user authorization only.
- No automatic host execution.
- No `dangerFullAccess` fallback.
- Effective model turn remains `:read-only`.
- Post-completion late evidence fails closed for authorization.
- CX2's bounded-verification path does not permission-alter or permanently redirect the shared `.codex-agent-cache` directory. Ordinary Codex-managed cache behavior remains unchanged.

### Qualification
- Validated with 408 deterministic regression tests across normal discovery and isolated profile environments.
- Qualified through 32 sandbox-block soak cycles, 42 failure-conflict cycles, 36 late-evidence fail-closed variations, 50 interleaved multi-command items, large stream soak up to 100 MB, 14 live App Server turns (zero tracebacks, zero stuck turns, read-only persistence maintained), representative multi-suite backend verification canary (18 suites passed, 1 skipped; 51 tests passed, 1 skipped; 0 failures), and comprehensive installer rollback matrix (fresh install, existing target without venv clean rollback, normal upgrade, recoverable failed upgrade exact rollback, obstructed rollback truthful reporting, recovery upgrade, idempotent reinstall, custom target root isolation, and side-by-side mutual isolation).

## [2.0.10] - 2026-08-24

### Fixed
- Windows read-only verification commands that require legitimate runtime/cache writes (such as Jest and other test/build/typecheck/lint workflows) can now be recovered through explicit bounded verification execution instead of remaining permanently `BLOCKED`.
- Prevented verification workflows from failing solely because the Codex read-only sandbox denies temporary directory or cache writes.
- Bounded process output capture to prevent unbounded process-memory accumulation during high-volume stdout/stderr generation.

### Added
- **Bounded Verification Execution**: Explicit one-shot interactive authorization for verification commands blocked by sandbox write restrictions.
- **Exact Command & CWD Display**: Transparent interactive prompt presenting the exact command string and working directory before bounded host execution.
- **Bounded Concurrent Output Capture**: Concurrent stream draining capping retained stdout and stderr buffers at 512 KiB each, empirically validated with child output volumes up to 100 MB per stream.
- **Full Process-Tree Termination**: Process lifecycle management forcefully terminating complete process hierarchies via `taskkill /F /T /PID` upon command timeout.
- **Bounded Host Execution Provenance**: Truthful recording of `bounded_host_execution` metadata in turn results and verification evidence summaries.

### Changed
- Upgraded CLI version (`CLI_VERSION = "2.0.10"`) and runtime version (`RUNTIME_VERSION = "2.0.10"`).
- Preserved Router version (`ROUTER_VERSION = "1.2.2"`) and validated Codex baseline (`VALIDATED_CODEX_VERSION = "0.144.4"`).

### Security / Reliability
- No automatic host execution.
- No `dangerFullAccess` fallback.
- User decline is fail-closed.
- One approval authorizes only the single exact command instance presented; no session-wide permission elevation.
- Effective model sandbox remains `:read-only` throughout the turn.
- Genuine test, lint, typecheck, and build failures remain genuine failures and are never offered bounded host execution.
- Shared `.codex-agent-cache` is never modified or permission-mutated.

### Qualification
- Validated with 355 deterministic regression tests across normal discovery and isolated profile environments.
- Qualified through Windows quoting matrix (15 variations), 50-offer approval soak, 11 failure semantics scenarios, output soak up to 100 MB, 20 timeout process-tree cycles, 50-turn interactive shell resilience soak, 6 real model canaries, and a live Jest sample-workload canary.

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
