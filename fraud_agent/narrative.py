"""Final LLM call: turns the numeric findings into an analyst-readable case
note. This is the only node whose whole job is language, not scoring.
"""
from __future__ import annotations

from langchain_together import ChatTogether

from fraud_agent.state import log_entry

MODEL_NAME = "openai/gpt-oss-120b"

_PROMPT = """You are writing a short case note for a human fraud analyst. \
Given the transaction and the risk factors below, write 2-4 sentences \
explaining what happened and why the system reached its decision. Be \
specific and factual -- no hedging filler, no repeating the raw numbers \
verbatim.

Transaction: {transaction}
Composite risk score: {risk_score}/100
Decision: {decision}
Risk factors: {risk_factors}
"""

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = ChatTogether(model=MODEL_NAME, temperature=0.3)
    return _model


def narrative_node(state: dict) -> dict:
    prompt = _PROMPT.format(
        transaction=state["transaction"],
        risk_score=state["risk_score"],
        decision=state["decision"],
        risk_factors=state["risk_factors"],
    )
    response = _get_model().invoke(prompt)
    return {
        "narrative": response.content,
        "audit_trail": [log_entry("narrative", "Generated the analyst-facing case note.")],
    }
