# Security Model

## External Authentication Isolation

CX2 does not store or manage credentials. It communicates with local OpenAI Codex services using the user's existing authenticated Codex session.

## Subprocess Execution Safety

All child commands executed during turns are invoked with explicit argument arrays and isolated environment boundaries.

## Verification Gate Enforcement

Verification evidence requires exit code 0 from legitimate test execution without masking operators.
