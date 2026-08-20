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
| 2.0.5 | :white_check_mark: |
| < 2.0.5 | :x: |
