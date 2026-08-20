# Known Limitations

1. **PowerShell 5.1 Argument Parsing**: Upstream console argument parsing may strip quotes in certain complex multiline argv patterns before reaching the launcher.
2. **Native Thread Deletion on Migrated Schemas**: On newer Codex state schemas (v42+), pinned Codex 0.144.4 may safely refuse native deletion; `/archive` is recommended instead.
3. **Non-Git Workspaces**: Thread session persistence is process-local in non-git directories.
4. **Repository Move / Rename**: Moving or renaming a repository directory initiates a new session binding.
5. **CCE Acceleration**: Code Context Engine integration is experimental and disabled by default (`cce.enabled: false`).
