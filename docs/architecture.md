# CX2 Architecture

CX2 is structured into decoupled supervisory and presentation components.

## Component Overview

1. **Native Launcher (`launcher/cx-launcher.cs`)**:
   - Compiled to `~/.cx/bin/cx.exe`.
   - Bypasses `cmd.exe` argument truncation and preserves embedded multiline arguments.
   - Dispatches to `runtime/cx2/cx2_cli.py` within the dedicated virtual environment.

2. **Core Router (`src/cx.py` & `runtime/cx2/router_adapter.py`)**:
   - Classifies task complexity (`routine`, `standard`, `deep`).
   - Evaluates write intent and mutation safety.
   - Selects model tier, reasoning effort, and sandbox permissions.

3. **Budget Guard (`runtime/cx2/budget_adapter.py`)**:
   - Reads quota telemetry from Codex.
   - Implements quota conservation thresholds (`NORMAL`, `CONSERVE`, `CRITICAL`, `EMERGENCY`).

4. **Numeric Selection Layer (`runtime/cx2/selection_context.py`)**:
   - In-memory, process-local mapping of `[1]..[N]` aliases to native 36-character Codex thread IDs.
   - Invalidation on thread mutation or zero-result searches.

5. **Terminal Presentation (`runtime/cx2/terminal_ui.py`)**:
   - Single-line turn metadata header.
   - Semantic PowerShell wrapper unwrap.
   - Single-line verification badge.
   - Localized Turkish action choices mapped to protocol tokens.

6. **Verification Gate (`runtime/cx2/verification_gate.py`)**:
   - Post-mutation file classification and test command evaluation.
   - Masked command detection (`cmd || true`, `cmd ; exit 0`).

7. **Codex Compatibility Layer (`runtime/cx2/codex_compat.py`)**:
   - Centralized semantic version parsing and capability evaluation for Codex CLI and App Server.
   - Validated baseline pinning (`0.144.4`) with non-fatal degradation for unverified newer runtimes.
   - Fail-closed native delete protection against modified SQLite state schemas without mutating user state.
   - Structured diagnostic summaries for `/doctor`.
