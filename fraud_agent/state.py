"""Shared state schema passed between every node in the fraud-review graph."""
import operator
import time
from typing import Annotated, Literal, TypedDict


def merge_findings(left: dict, right: dict) -> dict:
    """Reducer for parallel subagent writes.

    Each subagent writes to `findings` under its own key (e.g. "identity",
    "network"), so a plain dict merge is safe — no two subagents ever
    collide on the same key, which is what LangGraph requires when multiple
    parallel branches write to the same state field.
    """
    return {**left, **right}


def log_entry(node: str, detail: str) -> dict:
    """One timestamped line for `audit_trail` -- this is what the UI's
    Agent Log tab renders, so every node's log line should read like a
    sentence a fraud analyst could point to and explain, not a raw dump."""
    return {"node": node, "detail": detail, "time": time.time()}


class FraudCaseState(TypedDict, total=False):
    transaction: dict
    customer_profile: dict
    route: list[str]                             # subagents the supervisor chose to invoke
    findings: Annotated[dict, merge_findings]     # keyed by subagent name
    risk_score: float
    risk_factors: list[dict]
    decision: Literal["approve", "decline", "escalate"]
    narrative: str
    audit_trail: Annotated[list[dict], operator.add]
