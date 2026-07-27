"""Each subagent is a LangChain tool-calling agent: a system prompt + a model
+ a fixed set of tools, built with LangGraph's prebuilt `create_react_agent`.
LangGraph's job is only to call `node()` and merge the result back into
shared state — the reasoning happens inside the agent itself.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from langchain_together import ChatTogether
from langgraph.prebuilt import create_react_agent

from fraud_agent import tools
from fraud_agent.state import log_entry

MODEL_NAME = "openai/gpt-oss-120b"

_COMPLIANCE_POLICY = (
    Path(__file__).resolve().parent.parent / "data" / "reference" / "compliance_policy.md"
).read_text(encoding="utf-8")

# gpt-oss-120b via Together occasionally: (a) returns a transient 500, or
# (b) ends its turn with something other than the requested JSON (e.g. it
# echoes a tool's arguments instead of a final score/reason). Both are
# retried before falling back to a flagged, non-crashing result -- one
# flaky subagent shouldn't take down the whole case.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2
# Retries at temperature=0 would just reproduce the same broken completion --
# nudge the sampling up on each retry so a retry can actually behave
# differently instead of deterministically repeating the same failure.
RETRY_TEMPERATURES = [0.0, 0.4, 0.7]

_FINDING_INSTRUCTIONS = (
    "Call your evidence-gathering tool exactly once. Then, using only what "
    "it returned, send your final message containing ONLY a single JSON "
    "object -- no markdown, no code fences, no restating the tool name or "
    'its arguments -- in exactly this shape: {"score": 37, "reason": "one '
    'sentence"}. score is 0-100: how much this evidence raises fraud risk '
    "(0 = no concern, 100 = certain fraud)."
)

SUBAGENT_SPECS = {
    "risk_analysis": {
        "prompt": (
            "You are a fraud subagent specializing in transaction risk analysis. "
            "Your evidence tool compares this amount against a real reference "
            "dataset of confirmed-fraud and legitimate transactions (not a "
            "fabricated baseline), plus recent activity, travel plausibility, "
            "device/IP reputation, and linked-account signals. Weigh the "
            "observed fraud rate for this amount range and how extreme the "
            "percentile rank is, alongside the other signals. "
            + _FINDING_INSTRUCTIONS
        ),
        "tools": [tools.gather_risk_evidence],
    },
    "compliance": {
        "prompt": (
            "You are a fraud subagent specializing in compliance. Screen the "
            "transaction against the policy below -- when a specific policy "
            "section drives your score, name it (e.g. 'per policy section 2, "
            "structuring').\n\n"
            f"{_COMPLIANCE_POLICY}\n\n"
            + _FINDING_INSTRUCTIONS
        ),
        "tools": [tools.gather_compliance_evidence],
    },
}

_agent_cache: dict = {}


def _get_agent(name: str, temperature: float):
    """Agents are built lazily (on first use) and cached per (name,
    temperature) pair, so importing this module never requires an API key —
    only actually running a case does."""
    key = (name, temperature)
    if key not in _agent_cache:
        spec = SUBAGENT_SPECS[name]
        _agent_cache[key] = create_react_agent(
            model=ChatTogether(model=MODEL_NAME, temperature=temperature),
            tools=spec["tools"],
            prompt=spec["prompt"],
        )
    return _agent_cache[key]


def _extract_json(text: str) -> dict:
    """Extract the first complete, balanced JSON object from `text`.

    Open-weight models (gpt-oss-120b included) sometimes append trailing
    prose or repeat the answer after the JSON. A greedy "first { to last }"
    regex swallows that trailing content and produces invalid JSON, so this
    walks the string tracking brace depth (ignoring braces inside quoted
    strings) and stops at the first object that actually balances.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in subagent output: {text!r}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError(f"Unterminated JSON object in subagent output: {text!r}")


def _is_valid_finding(finding: object) -> bool:
    return (
        isinstance(finding, dict)
        and isinstance(finding.get("score"), (int, float))
        and 0 <= finding["score"] <= 100
        and isinstance(finding.get("reason"), str)
        and finding["reason"].strip() != ""
    )


def _run_agent_once(agent, user_msg: str) -> dict:
    result = agent.invoke({"messages": [("user", user_msg)]})
    return _extract_json(result["messages"][-1].content)


def _build_citations(name: str, txn: dict, profile: dict) -> list[dict]:
    """Computed the same deterministic way as the evidence itself -- not
    written by the LLM, so it can't misquote or invent a source. Attached
    to the finding regardless of whether the LLM's own summary succeeded,
    since it shows what evidence was actually gathered either way."""
    if name == "risk_analysis":
        return tools.explain_risk_evidence(
            account_id=profile.get("account_id", ""),
            amount=txn.get("amount", 0),
            category=txn.get("category", ""),
            geo=txn.get("geo", ""),
            home_geo=txn.get("home_geo", ""),
            device_id=txn.get("device_id", ""),
            ip_address=txn.get("ip_address", ""),
        )
    if name == "compliance":
        return tools.explain_compliance_evidence(
            name=profile.get("name", ""),
            amount=txn.get("amount", 0),
            geo=txn.get("geo", ""),
        )
    return []


def make_subagent_node(name: str):
    """Returns a LangGraph node function bound to one named subagent."""

    def node(state: dict) -> dict:
        txn = state["transaction"]
        profile = state.get("customer_profile", {})
        user_msg = (
            f"Transaction: {json.dumps(txn)}\n"
            f"Customer profile: {json.dumps(profile)}"
        )

        finding = None
        last_error: Exception | str | None = None
        for attempt in range(MAX_ATTEMPTS):
            temperature = RETRY_TEMPERATURES[min(attempt, len(RETRY_TEMPERATURES) - 1)]
            agent = _get_agent(name, temperature)
            try:
                candidate = _run_agent_once(agent, user_msg)
            except Exception as exc:  # transient API errors (e.g. Together 5xx)
                last_error = exc
            else:
                if _is_valid_finding(candidate):
                    finding = candidate
                    break
                last_error = f"malformed finding: {candidate!r}"
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        if finding is None:
            # Degrade gracefully: surface the failure to the analyst instead
            # of crashing the whole case over one flaky subagent. score=None
            # tells the aggregator to exclude this agent from the weighted
            # average rather than silently treating it as "no risk."
            finding = {
                "score": None,
                "reason": f"Analysis unavailable after {MAX_ATTEMPTS} attempts ({last_error}).",
            }

        finding["citations"] = _build_citations(name, txn, profile)

        score_label = "n/a" if finding["score"] is None else finding["score"]
        detail = f"Score {score_label} -- {finding['reason']}"
        return {
            "findings": {name: finding},
            "audit_trail": [log_entry(name, detail)],
        }

    return node
