"""Deterministic triage.

The supervisor is plain Python, not an LLM call — it decides which subagents
are worth running for a given transaction, instead of always paying for both.
This is what makes a fast-path possible: a small, routine transaction on a
known device only needs the Risk Analysis check, while anything unusual
(new device, new payee, or a prior flag on the account) also gets the
Compliance check.
"""
from __future__ import annotations

from fraud_agent.state import log_entry

FULL_SUITE = ["risk_analysis", "compliance"]
FAST_PATH = ["risk_analysis"]

FAST_PATH_MAX_AMOUNT = 250.0


def decide_route(state: dict) -> list[str]:
    txn = state["transaction"]
    profile = state.get("customer_profile", {})

    amount = txn.get("amount", 0)
    is_new_device = txn.get("device_new", False)
    is_new_payee = txn.get("new_payee", False)
    prior_flags = profile.get("prior_flag_count", 0)

    routine = (
        amount <= FAST_PATH_MAX_AMOUNT
        and not is_new_device
        and not is_new_payee
        and prior_flags == 0
    )
    return FAST_PATH if routine else FULL_SUITE


def supervisor_node(state: dict) -> dict:
    route = decide_route(state)
    path_label = "fast-path" if route == FAST_PATH else "full review"
    detail = f"Routed to: {', '.join(route)} ({path_label})"
    return {
        "route": route,
        "audit_trail": [log_entry("supervisor", detail)],
    }
