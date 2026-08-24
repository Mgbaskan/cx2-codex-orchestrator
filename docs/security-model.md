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

Interactive approval escalation attempts are bounded per turn to prevent approval-loop denial of service or terminal starvation.
