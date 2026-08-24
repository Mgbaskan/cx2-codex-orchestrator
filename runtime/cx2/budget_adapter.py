from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from cx_home import resolve_cx_home

CX_HOME = resolve_cx_home()
STAGE = CX_HOME / "runtime" / "cx2"
PRODUCTION_SRC = CX_HOME / "src"


for path in (
    str(STAGE),
    str(PRODUCTION_SRC),
):
    if path not in sys.path:
        sys.path.insert(
            0,
            path,
        )


import cx as production_cx

from client import (
    AppServerClient,
)

from router_adapter import (
    build_route,
)


EXPECTED_ROUTER_VERSION = "1.2.2"


class BudgetAdapterError(
    RuntimeError
):
    pass


# =========================================================
# App Server -> production quota compatibility bridge
# =========================================================

class _RawClientBridge:

    def __init__(
        self,
        app_server: AppServerClient,
    ) -> None:

        self.app_server = app_server

    def _request_raw(
        self,
        method: str,
        params: Any,
    ) -> Any:

        # account/rateLimits/read has undefined params in
        # App Server protocol. Production SDK historically
        # passes {}, so normalize it here without changing
        # production read_quota_snapshot().
        if (
            method
            == "account/rateLimits/read"
        ):
            return self.app_server.request(
                method,
                timeout=15.0,
            )

        return self.app_server.request(
            method,
            params,
            timeout=15.0,
        )


class _CodexQuotaBridge:

    def __init__(
        self,
        app_server: AppServerClient,
    ) -> None:

        self._client = (
            _RawClientBridge(
                app_server
            )
        )


# =========================================================
# Production compatibility
# =========================================================

def _policy() -> dict[str, Any]:

    version = getattr(
        production_cx,
        "ROUTER_VERSION",
        None,
    )

    if version != EXPECTED_ROUTER_VERSION:
        raise BudgetAdapterError(
            "Production router version mismatch: "
            f"{version!r}"
        )

    return (
        production_cx.load_policy()
    )


def read_live_quota(
    app_server: AppServerClient,
) -> dict[str, Any]:

    policy = _policy()

    bridge = (
        _CodexQuotaBridge(
            app_server
        )
    )

    quota = (
        production_cx.read_quota_snapshot(
            bridge,
            policy,
        )
    )

    if not isinstance(
        quota,
        dict,
    ):
        raise BudgetAdapterError(
            "Quota snapshot object değil."
        )

    return quota


# =========================================================
# Budget execution plan
# =========================================================

def build_execution_plan(
    prompt: str,
    cwd: Path,
    quota: dict[str, Any],
) -> dict[str, Any]:

    policy = _policy()

    route = build_route(
        prompt,
        cwd,
    )

    start_tier = route[
        "tier"
    ]

    base_chain = (
        production_cx.escalation_chain(
            start_tier,
            policy,
        )
    )

    state = str(
        quota.get(
            "state",
            "unknown",
        )
    )

    # Exact production execute_prompt semantics:
    #
    # quota reached -> return before model turn.
    if state == "reached":

        return {
            "route":
                route,

            "quota":
                quota,

            "blocked":
                True,

            "block_reason":
                "quota_or_spend_limit_reached",

            "base_chain":
                base_chain,

            "guarded_chain":
                [],

            "attempts":
                [],

            "budget_guard_applied":
                True,
        }

    guarded_chain = (
        production_cx.budget_guard_chain(
            start_tier,
            base_chain,
            quota,
            policy,
        )
    )

    visible_models = (
        production_cx.cached_visible_models()
    )

    if not visible_models:
        raise BudgetAdapterError(
            "Cached model catalog boş."
        )

    attempts = []

    for tier in guarded_chain:

        model = (
            production_cx.choose_model(
                tier,
                visible_models,
                policy,
            )
        )

        reasoning = (
            policy[
                "reasoning"
            ][tier]
        )

        attempts.append(
            {
                "tier":
                    tier,

                "model":
                    model,

                "reasoning":
                    reasoning,

                # Permission decision is task intent,
                # not escalation tier dependent.
                "permissions":
                    route[
                        "permissions"
                    ],
            }
        )

    return {
        "route":
            route,

        "quota":
            quota,

        "blocked":
            False,

        "block_reason":
            None,

        "base_chain":
            base_chain,

        "guarded_chain":
            guarded_chain,

        "attempts":
            attempts,

        "budget_guard_applied":
            True,
    }
