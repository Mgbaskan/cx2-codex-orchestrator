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
