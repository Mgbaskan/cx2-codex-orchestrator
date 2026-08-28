from __future__ import annotations

import hashlib
import json
from pathlib import Path


def offline_install_health(cx_home: Path) -> tuple[bool, str]:
    """Verify the installer-managed surface without network/account access."""

    root = Path(cx_home).resolve()
    manifest_path = root / "runtime" / "cx2" / "managed-files.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"managed manifest unavailable: {type(exc).__name__}: {exc}"
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or not hashes:
        return False, "managed manifest contains no hashes"
    for relative, expected in hashes.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            return False, "managed manifest has an invalid hash entry"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False, f"managed path escapes installation root: {relative}"
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except Exception as exc:
            return False, f"managed file unavailable: {relative}: {type(exc).__name__}"
        if actual.casefold() != expected.casefold():
            return False, f"managed file hash mismatch: {relative}"
    return True, f"managed source {manifest.get('version', 'unknown')} verified"


__all__ = ["offline_install_health"]
