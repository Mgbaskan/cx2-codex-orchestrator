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
| 2.0.9 | :white_check_mark: |
| 2.0.8 | :white_check_mark: |
| < 2.0.8 | :x: |

## Security Invariants & Guarantees

- **Fail-Closed Mutation Authorization**: Under Windows Codex 0.144.4 compatibility mode, effective execution operates in `read-only` sandbox with `approval_policy = "on-request"`. Any file mutation or command execution outside read-only bounds requires explicit one-shot user approval. User decline is fail-closed and preserves the filesystem.
- **No Global Host Fallback**: Under no circumstances does CX2 automatically downgrade or escalate a failed sandbox into unrestricted host execution (`dangerFullAccess`).
- **Bounded Approval Escalation**: Interactive approval escalation attempts are bounded per turn to prevent approval-loop denial of service or terminal starvation.
- **Evidence-Based Gate Verification**: Verification status (`VERIFIED`) strictly requires observed zero exit codes from legitimate test execution without masking operators. Approvals and prose do not substitute for empirical execution evidence.
