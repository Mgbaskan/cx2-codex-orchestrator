# Security Policy

## Reporting Security Issues

We take the security of CX2 seriously. If you discover a potential security vulnerability, please report it through **GitHub Private Vulnerability Reporting** via the repository Security tab.

Please **do not** report security vulnerabilities via public GitHub issues or discussions.

## Scope of Security Concerns

We are particularly interested in reports concerning:
- Sandboxing or permission bypasses
- Execution of unapproved or unintended shell commands
- Local privilege escalation or path traversal risks
- Accidental exposure or leakage of sensitive user data

## What NEVER to Include in Reports

When submitting a security report or diagnostic logs:
- **NEVER** include OpenAI Codex authentication tokens or `auth.json`
- **NEVER** include API keys or bearer tokens
- **NEVER** include proprietary or private source code from your local repositories
- **NEVER** include confidential session or conversation databases

## Supported Versions

| Version | Supported |
|:---|:---:|
| 2.0.13 | :white_check_mark: |
| 2.0.12 | :white_check_mark: |
| 2.0.11 | :white_check_mark: |
| 2.0.10 | :white_check_mark: |
| 2.0.9 | :white_check_mark: |
| < 2.0.9 | :x: |

## Security Invariants & Guarantees

- **Fail-Closed Mutation Authorization**: Under Windows Codex 0.144.4 compatibility mode, effective execution operates in `read-only` sandbox with `approval_policy = "on-request"`. Authorization is explicit; user decline is fail-closed and preserves the filesystem.
- **Scoped Ordinary File-Write Grant**: A user may remember ordinary create/edit/patch approval only for the current runtime instance, App Server thread and canonical workspace root. The in-memory grant cannot authorize unresolved or outside-workspace paths, destructive changes, shell/host execution, privilege escalation, `dangerFullAccess`, or another thread/workspace/runtime/process.
- **Visible Transcript Privacy**: The bounded local plaintext transcript database retains canonical visible assistant response text and approved lifecycle metadata only. Raw reasoning, hidden chain-of-thought, commentary, raw protocol payloads, command output and approval secrets are not copied into it; `/transcript clear` provides confirmed workspace-scoped deletion.
- **Explicit Bounded Verification Authorization**: When verification commands are blocked by sandbox write restrictions (e.g. `SANDBOX_DENIED`, `WORKSPACE_WRITE_REQUIRED`, `TEMP_CACHE_UNAVAILABLE`), CX2 does not automatically execute host commands. One-shot execution requires explicit affirmative user approval (`[1] Bu kez izin ver`). The exact command string and working directory are presented to the user. User decline is fail-closed.
- **Model Sandbox Invariant**: Bounded verification authorization applies strictly one-shot to the single command instance approved. The active model turn remains in `:read-only` sandbox throughout execution. `dangerFullAccess` is never used.
- **Failure Precedence Over Permission Noise**: Conclusive test, lint, typecheck, or build failures take precedence over sandbox/permission noise. Commands with genuine failure evidence are marked `FAILED` and are never eligible for bounded host execution offers.
- **Fail-Closed Late Evidence Authorization Gate**: The `item/completed` event serves as the authoritative decision point for bounded-verification host offers. Late stream deltas arriving after `item/completed` may update telemetry and audit classifications but strictly fail closed and cannot reopen authorization or create new host-execution offers.
- **No Global Host Fallback**: Under no circumstances does CX2 automatically downgrade or escalate a failed sandbox into unrestricted host execution (`dangerFullAccess`).
- **Bounded Approval Escalation**: Interactive approval escalation attempts are bounded per turn (maximum 6 prompts per turn) to prevent approval-loop denial of service or terminal starvation.
- **Evidence-Based Gate Verification**: Verification status (`VERIFIED`) strictly requires observed zero exit codes from legitimate test execution without masking operators. Approvals and prose do not substitute for empirical execution evidence.
- **Bounded Command Diagnostic Retention**: Command output and diagnostic streams retain a bounded head+tail window (first 64 KiB + most recent 448 KiB, retaining at most 512 KiB per command), empirically exercised with child output streams up to 100 MB per command.
- **Process-Tree Termination on Timeout**: Command timeouts forcefully terminate the complete process hierarchy to prevent orphaned background processes.
- **Shared Cache Protection**: CX2's bounded-verification path does not permission-alter or permanently redirect the shared `.codex-agent-cache` directory. Ordinary Codex-managed cache behavior remains unchanged.

## Residual Risks

When the user explicitly approves bounded verification execution, the approved command executes on the host outside the Codex read-only filesystem sandbox under a disposable environment profile. A command named `test`, `build`, or `lint` is not inherently safe and may execute project-defined shell commands.

**Mitigations:**
- Exact command and exact working directory are explicitly displayed before execution.
- Prompts require active user selection; invalid input defaults to fail-closed decline.
- One-shot scope: approvals never grant session-wide permission.
- Turn approval circuit breaker prevents repeated automated prompt attacks.
- Complete process-tree cleanup on timeout.
