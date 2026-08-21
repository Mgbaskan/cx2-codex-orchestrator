# Verification Assurance Gate

When code mutations occur, CX2 automatically inspects executed verification commands to validate changes. During read-only audits, it assesses overall verification evidence completeness.

## Command Outcome Classification

Each verification-relevant command (`TEST`, `TYPECHECK`, `LINT`, `BUILD`) is deterministically classified:

- **`PASSED`**: Process exited with code 0 and was not masked.
- **`FAILED`**: Command ran and produced definitive project test/lint/build failure evidence (e.g. failing assertion, test suite failure).
- **`BLOCKED`**: Execution was obstructed before or during the run by external environment factors (sandbox denial, `%TEMP%` cache init failure, missing executable).
- **`INTERRUPTED`**: Turn was cancelled or deadline expired.
- **`INCONCLUSIVE`**: Non-zero exit code without definitive project failure evidence or masked exit code (`|| true`).

## Verification Badges

Successful validation:
```text
[doğrulama] VERIFIED · 1 dosya (src/auth.ts) · npm test · 0.1s
```

Failed validation:
```text
[doğrulama] BAŞARISIZ · npm test · exit 1 · 1.4s
```

Blocked validation:
```text
[doğrulama] BLOCKED · go test · Access is denied · 0.2s
```

## Read-Only Audit Assurance

For whole-project and read-only inspections:

- **`COMPLETE`**: All executed checks produced conclusive outcomes (`PASSED` or `FAILED`).
- **`PARTIAL`**: Some checks passed, but others were `BLOCKED` or `INCONCLUSIVE`.
- **`UNVERIFIED`**: No checks were attempted, or all attempted checks were blocked/inconclusive.

```text
[audit] · COMPLETE · 3 checks · 3 passed
[audit] · PARTIAL · 5 checks · 2 passed
```

## Masked Command Guard

Commands that attempt to mask failure status codes (e.g. `npm test || true` or `npm test ; exit 0`) are detected and rejected as valid verification evidence.
