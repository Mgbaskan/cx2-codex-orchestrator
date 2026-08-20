# Contributing to CX2

Thank you for your interest in contributing to CX2!

CX2 is an opinionated orchestration and terminal UX layer for OpenAI Codex. We welcome contributions that improve reliability, performance, developer experience, and terminal presentation.

## Core Rules

1. **Zero-Secret Policy**: Never commit credentials, tokens, `auth.json`, personal paths, or private project artifacts.
2. **Frozen Release Integrity**: Once a version is tagged and released (e.g., `v2.0.5`), its production source files are frozen. All bug fixes and enhancements must target the next release milestone (`2.0.6+`).
3. **Deterministic Testing**: All new features and bug fixes must include deterministic, zero-model unit tests.
4. **Preserve External Auth Boundary**: CX2 reuses the user's existing Codex authentication. Never introduce code that manages, stores, or transmits external credentials.

## Development Workflow

1. Fork the repository and create a feature branch (`feature/my-improvement`).
2. Make your changes and write unit tests in `tests/`.
3. Run the test suite:
   ```powershell
   python -m unittest discover -s tests
   ```
4. Submit a Pull Request describing your changes and verification steps.
