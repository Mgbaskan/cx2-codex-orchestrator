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
- **No False Failure Masking**: Conclusive test, lint, typecheck, or build failures are never eligible for bounded host execution.
- **Bounded Output Capture**: Child process stdout and stderr streams are drained concurrently to prevent pipe deadlocks, retaining at most 512 KiB per stream (empirically validated with child output volumes up to 100 MB per stream).
- **Full Process-Tree Termination**: On timeout, all child and descendant processes are forcefully terminated via `taskkill /F /T /PID`.
- **Shared Cache Invariant**: The shared `.codex-agent-cache` directory is never modified or permission-altered.

### Residual Risk

When the user approves bounded verification execution, the approved command runs on the host outside the Codex read-only filesystem sandbox under a disposable execution environment profile. Commands in local projects may contain arbitrary build or test script logic. The security boundary relies on explicit human authorization of the exact command and working directory.
