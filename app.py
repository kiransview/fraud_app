"""HuggingFace Space entry point: a Gradio demo for the Sentinel fraud-review
multi-agent pipeline (see fraud_agent/graph.py).

This is a second front end for the same graph the FastAPI dashboard
(ui/server.py) drives -- built for the Gradio SDK's request/response model
instead of Server-Sent Events, so "live" pipeline progress here comes from
Gradio's native support for generator functions that `yield` repeatedly
(each yield re-renders the output components), not a persistent stream.

Requires a TOGETHER_API_KEY -- set it as a Space secret (Settings -> Variables
and secrets), never commit it.
"""
from __future__ import annotations

import uuid

import gradio as gr
from dotenv import load_dotenv

load_dotenv()

from langgraph.types import Command  # noqa: E402
from data.sample_transactions import SAMPLE_CASES  # noqa: E402
from fraud_agent.graph import graph  # noqa: E402

NODE_LABELS = {
    "supervisor": "Supervisor",
    "risk_analysis": "Risk Analysis",
    "compliance": "Compliance",
    "aggregator": "Risk Aggregator",
    "decision": "Decision Router",
    "human_review": "Analyst Decision",
    "narrative": "Narrative Agent",
}

BAND_LABELS = [(24, "Low"), (49, "Watch"), (74, "Elevated"), (101, "Critical")]


def _band(score: float) -> str:
    for ceiling, label in BAND_LABELS:
        if score <= ceiling:
            return label
    return "Critical"


def _apply_update(state: dict, update: dict) -> None:
    if "findings" in update:
        state.setdefault("findings", {}).update(update["findings"])
    for key in ("route", "risk_score", "risk_factors", "decision", "narrative"):
        if key in update:
            state[key] = update[key]


def _render_result(state: dict) -> str:
    score = state.get("risk_score")
    lines = []
    if score is not None:
        lines.append(f"### Composite score: {score}/100 ({_band(score)})")
        lines.append(f"**Decision:** {state.get('decision') or 'pending'}")
    else:
        lines.append("### Awaiting composite score…")

    factors = state.get("risk_factors") or [
        {"agent": k, **v} for k, v in (state.get("findings") or {}).items()
    ]
    if factors:
        lines.append("\n#### Risk factors")
        for f in sorted(factors, key=lambda x: (-1 if x.get("score") is None else x["score"]), reverse=True):
            label = NODE_LABELS.get(f.get("agent"), f.get("agent"))
            lines.append(f"\n**{label}** — score {f.get('score', 'n/a')}  \n{f.get('reason', '')}")
            for c in f.get("citations", []):
                lines.append(f"- *{c['label']}* — _{c['source']}_  \n  {c['excerpt']}")

    if state.get("narrative"):
        lines.append(f"\n#### Analyst narrative\n{state['narrative']}")

    return "\n".join(lines)


def run_case(
    amount, currency, merchant, category, channel, geo, home_geo,
    device_id, device_new, ip_address, new_payee,
    account_id, name, prior_flag_count,
):
    thread_id = f"space-{uuid.uuid4().hex[:10]}"
    case = {
        "transaction": {
            "id": thread_id,
            "amount": float(amount or 0),
            "currency": currency,
            "merchant": merchant,
            "category": category,
            "channel": channel,
            "geo": geo,
            "home_geo": home_geo,
            "device_id": device_id,
            "device_new": bool(device_new),
            "ip_address": ip_address,
            "new_payee": bool(new_payee),
        },
        "customer_profile": {
            "account_id": account_id,
            "name": name,
            "prior_flag_count": int(prior_flag_count or 0),
        },
    }
    config = {"configurable": {"thread_id": thread_id}}

    log_lines: list[str] = []
    state: dict = {}

    for step in graph.stream(case, config=config, stream_mode="updates"):
        for node_name, update in step.items():
            if node_name == "__interrupt__":
                log_lines.append("**Decision Router** — escalated, awaiting analyst decision")
                yield (
                    "\n\n".join(log_lines),
                    _render_result(state),
                    gr.update(visible=True), gr.update(visible=True),
                    thread_id,
                )
                continue
            _apply_update(state, update)
            detail = (update.get("audit_trail") or [{}])[-1].get("detail", "")
            log_lines.append(f"**{NODE_LABELS.get(node_name, node_name)}** — {detail}")
            yield (
                "\n\n".join(log_lines),
                _render_result(state),
                gr.update(visible=False), gr.update(visible=False),
                thread_id,
            )


def resume_case(thread_id: str, decision: str):
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(Command(resume=decision), config=config)
    return _render_result(result), gr.update(visible=False), gr.update(visible=False)


EXAMPLES = [
    [
        c["transaction"]["amount"], c["transaction"]["currency"], c["transaction"]["merchant"],
        c["transaction"]["category"], c["transaction"]["channel"], c["transaction"]["geo"],
        c["transaction"]["home_geo"], c["transaction"]["device_id"], c["transaction"]["device_new"],
        c["transaction"]["ip_address"], c["transaction"]["new_payee"],
        c["customer_profile"]["account_id"], c["customer_profile"]["name"],
        c["customer_profile"]["prior_flag_count"],
    ]
    for c in SAMPLE_CASES
]

with gr.Blocks(title="Sentinel — Fraud Review Multi-Agent Demo") as demo:
    gr.Markdown(
        "# Sentinel — payment fraud multi-agent demo\n"
        "A LangGraph pipeline: a deterministic supervisor routes each transaction to "
        "**Risk Analysis** (grounded in a real fraud dataset) and/or **Compliance** "
        "(real OFAC sanctions list + real FATF jurisdictions + real BSA structuring "
        "rules), aggregates their scores, and auto-approves, auto-declines, or "
        "escalates to a human. Pick an example below or fill in your own transaction."
    )

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### Transaction")
            amount = gr.Number(label="Amount", value=100.0)
            currency = gr.Dropdown(["USD", "EUR", "GBP", "CAD", "INR"], value="USD", label="Currency")
            merchant = gr.Textbox(label="Merchant / payee")
            category = gr.Dropdown(
                ["electronics", "grocery", "travel", "dining", "wire", "jewelry", "other"],
                value="electronics", label="Category",
            )
            channel = gr.Dropdown(
                ["ecommerce", "card_present", "wire_transfer"], value="ecommerce", label="Channel",
            )
            geo = gr.Textbox(label="Origin location", placeholder="e.g. Lagos, NG")
            home_geo = gr.Textbox(label="Account home location", placeholder="e.g. Columbus, OH")
            device_id = gr.Textbox(label="Device ID", value="dev-demo-1")
            device_new = gr.Checkbox(label="Device is new / unrecognized")
            ip_address = gr.Textbox(label="IP address", value="203.0.113.1")
            new_payee = gr.Checkbox(label="New payee")

            gr.Markdown("### Customer profile")
            account_id = gr.Textbox(label="Account ID", value="acct-demo-1")
            name = gr.Textbox(label="Account holder / business name")
            prior_flag_count = gr.Number(label="Prior fraud flags on this account", value=0, precision=0)

            run_btn = gr.Button("Run review", variant="primary")

            gr.Examples(
                examples=EXAMPLES,
                inputs=[
                    amount, currency, merchant, category, channel, geo, home_geo,
                    device_id, device_new, ip_address, new_payee,
                    account_id, name, prior_flag_count,
                ],
                label="Sample cases (fast-path, escalate, full-review)",
            )

        with gr.Column(scale=1):
            gr.Markdown("### Agent pipeline (live)")
            pipeline_log = gr.Markdown()
            result_md = gr.Markdown()
            with gr.Row():
                approve_btn = gr.Button("Approve", visible=False)
                decline_btn = gr.Button("Decline", visible=False)
            thread_id_state = gr.State("")

    run_btn.click(
        run_case,
        inputs=[
            amount, currency, merchant, category, channel, geo, home_geo,
            device_id, device_new, ip_address, new_payee,
            account_id, name, prior_flag_count,
        ],
        outputs=[pipeline_log, result_md, approve_btn, decline_btn, thread_id_state],
    )
    approve_btn.click(
        lambda tid: resume_case(tid, "approve"),
        inputs=[thread_id_state],
        outputs=[result_md, approve_btn, decline_btn],
    )
    decline_btn.click(
        lambda tid: resume_case(tid, "decline"),
        inputs=[thread_id_state],
        outputs=[result_md, approve_btn, decline_btn],
    )

if __name__ == "__main__":
    demo.queue().launch()
