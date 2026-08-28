# Command Reference

## CLI Execution Options

- `cx [prompt]`: Execute one-shot turn with positional prompt text.
- `cx --version`: Print the CX2 CLI/runtime and Router release identity without initializing the runtime or App Server.
- `cx --help` / `cx -h`: Print CLI usage without initializing the runtime or App Server.
- `cx --prompt-file <path>`: Execute one-shot turn loading full prompt from file.
- `cx --stdin`: Read one-shot turn prompt from standard input (PowerShell / shell pipe).
- `cx --route <prompt>`: Zero-model local deterministic routing preview for text prompt.
- `cx --route-file <path>`: Zero-model local deterministic routing preview for prompt file.
- `cx --file <path>`: Attach/mention a reference file context to the turn.
- `cx --doctor`: Run comprehensive environment and configuration diagnostics.
- `cx --doctor-offline`: Installer/internal structural hash check; performs no account/model availability check.
- `cx --stats`: Print local token telemetry statistics.
- `cx --quota`: Fetch and print a last-known rate-limit/quota snapshot.
- `cx --session`: Print current workspace session binding.

## Interactive Commands

- `/help`: Display grouped command reference.
- `/paste`: Enter multiline paste mode (`.send` on empty line to submit, `.cancel` to discard).
- `/history [filter]`: List recent threads with numeric selector aliases.
- `/search <query>`: Search conversation history with numeric aliases.
- `/thread [id|no]`: Show thread metadata and turn counts.
- `/turns [id|no]`: Show turn-by-turn history of a thread.
- `/resume [id|no]`: Bind the specified thread as the active session.
- `/rename [id|no] <name>`: Rename a thread.
- `/archive [id|no]`: Archive a thread.
- `/unarchive [id|no]`: Restore an archived thread.
- `/delete [id|no]`: Permanently delete a thread (requires confirmation).
- `/new`: Reset active workspace session binding.
- `/quota`: Explicitly refresh and show the last-known Codex quota snapshot.
- `/last [--page]`: Show the latest visible assistant response for the current safe thread/workspace context, optionally in the built-in pager.
- `/transcript clear`: Confirm and delete visible transcript rows for the current workspace.
- `/trace`: Show the bounded memory-only tool trace for the previous completed turn, including available CX-owned protocol/queue/classification/render timings. These timings do not claim visibility into Codex sandbox internals.
- `/stats`: Show local token telemetry summary.
- `/doctor`: Run CX2 runtime self-checks.
- `/clear`: Clear terminal display.
- `/exit`: Exit interactive shell.
