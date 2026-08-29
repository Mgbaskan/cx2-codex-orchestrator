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

## CX2 2.0.15 terminal contract

The interactive prompt maintains deterministic visual separation:
```text
[cx] <status>

CX>
```
Exactly one blank visual row separates the sticky status line from the input prompt, with the prompt starting at column 0. While blocking input owns the terminal, asynchronous status redraws are deferred to protect live typed user input from corruption. Once advanced into prompt mode, status parked in scrollback is not treated as cursor-addressable. Narrow terminal status text is cell-width bounded to prevent line-wrapping.

TTY capability is split by feature: `NO_COLOR` disables colour but does not
disable cursor control or the sticky status row. `TERM=dumb`, redirected
streams, or `CX2_STATIC_UI=1` select static presentation. Sticky status,
spinner, commands, approvals, responses and the pager use explicit current-row
ownership; leaving the pager or an approval restores the prior eligible status.

All model/command text crosses a presentation-only control sanitizer. Newline
and tab retain their structural meaning; CR, ESC, BEL, C0/C1 controls, CSI and
OSC sequences are shown as inert ASCII escapes. CX-generated ANSI remains
trusted and canonical transcript text remains unchanged.

Visible canonical assistant responses are retained separately at
`CX_HOME/data/visible-transcript.sqlite3`. The runtime stores UTF-8 chunks as
they arrive (64 KiB flush target), retains at most 16 MiB per response, 200
completed responses and 64 MiB of logical retained payload, and prunes records
older than 30 days. The database is local plaintext. Raw reasoning, commentary
items and App Server payloads are never copied into it. A failed transcript
database produces a bounded warning and does not fail the turn.
The 16 MiB boundary applies only to durable retention. Live response rendering
continues beyond it; the retained row is marked truncated while final-answer
identity and exact equality remain deterministic through bounded UTF-8
length/digest accounting.

`/last` shows the latest response for the current safe thread/workspace
context, including `partial`, `failed`, `interrupted` or `truncated` state.
`/last --page` uses the built-in read-only pager and falls back to complete
plain output when no interactive terminal is available. Its controls are
Up/Down, PgUp/PgDn, Home/End, Space, Enter, `b`, `q`, Esc and Ctrl+C.
`/transcript clear`
requires an explicit interactive confirmation and only deletes transcript rows.

File-write “for this session” approval is limited to ordinary create/edit/patch
operations under the exact current workspace, and is held only in memory for
the current runtime/thread. It is cleared by `/new`, a thread or workspace
change, App Server restart, runtime replacement or CLI exit. It never covers
shell/host execution, privileged permissions, destructive operations, another
workspace or another process. Host execution remains a separate bounded,
one-shot approval.

`/trace` and `/trace last` show a bounded projection of the authoritative
command ledger: exact command and CWD, status, exit code, duration,
classification, host-execution flag, and bounded output/truncation evidence.
The trace is memory-only, contains at most 64 commands from the previous
completed turn, and is cleared on `/new`, App Server/runtime replacement or CLI
exit. Oversized projected fields carry explicit retained/dropped-byte notices;
trace does not survive restart.
Quota is a pre-turn snapshot (or explicit `/quota` refresh), not a live feed;
the static header and sticky line report captured age (or `age unknown`) and show `? · unavailable` when no
value exists. Matching token-usage events update context. Cursor/status UI,
colour, Unicode glyphs and pager raw mode all have safe fallbacks for
`NO_COLOR`, `TERM=dumb`, redirected streams, narrow consoles and screen-reader
or static usage. Non-TTY output is deterministic plain text with no ANSI,
spinner, cursor control or interactive approval prompt; required approvals
decline fail-closed with an explicit diagnostic.

Multiline `/paste` keeps `.send`, `.cancel`, escaped sentinels, UTF-8 and the
1 MiB limit unchanged. The byte limit is enforced as lines are acquired, and
the accepted line/character count does not log pasted content. The terminal's
normal input echo remains active, so `/paste` is not a secret/no-echo editor.

Markdown presentation is intentionally small: headings, bold, inline and fenced
code, simple lists, blockquotes, links and separators are formatted. Tables,
nested block parsing and malformed or unfinished delimiters remain literal text.
Formatting never changes the canonical response saved by the transcript store.
An unfinished presentation line is bounded to 64 KiB; larger newline-free text
is emitted in safe literal chunks. Pager wrapping is near-linear and pages are
produced lazily from compact source spans. Advanced grapheme clusters beyond
combining-mark/common-wide-character handling may still wrap imperfectly.
