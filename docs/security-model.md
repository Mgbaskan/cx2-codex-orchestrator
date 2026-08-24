# Security Model

## External Authentication Isolation

CX2 does not store or manage credentials. It communicates with local OpenAI Codex services using the user's existing authenticated Codex session.

## Subprocess Execution Safety

All child commands executed during turns are invoked with explicit argument arrays and isolated environment boundaries.

## Verification Gate Enforcement

Verification evidence requires exit code 0 from legitimate test execution without masking operators.

## Fail-Closed Mutation Authorization

Under Windows Codex 0.144.4 compatibility mode, effective execution operates in `read-only` sandbox with `approval_policy = "on-request"`. Any file mutation or command execution outside read-only bounds requires explicit one-shot user approval. User decline is fail-closed and preserves the filesystem.

## No Global Host Fallback

Under no circumstances does CX2 automatically downgrade or escalate a failed sandbox into unrestricted host execution (`dangerFullAccess`).

## Bounded Approval Escalation

Interactive approval escalation attempts are bounded per turn (maximum 6 prompts per turn) to prevent approval-loop denial of service or terminal starvation.

## Bounded Verification Execution

When a verification command (e.g. `npm test`, `pytest`) fails under the Codex read-only sandbox due to legitimate temporary, cache, or build write requirements (`SANDBOX_DENIED`, `WORKSPACE_WRITE_REQUIRED`, `TEMP_CACHE_UNAVAILABLE`), CX2 does not automatically execute host commands or elevate the model turn's permissions.

Instead, CX2 offers one-shot bounded verification execution:
- **Exact Command & CWD Display**: The user is presented with the exact command string and working directory.
- **Explicit Interactive Approval**: Execution requires affirmative user selection (`[1] Bu kez izin ver`). Any other input or decline (`[3] Reddet`) fails closed without executing the command.
- **One-Shot Scope**: Authorization is valid only for that single command execution instance and does not grant persistent or session-wide privileges.
- **Model Sandbox Isolation**: The model's active turn remains in `:read-only` mode. `dangerFullAccess` is never used.
- **Failure Precedence Over Permission Noise**: Conclusive test, lint, typecheck, or build failures take precedence over sandbox/permission noise. Commands with genuine failure evidence are marked `FAILED` and are never eligible for bounded host execution offers.
- **Fail-Closed Late Evidence Authorization Barrier**: The `item/completed` event serves as the authoritative decision point for bounded-verification host offers. Late stream deltas arriving after `item/completed` may update telemetry and audit classifications but strictly fail closed and cannot reopen authorization or create new host-execution offers.
- **Bounded Command Diagnostic Retention**: Child command output and diagnostic streams retain a bounded head+tail window (first 64 KiB + most recent 448 KiB, retaining at most 512 KiB per command), empirically exercised with child output streams up to 100 MB per command.
- **Full Process-Tree Termination**: On timeout, all child and descendant processes are forcefully terminated via `taskkill /F /T /PID`.
- **Shared Cache Invariant**: CX2's bounded-verification path does not permission-alter or permanently redirect the shared `.codex-agent-cache` directory. Ordinary Codex-managed cache behavior remains unchanged.

### Residual Risk

When the user approves bounded verification execution, the approved command runs on the host outside the Codex read-only filesystem sandbox under a disposable execution environment profile. Commands in local projects may contain arbitrary build or test script logic. The security boundary relies on explicit human authorization of the exact command and working directory.
