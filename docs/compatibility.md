# Compatibility

## Supported Environments

- **Platform**: Windows 10, Windows 11
- **Shell**: PowerShell 5.1, PowerShell 7+, Windows Terminal, Command Prompt
- **Python**: Python >= 3.10 enforced (Python 3.10, 3.11, 3.12 supported; 3.12 validated)
- **Codex**: OpenAI Codex 0.144.4 validated baseline

## Codex Compatibility Model

CX2 utilizes a centralized, model-free compatibility abstraction (`runtime/cx2/codex_compat.py`) to manage interactions with the Codex CLI and App Server runtime.

### Version Classifications

- **PINNED / VALIDATED**: `0.144.4` (the exact baseline version CX2 is tested and validated against in development).
- **DETECTED**: The runtime version identified from the configured Codex CLI executable (`<codex> --version`).
- **UNVERIFIED**: A detected Codex CLI version that differs from the validated baseline (e.g. `0.148.0-alpha.9`). CX2 continues execution where core App Server contracts are intact, logging diagnostic warnings rather than hard-failing.
- **INCOMPATIBLE**: A missing executable, missing core package, or major contract break that prevents core App Server operations (`initialize`, `thread/start`, `turn/start`).

### Feature-Specific Degradation

A failure or incompatibility in an optional capability does not disable the entire CX2 runtime:

- **Core App Server**: If `0.144.4` or compatible, core session, turn processing, and streaming remain `SUPPORTED`.
- **Windows Workspace-Write Sandbox (Codex 0.144.4)**: On the qualified Windows environment with Codex CLI 0.144.4, native `workspaceWrite` sandbox initialization reproduced a deterministic pre-command hang. CX2 decouples the requested router sandbox from effective execution:
  - `requested_sandbox`: remains `workspace-write` in the route manifest (preserving truthful task classification for policy and verification).
  - `effective_sandbox`: resolved to `read-only` compatibility mode.
  - `approval_policy`: remains `on-request`.
  - Inspections, searches, and reads run sandboxed without friction. Actual file mutations prompt the user for explicit one-shot approval (`item/fileChange/requestApproval` or `item/commandExecution/requestApproval`).
  - In CX2 2.0.10, verification commands blocked solely by sandbox write restrictions (e.g. `SANDBOX_DENIED`, `WORKSPACE_WRITE_REQUIRED`, `TEMP_CACHE_UNAVAILABLE`) are recovered via explicit one-shot bounded verification execution with transparent command/CWD prompts, while the active model turn remains in read-only sandbox mode.
  - `cx doctor` reports `windows_workspace_write: DEGRADED` (`windows_0.144.4_workspace_write_degraded`).
- **Native Thread Deletion**: On newer Codex state schemas (v42+ where `agent_jobs` is dropped), native delete is safely degraded to fail-closed (`SUPPORTED_WITH_DEGRADATION`). CX2 never mutates or downgrades the shared SQLite schema to force compatibility; `/archive` is recommended instead.

## Non-Windows Platforms

Linux and macOS environments are not officially supported or tested in current releases.
