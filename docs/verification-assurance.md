# Verification Assurance Gate

When code mutations occur, CX2 automatically inspects executed verification commands to validate changes.

## Verification Badge

Successful validation renders a compact badge:
```
[doğrulama] VERIFIED · 1 dosya (src/auth.ts) · npm test · 0.1s
```

Failed validation displays explicit exit status:
```
[doğrulama] BAŞARISIZ · npm test · exit 1 · 1.4s
```

## Masked Command Guard

Commands that attempt to mask failure status codes (e.g. `npm test || true` or `npm test ; exit 0`) are detected and rejected as valid verification evidence.
