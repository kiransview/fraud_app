"""Wires every node into the executable fraud-review graph.

    START -> supervisor -> [fan out to chosen subagents in parallel]
          -> aggregator -> approve | decline | human_review -> narrative -> END

Building/importing this module never requires an API key -- the ChatTogether
model instances are only constructed lazily, the first time a subagent or the
narrative node actually runs. Only `graph.invoke(...)` needs one.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from fraud_agent.aggregator import aggregator_node
from fraud_agent.narrative import narrative_node
from fraud_agent.state import FraudCaseState, log_entry
from fraud_agent.subagents import SUBAGENT_SPECS, make_subagent_node
from fraud_agent.supervisor import supervisor_node

APPROVE_MAX = 25   # risk_score <= this -> auto-approve
DECLINE_MIN = 90   # risk_score >= this -> auto-decline
                   # anything in between -> escalate to a human analyst


def fan_out(state: FraudCaseState):
    """Reads the supervisor's chosen route and dispatches each subagent as
    its own parallel branch. All branches converge on "aggregator" once
    every dispatched agent has written its finding."""
    return [Send(name, state) for name in state["route"]]


def decide(state: FraudCaseState) -> str:
    score = state["risk_score"]
    if score >= DECLINE_MIN:
        return "decline"
    if score <= APPROVE_MAX:
        return "approve"
    return "escalate"


def approve_node(state: FraudCaseState) -> dict:
    detail = f"Composite score {state['risk_score']}/100 is at or below the auto-approve threshold ({APPROVE_MAX}) -- auto-approved."
    return {"decision": "approve", "audit_trail": [log_entry("decision", detail)]}


def decline_node(state: FraudCaseState) -> dict:
    detail = f"Composite score {state['risk_score']}/100 is at or above the auto-decline threshold ({DECLINE_MIN}) -- auto-declined."
    return {"decision": "decline", "audit_trail": [log_entry("decision", detail)]}


def human_review_node(state: FraudCaseState) -> dict:
    """Pauses the graph durably and waits for an analyst's decision.

    Resume from the dashboard/API with:
        graph.invoke(Command(resume="approve"), config=config)
    using the same `config["configurable"]["thread_id"]` the case was
    started with -- the MemorySaver checkpointer (swap for Postgres/Redis in
    production) is what makes the pause durable across process restarts.
    """
    analyst_decision = interrupt({
        "reason": "escalated_for_review",
        "transaction": state["transaction"],
        "risk_score": state["risk_score"],
        "risk_factors": state["risk_factors"],
    })
    detail = f"Analyst reviewed the escalated case and chose: {analyst_decision}."
    return {
        "decision": analyst_decision,
        "audit_trail": [log_entry("human_review", detail)],
    }


def build_graph():
    g = StateGraph(FraudCaseState)

    g.add_node("supervisor", supervisor_node)
    for name in SUBAGENT_SPECS:
        g.add_node(name, make_subagent_node(name))
    g.add_node("aggregator", aggregator_node)
    g.add_node("approve", approve_node)
    g.add_node("decline", decline_node)
    g.add_node("human_review", human_review_node)
    g.add_node("narrative", narrative_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", fan_out, list(SUBAGENT_SPECS))
    for name in SUBAGENT_SPECS:
        g.add_edge(name, "aggregator")

    g.add_conditional_edges("aggregator", decide, {
        "approve": "approve",
        "decline": "decline",
        "escalate": "human_review",
    })

    g.add_edge("approve", END)
    g.add_edge("decline", "narrative")
    g.add_edge("human_review", "narrative")
    g.add_edge("narrative", END)

    return g.compile(checkpointer=MemorySaver())


graph = build_graph()
