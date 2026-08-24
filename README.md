# CX2 — Intelligent Orchestration and Terminal UX Layer for OpenAI Codex

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Release](https://img.shields.io/badge/release-v2.0.11-green.svg)](https://github.com/Mgbaskan/cx2-codex-orchestrator/releases/tag/v2.0.11)
[![Tests](https://github.com/Mgbaskan/cx2-codex-orchestrator/actions/workflows/test.yml/badge.svg)](https://github.com/Mgbaskan/cx2-codex-orchestrator/actions/workflows/test.yml)

**CX2** is a Windows-first, policy-driven orchestration and terminal UX layer for OpenAI Codex.

It sits around the Codex App Server lifecycle and automates task routing, quota-aware execution, reasoning level selection, sandbox permissions, thread/session management, verification, approvals, and terminal presentation.

CX2 does **not** replace Codex and does not introduce another AI model for orchestration decisions. Its routing and policy logic are deterministic and rule-based.

---

> [!NOTE]
>
> ### Disclaimer & Trademark Notice
>
> CX2 is an independent, unofficial community project.
>
> It is **not affiliated with, endorsed by, or maintained by OpenAI**.
>
> OpenAI and Codex are referenced solely for descriptive and compatibility purposes.

---

## What is CX2?

OpenAI Codex is already capable of understanding repositories, editing files, executing commands, and working through multi-step coding tasks.

CX2 adds a supervisory layer around that workflow.

Instead of treating every prompt identically, CX2 evaluates the current task and repository context before starting a Codex turn.

It can automatically determine:

- task complexity
- write intent
- model tier
- reasoning effort
- sandbox permissions
- quota/budget constraints
- thread/session reuse
- post-mutation verification requirements

Conceptually:

```text
Developer
    |
    v
+-------------------------------+
|             CX2               |
|                               |
|  Task & Risk Classification   |
|  Write Intent Detection       |
|  Model / Reasoning Routing    |
|  Quota & Budget Guard         |
|  Session / Thread Management  |
|  Approval Handling            |
|  Verification Assurance       |
|  Terminal UX                  |
+---------------+---------------+
                |
                v
+-------------------------------+
|       OpenAI Codex            |
|       App Server / Core       |
+---------------+---------------+
                |
                v
       Shell / Files / Web
```

CX2 is therefore closer to a **deterministic Codex supervisor and execution policy layer** than a separate coding agent or multi-agent framework.

---

## Why CX2?

A long Codex session can involve several decisions that the user would otherwise need to manage manually:

- Which model should handle this task?
- How much reasoning effort is justified?
- Should the turn be read-only or allowed to modify the workspace?
- Is the remaining quota sufficient for a deep task?
- Should an existing thread be resumed?
- Did the mutation actually pass a meaningful verification command?
- Is a successful exit code trustworthy?
- How can long native thread IDs be managed efficiently?
- How can all of this remain readable inside a terminal?

CX2 attempts to automate those decisions while keeping the behavior deterministic, observable, and user-controlled.

---

# Key Features

## Deterministic Task & Risk Routing

CX2 classifies tasks into three primary tiers:

| Tier       | Typical use                                                      | Reasoning | Typical sandbox               |
| ---------- | ---------------------------------------------------------------- | --------- | ----------------------------- |
| `routine`  | inspection, explanation, simple UI work                          | `low`     | `read-only` or task-dependent |
| `standard` | bug fixes, features, refactors                                   | `medium`  | task-dependent                |
| `deep`     | architecture, concurrency, security, complex root cause analysis | `high`    | task-dependent                |

Routing does **not** require an extra model call.

CX2 evaluates deterministic task-shape signals such as:

- **Lexical complexity**: Critical concurrency, distributed consistency, structural redesign, root-cause investigations.
- **Cross-surface implementation**: Coordinated changes across multiple subprojects (e.g. mobile, backend, web).
- **Plan & code reconciliation**: Tasks requiring implementation alignment against plan specifications.
- **Verification matrices**: Tasks defining multi-command or multi-surface quality requirements.
- **Sensitive surface mutation**: Mutations targeting auth/tokens, database migrations, infrastructure, or secrets.
- **Repository characteristics**: Monorepo structures, tracked file counts, dirty working-tree state.
- **Explicit write intent & negation**: Distinguishing positive mutation from explicit read-only instructions (`do not modify files`, `sadece oku`).
- **Routine reductions**: Bounded risk reductions for superficial styling, typography, typo fixes, or documentation.

Complex tasks route to `deep` tier and `high` reasoning based on task structure rather than prompt length alone.

The routing engine is intentionally heuristic and deterministic rather than LLM-based.

This makes routing:

- predictable
- fast
- inexpensive
- testable
- explainable

but it also means classification is not equivalent to full semantic understanding.

---

## Automatic Model Selection

CX2 maps routing tiers to available Codex models based on the configured policy:

```text
routine  -> gpt-5.6-luna
standard -> gpt-5.6-terra
deep     -> gpt-5.6-sol
```

If a preferred model is unavailable or not exposed in the user's Codex profile, CX2 deterministically falls back to the next configured visible model according to the policy hierarchy.

Model availability depends on the user's Codex account, environment, and upstream availability.

These model names should be treated as compatibility/runtime policy rather than permanent API guarantees.

---

## Write Intent Detection

Task complexity and mutation permissions are evaluated separately.

For example:

```text
Explain the authentication system.
```

may be classified as a complex task while still remaining:

```text
read-only
```

whereas:

```text
Change the button color.
```

may be routine while requiring:

```text
workspace-write
```

CX2 also recognizes explicit negation such as:

```text
do not modify any files
read-only
sadece oku
dosyalarda değişiklik yapma
```

to avoid accidentally escalating a read-only request into a mutation-capable turn.

---

## Quota-Aware Budget Guard

CX2 monitors Codex rate-limit information and applies a budget state before execution.

Current states include:

```text
NORMAL
CONSERVE
CRITICAL
EMERGENCY
```

The goal is to avoid consuming expensive routing tiers unnecessarily when remaining capacity is low.

Example terminal header:

```text
[cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE
```

If the configured quota or spend limit is exhausted, CX2 can prevent a new model turn from starting.

---

## Persistent Repository Sessions

Git repositories can retain CX2 session continuity across turns.

CX2 tracks repository-scoped session information while keeping native Codex thread IDs authoritative.

It does not replace the Codex thread model.

The session layer adds convenience around it.

---

## Numeric Thread Selection

Native Codex thread IDs are long.

CX2 makes history easier to use by assigning temporary process-local aliases:

```text
[1] Authentication refactor
[2] Docker build issue
[3] API validation work
```

You can then use:

```text
/resume 1
/thread 2
/turns 3
/archive 2
```

instead of copying a full native thread UUID.

The numeric mapping is process-local and UX-only.

The real Codex thread ID remains the source of truth.

---

## History and Search

CX2 provides compact thread management commands:

```text
/history
/search <query>
/thread <id|no>
/turns <id|no>
/resume <id|no>
/rename <id|no>
/archive <id|no>
/unarchive <id|no>
/delete <id|no>
```

History and search results are optimized for terminal readability and numeric selection.

---

## Compact Terminal UX

CX2 reduces the amount of infrastructure noise normally visible during execution.

Instead of multiple metadata lines:

```text
quota...
session...
route...
```

CX2 renders a compact status header:

```text
[cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE
```

Shell commands are also normalized for display.

For example:

```text
> "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Command "git status --short"
```

can be displayed as:

```text
> git status --short
[ok] 323ms
```

The original raw command remains available internally for execution and verification logic.

---

## Post-Mutation Verification Assurance

When Codex modifies files, CX2 can inspect subsequent validation evidence rather than relying only on the assistant's final message.

Example:

```text
[doğrulama] VERIFIED · 1 dosya · npm test · 0.4s
```

Failure:

```text
[doğrulama] BAŞARISIZ · npm test · exit 1 · 1.3s
```

CX2 also attempts to reject command patterns that can mask failures, such as:

```bash
npm test || true
```

or:

```bash
npm test ; exit 0
```

---

## Required Verification Contract & CWD Provenance

When a user prompt includes an explicit quality gate specification:

```markdown
QUALITY GATES

Backend:
- npm run lint
- npm test
- npm run build

Web:
- npm run lint
- npm run build
```

CX2 deterministically extracts the required gates and matches them against actual commands executed by the Codex App Server:

- **Deterministic Gate Extraction**: Quality gates under explicit verification/quality-gate headings (e.g. `QUALITY GATES`, `REQUIRED VERIFICATION`, `VERIFICATION GATES`, `CHECKS TO RUN`, `DOĞRULAMA KAPILARI`) are extracted without model calls. Concrete executable command lines are parsed conservatively; vague prose instructions (e.g. "run all tests") do not create required gates.
- **Strict Surface / Working-Directory Isolation**: Commands executed in one subproject directory do not satisfy gates in another. For example, `backend -> npm run build` (executed with `cwd="backend"`) is distinct evidence from `web -> npm run build` (executed with `cwd="web"`).
- **CWD Provenance Preservation**: The CX2 runtime preserves working directory provenance from the App Server through the execution ledger to ensure monorepo commands match their intended surface.
- **Non-Authoritative Model Prose**: Assistant prose claims (e.g. "All tests passed") cannot satisfy required gates. Only observed App Server command executions with matching command identity, surface working directory, and exit code 0 count as passed.
- **No Blind Auto-Execution**: CX2 does **not** arbitrarily execute prompt command text on the host outside the Codex turn. The model executes commands within its standard sandbox and approval lifecycle.
- **Accurate Gate Badges**:
  ```text
  [doğrulama] · VERIFIED · zorunlu 8/8 kapı geçti
  ```
  If any required gate is missing, failed, or blocked, final status cannot become `VERIFIED`.

### Important

`VERIFIED` does **not** mean:

> the implementation is mathematically proven to be correct or bug-free.

It means:

> the observed validation evidence satisfied CX2's verification rules.

Human review remains appropriate for important changes.

---

## Read-Only Audit Assurance

During read-only inspection and broad repository audit tasks, CX2 tracks verification command executions and evaluates overall evidence completeness:

```text
[audit] · COMPLETE · 3 checks · 3 passed
```

Partial evidence (e.g. when some checks pass while others are blocked by sandbox/environment limits):

```text
[audit] · PARTIAL · 5 checks · 2 passed
```

### Bounded Whole-Project Audits

Broad whole-project audits use bounded, risk-prioritized execution guidance to reduce exhaustive sequential traversal and reserve execution budget for verification and structured finalization.

Tier-calibrated timeouts reduce premature timeout risk for complex deep tasks while keeping routine tasks bounded:

```text
routine  -> 300s
standard -> 450s
deep     -> 600s
```

---

## Interactive Approvals

CX2 preserves Codex approval semantics while presenting them in a compact terminal-oriented interface.

Example:

```text
[onay] Komut çalıştırma izni gerekiyor.

[1] Bu kez izin ver
[2] Oturum boyunca izin ver
[3] Reddet

Seçim [3]:
```

The human-readable labels are mapped back to the native Codex approval tokens.

CX2 does not silently broaden permissions.

Non-interactive environments retain safe-deny behavior.

---

## Runtime Recovery and Ctrl+C

CX2 includes runtime handling for:

- interrupted turns
- App Server lifecycle recovery
- pending request cleanup
- controlled Ctrl+C interruption
- failure boundaries

Interrupted execution must not be reported as successfully verified.

---

## Web and Tool Routing

CX2 can control native Codex web-tool availability at the turn/session level when required by the routing flow.

Web activity remains a Codex capability; CX2 manages when that capability is exposed to the active runtime.

---

## Files and Attachments

The CX2 runtime supports Codex workflows involving:

- text files
- source code
- images
- PDFs
- binary attachments

Actual interpretation and processing capabilities depend on the underlying Codex runtime.

---

# Security Model

CX2 is designed around several boundaries.

## Codex Authentication Remains External

CX2 does not bundle credentials.

It reuses the user's existing supported Codex authentication environment.

The public repository must never contain:

```text
auth.json
API keys
access tokens
refresh tokens
private session databases
```

CX2's installer does not copy or manage the user's Codex credentials.

---

## Workspace Permissions Are Explicit

CX2 distinguishes between:

```text
read-only
```

and:

```text
workspace-write
```

based on the detected task intent.

Approval and sandbox behavior remain controlled by the Codex runtime.

---

## Local Runtime State

CX2 runtime data lives under:

```text
~/.cx
```

Examples include:

```text
data/
logs/
runtime/
```

CX2 runtime state is not intended to be committed into user repositories.

---

# Requirements

## Supported Platform

CX2 is currently **Windows-first**.

Validated environment:

- Windows 10 / Windows 11
- Windows PowerShell 5.1+
- Python 3.12
- Git for Windows
- OpenAI Codex local authentication/environment

The public installer currently targets Windows.

Other operating systems are not yet considered validated platforms.

---

## Python

Python 3.12 is the primary validated version.

The project may operate on compatible Python 3.x versions, but the GitHub CI baseline currently targets Python 3.12.

---

## Codex Runtime & Compatibility

CX2 uses a centralized compatibility layer (`runtime/cx2/codex_compat.py`) to manage interactions with the OpenAI Codex CLI and App Server runtime.

- **Validated Baseline**: `openai-codex 0.144.4` / `openai-codex-cli-bin 0.144.4`.
- **Capability-Oriented**: Distinguishes core App Server operations from optional capabilities.
- **Safe Degradation**: Unverified newer versions or modified state schemas (e.g. SQLite schema v42+) degrade optional features like `/delete` cleanly to fail-closed without breaking the core runtime or modifying the database. `/archive` is available as a non-destructive alternative.
- **Non-Vendored**: CX2 uses the local virtual environment or detected Codex executable; authentication uses the local Codex environment.

---

# Installation

Clone the repository:

```powershell
git clone https://github.com/Mgbaskan/cx2-codex-orchestrator.git
cd cx2-codex-orchestrator
```

Run the installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

The installer will:

1. Create the `~/.cx` directory structure.
2. Create a dedicated Python virtual environment.
3. Install pinned runtime dependencies.
4. Copy CX2 runtime files.
5. Compile the native Windows launcher.
6. Install `cx.exe` under `~/.cx/bin`.
7. Add `~/.cx/bin` to the user's `PATH` if required.
8. Run `cx --doctor`.

If an existing CX installation is detected, the installer updates CX-managed
source/runtime files while preserving user-managed state such as the existing
policy configuration and local runtime data. Back up custom modifications
before upgrading.

---

# Quick Start

Open a Git repository:

```powershell
cd C:\Projects\my-project
```

Start CX2:

```powershell
cx
```

You can now interact normally:

```text
cx> explain the authentication flow
```

### Multiline Interactive Input (`/paste`)

For long multiline prompts in interactive mode, enter paste mode:

```text
cx> /paste
paste> Implement the authentication middleware refactor.
paste> Ensure all error types conform to the RFC-7807 specification.
paste>
paste> QUALITY GATES
paste> - npm run lint
paste> - npm test
paste> .send
```

Use `.send` on an empty line to submit, or `.cancel` to discard.

### One-Shot Execution

Positional prompt:

```powershell
cx "Fix the validation bug and run the relevant tests"
```

### Long Prompt File Transport (`--prompt-file`)

Pass complex, large task files or specifications with complete UTF-8/multiline fidelity:

```powershell
cx --prompt-file .\task.md
```

### Standard Input Piping (`--stdin`)

Pipe prompt text directly into CX2:

```powershell
Get-Content -Raw -Encoding UTF8 .\task.md | cx --stdin
```

### Deterministic Route Preview (`--route-file` / `--route`)

Preview the tier, reasoning level, model selection, and sandbox mode without performing model inference:

```powershell
cx --route-file .\task.md
```

> [!NOTE]
> `--file` remains an attachment/context file mention (e.g. `cx "Inspect this log" --file error.log`). It is distinct from `--prompt-file`, which supplies the entire turn prompt.

## Basic

| Command  | Description           |
| -------- | --------------------- |
| `/help`  | Show interactive help |
| `/paste` | Enter multiline paste mode (`.send` to submit, `.cancel` to abort) |
| `/clear` | Clear terminal output |
| `/exit`  | Exit CX2              |

## Session

| Command         | Description                   |
| --------------- | ----------------------------- |
| `/new`          | Start a fresh session binding |
| `/session`      | Display current session state |
| `/quota`        | Display live quota state      |
| `/stats`        | Display usage statistics      |
| `/route <task>` | Preview routing decision      |
| `/doctor`       | Run runtime diagnostics       |

## History and Threads

| Command                   | Description                                       |
| ------------------------- | ------------------------------------------------- |
| `/history [filter]`       | List native Codex threads                         |
| `/search <query>`         | Search thread history                             |
| `/thread [id\|no]`        | Show thread details                               |
| `/turns [id\|no]`         | Show thread turns                                 |
| `/resume [id\|no]`        | Resume a thread                                   |
| `/rename [id\|no] <name>` | Rename a thread                                   |
| `/archive [id\|no]`       | Archive a thread                                  |
| `/unarchive [id\|no]`     | Restore an archived thread                        |
| `/delete [id\|no]`        | Permanently delete a native thread when supported |

Numeric selectors come from the latest visible `/history` or `/search` result.

Example:

```text
/history

[1] Authentication cleanup
[2] Docker migration
[3] API tests
```

Then:

```text
/resume 2
```

---

# Routing Model

The current CX2 routing model is deterministic.

Conceptually:

```text
Prompt
   +
Repository Signals
   +
Write Intent
   +
Policy
   +
Quota
   |
   v
Task Score
   |
   +--> routine
   +--> standard
   +--> deep
   |
   v
Model + Reasoning + Sandbox
```

CX2 intentionally avoids using another LLM to decide how Codex should be invoked.

See:

[Routing Model](docs/routing-model.md)

---

# Configuration

The example policy is located at:

```text
config/policy.example.json
```

The installed configuration is stored under:

```text
~/.cx/policy.json
```

Policy controls behavior such as:

- tier thresholds (`routine_max: 1`, `deep_min: 7`)
- reasoning levels (`routine: low`, `standard: medium`, `deep: high`)
- model preferences and fallbacks
- budget thresholds (70%, 85%, 95%, 100%)
- tier execution timeouts (`routine: 300s`, `standard: 450s`, `deep: 600s`, override via `execution.turn_timeout_sec`)
- escalation
- runtime features
- experimental integrations

See:

[Configuration Guide](docs/configuration.md)

---

# Experimental Features

## CCE

CCE integration exists as an experimental, policy-gated feature.

It is:

```text
disabled by default
```

and should not currently be considered part of the core stable runtime contract.

---

## RTK

RTK can be used as an optional command/tool integration.

RTK binaries are not bundled with CX2.

No specific optimization or token-saving percentage is guaranteed.

---

# Known Limitations

CX2 v2.0.11 currently has several known limitations.

### Windows-first

The current installer, launcher, terminal handling, and CI baseline are Windows-oriented.

### Windows Codex 0.144.4 Sandbox Compatibility Mode

On the qualified Windows environment with Codex CLI 0.144.4, native `workspaceWrite` sandbox initialization reproduced a deterministic pre-command hang. CX2 applies an exact compatibility mitigation: mutating tasks route with requested sandbox `workspace-write` while the effective runtime execution runs in `read-only` sandbox with `approval_policy = "on-request"`. Inspections and reads continue sandboxed, while mutations trigger explicit user authorization (`item/fileChange/requestApproval` or `item/commandExecution/requestApproval`).

In CX2 2.0.11, verification commands that require temporary runtime or cache writes and fail with sandbox write denials (e.g. `SANDBOX_DENIED`, `WORKSPACE_WRITE_REQUIRED`, `TEMP_CACHE_UNAVAILABLE`) are classified truthfully from streamed command output (`outputDelta`) and recovered via explicit one-shot bounded verification execution with transparent command and working directory approval, without altering the model turn's read-only sandbox. Genuine test, typecheck, lint, or build failures take precedence over permission noise and are not offered host retry.

### Windows / App Server Sandbox Spawning

Under constrained Windows sandboxes, complex test harnesses that spawn nested child processes (e.g. multi-process test runner harnesses) may be restricted by the platform environment. When this occurs, outcomes are safely classified as `BLOCKED` or `INCONCLUSIVE` rather than false project test failures. Direct in-process test scripts are recommended where possible.

### Conservative Required Gate Extraction

Required verification gate extraction is intentionally conservative and requires explicit verification headings (e.g. `QUALITY GATES`, `DOĞRULAMA KAPILARI`). Vague inline prose mentions will not be converted into strict required gates.

### No Automatic Host Execution

CX2 does not automatically execute verification commands on the host outside the active model turn. Bounded verification execution strictly requires explicit one-shot interactive authorization per command instance. Post-completion late stream deltas fail closed and cannot reopen host authorization.

### Native Thread Delete Compatibility

CX2 uses a pinned Codex App Server compatibility baseline.

On newer Codex state schemas (v42+), native delete safely refuses the operation rather than attempting an unsafe database mutation (`DEGRADED`).

When this occurs:

```text
/archive
```

is the recommended non-destructive alternative.

### PowerShell 5.1 Argument Handling

Some direct PowerShell 5.1 argv quoting behavior occurs before the native CX launcher receives arguments. Complex embedded quoting in one-shot positional arguments may encounter PowerShell parsing limitations; `--prompt-file` or `/paste` mode is recommended for complex multiline input.

### Non-Git Directories

Non-Git interactive continuity is process-local. Persistent repository-scoped session behavior is primarily designed around Git repositories.

### Repository Move / Rename

Moving or renaming a repository can change the repository identity used by the session layer.

### Deterministic Routing

Routing is heuristic and rule-based. It is predictable and testable, but it does not have the semantic understanding of a dedicated language model.

### Broad Whole-Project Audits

Broad audits on massive codebases with hundreds of files may require custom turn timeout configuration in `policy.json` (e.g. up to 1800s) if the default 600s budget is exhausted during deep inspection.

---

# Project Status

Current stable release:

```text
CX2 2.0.11
```

Status:

```text
STABLE / FROZEN
```

The public release includes:

- streamed command output diagnostic classification (`outputDelta`)
- bounded per-command head+tail diagnostic retention (64 KiB head + 448 KiB tail, max 512 KiB)
- failure precedence over permission noise (genuine test/typecheck/lint/build failures)
- fail-closed late evidence authorization barrier
- long prompt transport (`--prompt-file`, `--stdin`, `--route-file`, `/paste`)
- deterministic task-shape risk routing (Router 1.2.2)
- required verification gate extraction and matching
- working-directory (CWD) execution provenance
- bounded verification execution (one-shot approval, exact command/CWD display)
- bounded streaming output capture and process-tree lifecycle management
- quota-aware execution
- persistent Git sessions
- numeric thread aliases
- history/search management
- compact terminal UX
- semantic command presentation
- verification assurance
- approval handling
- runtime recovery
- Windows installer
- deterministic public tests
- GitHub Actions CI

Released versions are treated as immutable.

Future source changes will be released under a newer version.

---

# Recommended Workflow

For important repositories, CX2 should still be used as part of a normal engineering workflow:

```text
Feature branch
     |
     v
CX2 / Codex
     |
     v
Verification
     |
     v
git diff / review
     |
     v
Human approval
     |
     v
Merge
```

CX2 verification is intended to improve evidence quality, not eliminate human review.

---

# Documentation

Detailed documentation is available in [`docs/`](docs/):

- [Architecture Overview](docs/architecture.md)
- [Installation Guide](docs/installation.md)
- [Configuration & Policy](docs/configuration.md)
- [Routing Model](docs/routing-model.md)
- [Terminal UX Design](docs/terminal-ux.md)
- [Verification Assurance](docs/verification-assurance.md)
- [Command Reference](docs/commands.md)
- [Security Model](docs/security-model.md)
- [Compatibility](docs/compatibility.md)
- [Known Limitations](docs/known-limitations.md)

---

# Development

Run the deterministic public test suite:

```powershell
python -m unittest discover -s tests -v
```

GitHub Actions runs the same public deterministic test suite on Windows.

Model calls are not required for these unit tests.

---

# Contributing

Contributions are welcome.

Before contributing, please read:

[CONTRIBUTING.md](CONTRIBUTING.md)

Important project rules include:

- never commit credentials
- never commit Codex authentication files
- never commit private thread/session databases
- use deterministic tests where possible
- do not hotfix frozen releases
- behavior changes must target the next release version

For security issues, see:

[SECURITY.md](SECURITY.md)

---

# Security Reporting

Please do not disclose sensitive vulnerabilities in public issues.

Use GitHub's private vulnerability reporting / Security Advisory mechanism when available.

Never include:

- Codex authentication files
- API keys
- access tokens
- private repository contents
- personal conversation history
- private session databases

See:

[SECURITY.md](SECURITY.md)

---

# Roadmap

Potential future directions include:

- richer deterministic repository-aware risk scoring
- target-file and dependency-aware routing signals
- broader test/config sensitivity detection
- improved compatibility handling for newer Codex App Server versions
- additional installer validation
- broader platform support
- improved runtime observability
- expanded public regression coverage

The project intentionally avoids adding complexity solely for feature count.

---

# License

CX2 is licensed under the:

[Apache License 2.0](LICENSE)

See also:

[NOTICE](NOTICE)

---

# Disclaimer

CX2 is an independent, unofficial community project.
It is not affiliated with, endorsed by, sponsored by, or maintained by OpenAI.

OpenAI and Codex are referenced only to describe compatibility with the OpenAI Codex ecosystem.
