from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from cx_home import resolve_cx_home

CX_HOME = resolve_cx_home()
PRODUCTION_SRC = CX_HOME / "src"


if str(PRODUCTION_SRC) not in sys.path:
    sys.path.insert(
        0,
        str(PRODUCTION_SRC),
    )


import cx as production_cx

from codex_compat import resolve_sandbox_compatibility


EXPECTED_ROUTER_VERSION = "1.2.2"


class RouterAdapterError(
    RuntimeError
):
    pass


def production_version() -> str:

    value = getattr(
        production_cx,
        "ROUTER_VERSION",
        None,
    )

    if not isinstance(
        value,
        str,
    ):
        raise RouterAdapterError(
            "Production ROUTER_VERSION yok."
        )

    return value


def permission_profile_for_sandbox(
    sandbox: str,
) -> str:

    mapping = {
        "read-only":
            ":read-only",

        "workspace-write":
            ":workspace",
    }

    try:
        return mapping[
            sandbox
        ]

    except KeyError as exc:
        raise RouterAdapterError(
            f"Unsupported sandbox: {sandbox!r}"
        ) from exc


def build_route(
    prompt: str,
    cwd: Path,
) -> dict[str, Any]:

    version = (
        production_version()
    )

    if version != EXPECTED_ROUTER_VERSION:
        raise RouterAdapterError(
            "Production router version mismatch: "
            f"{version!r}"
        )

    policy = (
        production_cx.load_policy()
    )

    repo = (
        production_cx.detect_repo(
            cwd.resolve()
        )
    )

    route = (
        production_cx.classify(
            prompt,
            repo,
            policy,
        )
    )

    visible_models = (
        production_cx.cached_visible_models()
    )

    if not visible_models:
        raise RouterAdapterError(
            "Cached model catalog boş."
        )

    model = (
        production_cx.choose_model(
            route["tier"],
            visible_models,
            policy,
        )
    )

    sandbox = route.get(
        "sandbox"
    )

    if not isinstance(
        sandbox,
        str,
    ):
        raise RouterAdapterError(
            "Sandbox route yok."
        )

    compat_decision = (
        resolve_sandbox_compatibility(
            sandbox
        )
    )

    effective_sandbox = (
        compat_decision.effective_sandbox
    )

    permissions = (
        permission_profile_for_sandbox(
            effective_sandbox
        )
    )

    reasoning = route.get(
        "reasoning"
    )

    if not isinstance(
        reasoning,
        str,
    ) or not reasoning:
        raise RouterAdapterError(
            "Reasoning route yok."
        )

    root = repo.get(
        "root"
    )

    if not isinstance(
        root,
        str,
    ) or not root:
        raise RouterAdapterError(
            "Repo root yok."
        )

    return {
        "router_version":
            version,

        "prompt":
            prompt,

        "repo":
            repo,

        "score":
            route.get(
                "score"
            ),

        "tier":
            route.get(
                "tier"
            ),

        "model":
            model,

        "reasoning_for_turn":
            reasoning,

        # Keep original router decision for
        # telemetry / diagnostics.
        "sandbox":
            sandbox,

        "requested_sandbox":
            sandbox,

        "effective_sandbox":
            effective_sandbox,

        "sandbox_compatibility_mode":
            compat_decision.compatibility_mode,

        "sandbox_degraded":
            compat_decision.degraded,

        "sandbox_compatibility_reason":
            compat_decision.reason,

        # App Server execution profile.
        "permissions":
            permissions,

        "mutating":
            bool(
                route.get(
                    "mutating"
                )
            ),

        "reasons":
            route.get(
                "reasons",
                [],
            ),

        "thread": {
            "model":
                model,

            "cwd":
                root,

            "runtimeWorkspaceRoots": [
                root,
            ],

            "approvalPolicy":
                "never",

            # IMPORTANT:
            # legacy "sandbox" intentionally omitted.
            "permissions":
                permissions,

            "ephemeral":
                True,
        },

        "turn": {
            "effort":
                reasoning,
        },

        "budget_guard_applied":
            False,
    }
