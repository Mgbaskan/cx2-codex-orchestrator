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

## Required Verification Contract

When users specify required quality gates in their prompt under an explicit verification heading (e.g. `QUALITY GATES`, `REQUIRED VERIFICATION`, `VERIFICATION GATES`, `CHECKS TO RUN`, `DOĞRULAMA KAPILARI`), CX2 activates the Required Verification Contract:

1. **Deterministic Gate Extraction**: Gates are extracted directly from the prompt text without model calls. Concrete executable command lines are parsed conservatively; vague prose instructions (e.g. "run all tests") do not create required gates.
2. **Execution Ledger Matching**: Actual commands executed by the Codex App Server are matched against required gates by:
   - **Command Identity**: Script names, test flags, and subcommands.
   - **Surface / CWD Isolation**: Working directory provenance is strictly enforced. An execution in `backend` (e.g. `npm run build`) does not satisfy a `web` gate (`npm run build`).
3. **Upper-Bound Assurance**: A task can only achieve `VERIFIED` status if all required gates evaluate to `ALL_PASSED`. Missing, failed, or blocked gates prevent false verification.
4. **Non-Authoritative Model Prose**: Assistant messages asserting success (e.g. "I ran all tests and they passed") cannot satisfy required gates. Only observed App Server command executions count as verification evidence.
5. **No Blind Host Auto-Execution**: CX2 does not execute arbitrary prompt command text on the host outside the active model turn. The model performs required commands within its standard sandbox and approval contract.

### Required Verification Badge

When required gates are present:

```text
[doğrulama] · VERIFIED · zorunlu 8/8 kapı geçti
```

When required gates are partially completed or missing:

```text
[doğrulama] · UNVERIFIED · zorunlu 4/8 kapı (4 eksik)
```

## Masked Command Guard

Commands that attempt to mask failure status codes (e.g. `npm test || true` or `npm test ; exit 0`) are detected and rejected as valid verification evidence.
