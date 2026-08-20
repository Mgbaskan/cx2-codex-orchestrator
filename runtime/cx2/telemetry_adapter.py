from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any


CX_HOME = Path.home() / ".cx"
PRODUCTION_SRC = CX_HOME / "src"

if str(PRODUCTION_SRC) not in sys.path:
    sys.path.insert(
        0,
        str(PRODUCTION_SRC),
    )


import cx as production_cx


EXPECTED_ROUTER_VERSION = "1.1.3"


class TelemetryAdapterError(
    RuntimeError
):
    pass


def _check_version() -> None:

    version = getattr(
        production_cx,
        "ROUTER_VERSION",
        None,
    )

    if version != EXPECTED_ROUTER_VERSION:
        raise TelemetryAdapterError(
            "Production router version mismatch: "
            f"{version!r}"
        )


def normalize_token_usage(
    token_usage: Any,
) -> dict[str, Any]:

    _check_version()

    if not isinstance(
        token_usage,
        dict,
    ):
        raise TelemetryAdapterError(
            "App Server tokenUsage object değil."
        )

    # Preserve the exact App Server payload.
    # Production helpers remain responsible for semantics.
    return token_usage


# =============================================================
# Session context
#
# IMPORTANT:
# Do NOT reproduce the formula here.
#
# Production usage_context_info() is the sole source of truth:
#
#   tokens  = usage.last.totalTokens
#   window  = usage.modelContextWindow
#   percent = tokens / window * 100
# =============================================================

def context_info_from_token_usage(
    token_usage: Any,
) -> dict[str, Any]:

    usage = normalize_token_usage(
        token_usage
    )

    result_like = SimpleNamespace(
        usage=usage
    )

    context = production_cx.usage_context_info(
        result_like
    )

    if not isinstance(
        context,
        dict,
    ):
        raise TelemetryAdapterError(
            "usage_context_info() dict dondurmedi."
        )

    return context


# =============================================================
# Per-turn counters
#
# Production value_from() intentionally uses usage.last rather
# than cumulative usage.total.
# =============================================================

def turn_counters_from_token_usage(
    token_usage: Any,
) -> dict[str, int]:

    usage = normalize_token_usage(
        token_usage
    )

    return {
        "input_tokens":
            production_cx.value_from(
                usage,
                "inputTokens",
                "input_tokens",
            ),

        "cached_input_tokens":
            production_cx.value_from(
                usage,
                "cachedInputTokens",
                "cached_input_tokens",
            ),

        "output_tokens":
            production_cx.value_from(
                usage,
                "outputTokens",
                "output_tokens",
            ),

        "reasoning_output_tokens":
            production_cx.value_from(
                usage,
                "reasoningOutputTokens",
                "reasoning_output_tokens",
            ),
    }


# =============================================================
# StreamingTurnRunner result adapter
#
# turn_runner.py exposes:
#
#   result.token_usage
#
# SDK production exposes:
#
#   result.usage
#
# This is the ONLY representation bridge required.
# =============================================================

def context_info_from_turn_result(
    turn_result: Any,
) -> dict[str, Any]:

    token_usage = getattr(
        turn_result,
        "token_usage",
        None,
    )

    return context_info_from_token_usage(
        token_usage
    )


def turn_counters_from_turn_result(
    turn_result: Any,
) -> dict[str, int]:

    token_usage = getattr(
        turn_result,
        "token_usage",
        None,
    )

    return turn_counters_from_token_usage(
        token_usage
    )


__all__ = [
    "EXPECTED_ROUTER_VERSION",
    "context_info_from_token_usage",
    "context_info_from_turn_result",
    "normalize_token_usage",
    "turn_counters_from_token_usage",
    "turn_counters_from_turn_result",
]
