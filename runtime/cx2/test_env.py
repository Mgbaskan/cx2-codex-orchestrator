from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid


class ExecutionEnvironmentError(RuntimeError):
    """Raised when an unsafe temporary execution environment operation is attempted."""
    pass


@dataclass
class ExecutionEnvironmentProfile:
    """Represents an isolated, disposable external temporary execution environment."""
    temp_root: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    created_dirs: list[Path] = field(default_factory=list)
    _cleaned: bool = False

    def cleanup(self) -> None:
        """Safely clean up the disposable temporary execution environment."""
        if self._cleaned:
            return

        resolved_root = self.temp_root.resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()

        # Strict safety guards to prevent accidental deletion of important directories
        try:
            is_in_temp = resolved_root.is_relative_to(system_temp)
        except (ValueError, AttributeError):
            is_in_temp = False

        if not is_in_temp:
            raise ExecutionEnvironmentError(
                f"Refusing to delete temp root outside system temp: {resolved_root}"
            )

        if resolved_root == system_temp:
            raise ExecutionEnvironmentError(
                f"Refusing to delete system temp root directly: {resolved_root}"
            )

        user_home = Path.home().resolve()
        if resolved_root == user_home or resolved_root in user_home.parents:
            raise ExecutionEnvironmentError(
                f"Refusing to delete path overlapping user home: {resolved_root}"
            )

        if not resolved_root.name.startswith("cx2-test-"):
            raise ExecutionEnvironmentError(
                f"Refusing to delete temp root with unexpected prefix: {resolved_root.name}"
            )

        if resolved_root.exists():
            shutil.rmtree(resolved_root, ignore_errors=True)

        self._cleaned = True


def build_test_environment(
    base_env: dict[str, str] | None = None,
    prefix: str = "cx2-test-",
) -> ExecutionEnvironmentProfile:
    """
    Build a deterministic, isolated, collision-safe test execution environment.

    Configures external writable temp and cache directories for Python, Go,
    and Node toolchains so that read-only workspaces are never mutated during
    verification and test execution.
    """
    system_temp = Path(tempfile.gettempdir()).resolve()
    unique_id = uuid.uuid4().hex[:12]
    temp_root = system_temp / f"{prefix}{unique_id}"
    temp_root.mkdir(parents=True, exist_ok=True)

    # Subdirectories for individual ecosystems
    tmp_dir = temp_root / "tmp"
    pycache_dir = temp_root / "pycache"
    gocache_dir = temp_root / "gocache"
    gotmp_dir = temp_root / "gotmp"
    npm_cache_dir = temp_root / "npm-cache"

    created_dirs = [tmp_dir, pycache_dir, gocache_dir, gotmp_dir, npm_cache_dir]
    for d in created_dirs:
        d.mkdir(parents=True, exist_ok=True)

    overrides: dict[str, str] = {
        # General process temp directories (Windows compatibility)
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "CX_TEST_TEMP_ROOT": str(temp_root),

        # Python: prevent bytecode writing to read-only workspace; redirect any cache
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(pycache_dir),

        # Go: redirect compilation build cache, disable telemetry for offline determinism
        "GOCACHE": str(gocache_dir),
        "GOTELEMETRY": "off",
        "GOTMPDIR": str(gotmp_dir),

        # Node / npm: redirect package cache away from global/workspace state
        "npm_config_cache": str(npm_cache_dir),
    }

    if base_env is not None:
        merged = dict(base_env)
        merged.update(overrides)
        env_dict = merged
    else:
        env_dict = overrides

    return ExecutionEnvironmentProfile(
        temp_root=temp_root,
        env_overrides=env_dict,
        created_dirs=created_dirs,
    )


class TestExecutionEnvironment:
    """Context manager for disposable test execution environments."""

    def __init__(
        self,
        base_env: dict[str, str] | None = None,
        prefix: str = "cx2-test-",
    ):
        self.base_env = base_env
        self.prefix = prefix
        self.profile: ExecutionEnvironmentProfile | None = None

    def __enter__(self) -> ExecutionEnvironmentProfile:
        self.profile = build_test_environment(self.base_env, self.prefix)
        return self.profile

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.profile is not None:
            self.profile.cleanup()
