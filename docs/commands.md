# Command Reference

## CLI Execution Options

- `cx [prompt]`: Execute one-shot turn with positional prompt text.
- `cx --prompt-file <path>`: Execute one-shot turn loading full prompt from file.
- `cx --stdin`: Read one-shot turn prompt from standard input (PowerShell / shell pipe).
- `cx --route <prompt>`: Zero-model local deterministic routing preview for text prompt.
- `cx --route-file <path>`: Zero-model local deterministic routing preview for prompt file.
- `cx --file <path>`: Attach/mention a reference file context to the turn.
- `cx --doctor`: Run comprehensive environment and configuration diagnostics.
- `cx --stats`: Print local token telemetry statistics.
- `cx --quota`: Print live rate limit and quota information.
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
- `/quota`: Show current live Codex quota status.
- `/stats`: Show local token telemetry summary.
- `/doctor`: Run CX2 runtime self-checks.
- `/clear`: Clear terminal display.
- `/exit`: Exit interactive shell.
