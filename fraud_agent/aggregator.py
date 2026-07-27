"""Combines subagent findings into a single composite score.

Plain Python, not an LLM call — scoring should be auditable and reproducible.
Includes a hard override: a compliance hit shouldn't be averaged down by a
clean risk-analysis finding.
"""
from __future__ import annotations

from fraud_agent.state import log_entry

WEIGHTS = {
    "risk_analysis": 0.65,
    "compliance": 0.35,
}

COMPLIANCE_HARD_OVERRIDE_SCORE = 90


def aggregator_node(state: dict) -> dict:
    findings = state.get("findings", {})

    if not findings:
        return {
            "risk_score": 0.0,
            "risk_factors": [],
            "audit_trail": [log_entry("aggregator", "No findings to aggregate.")],
        }

    # A subagent that failed after retries (see subagents.py) reports
    # score=None -- exclude it from the weighted average rather than let it
    # silently count as "no risk," but still surface it to the analyst.
    usable = {name: f for name, f in findings.items() if f.get("score") is not None}
    unavailable = [name for name in findings if name not in usable]

    compliance_score = usable.get("compliance", {}).get("score")
    if compliance_score is not None and compliance_score >= COMPLIANCE_HARD_OVERRIDE_SCORE:
        score = 100.0
        detail = "Compliance hard override — composite forced to 100."
    elif not usable:
        # Zero usable signal -- never auto-approve or auto-decline on no
        # information. Force the case into human review instead.
        score = 50.0
        detail = "All subagents failed -- forcing to human review."
    else:
        total_weight = sum(WEIGHTS[name] for name in usable)
        score = sum(WEIGHTS[name] * f["score"] for name, f in usable.items()) / total_weight
        detail = f"Weighted composite across {len(usable)} agent(s)."
        if unavailable:
            detail += f" ({len(unavailable)} unavailable: {', '.join(unavailable)})"

    risk_factors = [
        {
            "agent": name,
            "score": f.get("score"),
            "reason": f.get("reason", ""),
            "citations": f.get("citations", []),
        }
        for name, f in findings.items()
    ]
    risk_factors.sort(key=lambda x: -1 if x["score"] is None else x["score"], reverse=True)

    score = round(score, 1)
    return {
        "risk_score": score,
        "risk_factors": risk_factors,
        "audit_trail": [log_entry("aggregator", f"{detail} Composite score = {score}/100.")],
    }
