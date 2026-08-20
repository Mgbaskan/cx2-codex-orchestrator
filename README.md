# CX2 — Intelligent Orchestration and Terminal UX Layer for OpenAI Codex

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](https://microsoft.com/windows)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Release](https://img.shields.io/badge/release-v2.0.5-green.svg)](https://github.com/Mgbaskan/cx2-codex-orchestrator/releases/tag/v2.0.5)

**CX2** is an opinionated orchestration and terminal UX layer for OpenAI Codex. It provides deterministic task/risk routing, quota-aware budget guards, persistent multi-turn Git sessions, process-local numeric thread selection, post-mutation verification assurance, and a compact, noise-free terminal interface.

---

> [!NOTE]
> **Disclaimer & Trademark Notice**
> CX2 is an independent, unofficial community project. It is **not** affiliated with, endorsed by, or maintained by OpenAI. "OpenAI" and "Codex" are trademarks of OpenAI, Inc. and are used herein solely for descriptive and compatibility identification purposes.

---

## What is CX2?

CX2 operates as an intelligent supervisory layer between the developer's terminal and the OpenAI Codex runtime. Rather than sending every prompt to a single model with static parameters, CX2 dynamically evaluates task complexity, risk level, and remaining quota to select optimal model tiers, reasoning effort, and sandbox permissions.

```
+-------------------------------------------------------------------+
|                           Developer                               |
|                         (Terminal / CLI)                          |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|                           CX2 Layer                               |
|  - Task & Risk Classifier     - Quota & Budget Guard              |
|  - Process-Local Selector     - Single-Line Turn Header           |
|  - Post-Mutation Verification - Localized Approvals               |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|                      OpenAI Codex Runtime                         |
|                    (App Server / Execution)                       |
+-------------------------------------------------------------------+
```

---

## Key Features

- **Intelligent Task & Risk Routing**: Automatically classifies prompts into `routine`, `standard`, or `deep` tiers, adjusting model selection (`gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`), reasoning effort (`low`, `medium`, `high`), and sandbox permissions (`read-only`, `workspace-write`).
- **Quota-Aware Budget Guard**: Continuously monitors remaining Codex rate limits and transitions between `NORMAL`, `CONSERVE`, `CRITICAL`, and `EMERGENCY` budget modes to prevent mid-session quota exhaustion.
- **Process-Local Numeric Thread Selection**: Sequential aliases (`[1]`, `[2]`, `[3]`) for `/history` and `/search` listings allow rapid thread navigation (`/resume 1`, `/thread 2`) without copying 36-character UUIDs.
- **Compact Terminal UX**: Single-line turn metadata header (`[cx] RESUME · gpt-5.6-luna · low · read-only · 27% kaldı · CONSERVE`), semantic PowerShell command unwrapping, and clean tool-to-assistant transitions.
- **Automated Verification Gate**: Evaluates post-mutation test commands and presents concise verification badges (`[doğrulama] VERIFIED · 1 dosya · npm test · 0.1s`).
- **External Authentication Boundary**: Reuses your existing local OpenAI Codex authentication. CX2 never manages, stores, or transmits external API keys or credentials.
- **Zero-Model Overhead**: All routing, history parsing, numeric selection, and presentation formatting are 100% deterministic and execute with zero model inference calls.

---

## Requirements

- **Operating System**: Windows 10 / 11 (PowerShell 5.1+)
- **Python**: Python 3.10+ (Python 3.12 validated)
- **Codex**: Valid OpenAI Codex environment and local authentication
- **Git**: Git for Windows (for repository session tracking)

---

## Installation

Clone the repository and run the automated installer:

```powershell
git clone https://github.com/Mgbaskan/cx2-codex-orchestrator.git
cd cx2-codex-orchestrator
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

The installer will:
1. Create `~/.cx` directory structure.
2. Set up a dedicated Python virtual environment.
3. Install pinned runtime dependencies.
4. Compile the native Windows launcher (`cx.exe`).
5. Add `~/.cx/bin` to your User `PATH`.
6. Run `cx --doctor` self-check.

---

## Quick Start

Launch CX2 interactively in any Git repository:

```powershell
cx
```

Or execute a one-shot task:

```powershell
cx "Explain the authentication flow in src/auth.ts"
```

### Interactive Commands

| Command | Description |
|:---|:---|
| `/help` | Display grouped interactive help |
| `/history` | List recent threads with numeric selectors (`[1]`, `[2]`, ...) |
| `/search <query>` | Search thread history with numeric aliases |
| `/thread [id\|no]` | Display detailed thread metadata |
| `/turns [id\|no]` | Display thread turn history |
| `/resume [id\|no]` | Resume an existing thread in the current workspace |
| `/new` | Reset current session binding and start fresh |
| `/quota` | Display current live Codex quota status |
| `/doctor` | Run comprehensive runtime diagnostics |
| `/exit` | Exit interactive mode |

---

## Documentation

- [Architecture Overview](docs/architecture.md)
- [Installation Guide](docs/installation.md)
- [Configuration & Policy](docs/configuration.md)
- [Routing Model](docs/routing-model.md)
- [Terminal UX Design](docs/terminal-ux.md)
- [Verification Assurance Gate](docs/verification-assurance.md)
- [Command Reference](docs/commands.md)
- [Security Model](docs/security-model.md)
- [Compatibility](docs/compatibility.md)
- [Known Limitations](docs/known-limitations.md)

---

## License

This project is licensed under the [Apache License 2.0](LICENSE).
