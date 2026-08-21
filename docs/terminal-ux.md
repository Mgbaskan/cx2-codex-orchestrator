# Terminal UX Design

CX2 2.0.5 introduces a compact, high-signal presentation layer designed to minimize visual clutter in developer terminals.

## Turn Status Header

Turn metadata is rendered as a single concise line:

```
[cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE
```

## Semantic Shell Command Trace

Raw wrapped commands such as:
```powershell
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Command "npm test"
```
are unwrapped and displayed cleanly:
```
> npm test
[ok] 120ms
```

## Interactive Approvals

Approval prompts provide numbered options with clear Turkish labels mapped to native protocol tokens:
```
[onay] Run command
  npm run build
  [1] Bu kez izin ver | [2] Oturum boyunca izin ver | [3] Reddet | [4] İptal
Seçim [3]:
```

## Verification & Audit Badges

### Post-Mutation Verification
When workspace files are modified, CX2 validates observed evidence and renders a single-line summary:
```text
[doğrulama] VERIFIED · 1 dosya · npm test · 0.4s
[doğrulama] BAŞARISIZ · npm test · exit 1 · 1.3s
[doğrulama] BLOCKED · go test · Access is denied · 0.2s
```

### Read-Only Audit Assurance
During broad read-only inspections, CX2 evaluates verification completeness across executed commands:
```text
[audit] · COMPLETE · 3 checks · 3 passed
[audit] · PARTIAL · 5 checks · 2 passed · 1 blocked
[audit] · UNVERIFIED · 2 checks
```
*Note*: `inconclusive_count` is included in `total_checks` to ensure transparent accounting without producing false failure or passed claims.
