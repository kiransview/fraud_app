"""FastAPI service that exposes the fraud-review LangGraph to the dashboard UI.

    uvicorn ui.server:app --reload --port 8000

Two things make this more than a typical CRUD API:

- POST /api/cases kicks off a real LangGraph run in a background thread and
  returns immediately; the frontend watches progress over
  GET /api/cases/{id}/stream, a Server-Sent Events feed (via sse-starlette)
  that emits one event per node as the graph actually executes it -- this is
  live pipeline telemetry, not a client-side animation.
- An escalated case pauses mid-graph (LangGraph's interrupt()) until
  POST /api/cases/{id}/resume supplies an analyst's decision, then the graph
  resumes from exactly where it paused.

Run from the project root (so the `fraud_agent` and `data` packages import
correctly): `uvicorn ui.server:app --reload`
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_together import ChatTogether

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.sample_transactions import SAMPLE_CASES  # noqa: E402
from fraud_agent.graph import graph  # noqa: E402

app = FastAPI(title="Sentinel Fraud Review API")

# -------------------------------------------------------- intake assistant --
# A separate, small-talk chat model that helps an analyst fill out the intake
# form and understand what the pipeline does with each field. It never scores
# or decides anything itself -- only the real graph run does that.
_ASSISTANT_MODEL_NAME = "openai/gpt-oss-120b"
_assistant_model: ChatTogether | None = None

_ASSISTANT_SYSTEM_PROMPT = """You are the intake assistant for Sentinel, a payment-fraud \
review system. You help a fraud analyst fill out the transaction intake form correctly \
and understand what the automated review pipeline will do with their case. Be concise \
-- a sentence or two per answer unless the analyst asks for more detail.

TRANSACTION FIELDS
- id: unique transaction identifier (e.g. "TXN-12345"). Optional -- auto-generated if left blank.
- amount: the transaction amount, as a number.
- currency: ISO currency code (USD, EUR, GBP, ...).
- merchant: the merchant or payee name.
- category: electronics, grocery, travel, dining, wire, jewelry, or other. This affects \
the behavioral-baseline comparison, since typical spend varies a lot by category.
- channel: ecommerce, card_present, or wire_transfer.
- geo: city/country the transaction originated from.
- home_geo: the account holder's usual home location. The Risk Analysis agent compares \
geo vs home_geo to flag "impossible travel" -- too far, too fast since the last session.
- device_id / device_new: whether this device has been seen on the account before. A new \
device is one of the strongest signals the Risk Analysis agent weighs.
- ip_address: the IP address the transaction came from; screened for proxy/VPN/abuse history.
- new_payee: whether the payee/recipient was just added to the account. Most relevant for \
wire transfers -- a brand-new payee receiving a large wire is a classic mule pattern.

CUSTOMER PROFILE FIELDS
- account_id: the account identifier.
- name: the account holder's name (or business name). Screened against sanctions/PEP lists.
- prior_flag_count: how many times this account has previously been flagged. Any non-zero \
count forces the full review regardless of amount.

HOW THE PIPELINE USES THIS
There are two subagents: **Risk Analysis** (spending patterns, travel plausibility, \
device/IP reputation, linked-account signals) and **Compliance** (sanctions screening \
and business rules).
1. The Supervisor looks at amount, device_new, new_payee, and prior_flag_count to choose \
between running both agents and a cheap fast-path (Risk Analysis only) for routine \
transactions -- currently, amounts of $250 or less on a known device with no new payee \
and no prior flags take the fast-path.
2. Each subagent scores 0-100 based on its own evidence.
3. The Aggregator combines the two scores into a weighted composite score, with a hard \
override: a strong compliance match forces the composite straight to 100.
4. The composite score routes the case: 25 or below auto-approves, 90 or above auto-declines, \
anything in between escalates to a human analyst.

If the analyst's current form values are shared with you, use them to give specific \
feedback (e.g. flag an unusual amount/category combination, or inconsistent device_new / \
new_payee values) rather than only generic explanations. Never invent a risk score or \
decision yourself -- only an actual pipeline run produces that; you're here to help fill \
out the form and understand the process."""


def _get_assistant_model() -> ChatTogether:
    global _assistant_model
    if _assistant_model is None:
        _assistant_model = ChatTogether(model=_ASSISTANT_MODEL_NAME, temperature=0.3)
    return _assistant_model


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn]
    form_snapshot: dict | None = None


@app.post("/api/assistant/chat")
async def assistant_chat(payload: ChatRequest) -> dict:
    system_prompt = _ASSISTANT_SYSTEM_PROMPT
    if payload.form_snapshot:
        system_prompt += (
            "\n\nThe analyst's form currently has these values filled in:\n"
            + json.dumps(payload.form_snapshot, indent=2)
        )

    lc_messages: list[Any] = [SystemMessage(content=system_prompt)]
    for turn in payload.messages:
        if turn.role == "user":
            lc_messages.append(HumanMessage(content=turn.content))
        else:
            lc_messages.append(AIMessage(content=turn.content))

    model = _get_assistant_model()

    def _call() -> str:
        return model.invoke(lc_messages).content

    reply = await run_in_threadpool(_call)
    return {"reply": reply}

STATIC_DIR = Path(__file__).parent / "static"

# The case registry (queue/detail views, and every case's full audit_trail
# log) is persisted to this JSON file and reloaded on startup, so a new
# session still shows past cases and their logs instead of an empty queue.
# LangGraph's own execution checkpointer (fraud_agent/graph.py) is still
# MemorySaver (in-process only) -- see _load_registry() for what that means
# for a case that was mid-run when the server last stopped.
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "case_registry.json"

_registry_lock = threading.Lock()
_registry: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _save_registry_locked() -> None:
    """Writes `_registry` to disk. Caller must already hold `_registry_lock`."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = REGISTRY_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_registry, f)
    os.replace(tmp_path, REGISTRY_PATH)  # atomic replace on both Windows and POSIX


def _load_registry() -> None:
    if not REGISTRY_PATH.exists():
        return
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    with _registry_lock:
        _registry.update(loaded)
        for case in _registry.values():
            # The graph's own execution checkpointer (MemorySaver) is
            # in-process only -- it has zero memory of ANY case from a
            # previous process, including one that reached "escalated" and
            # is just waiting on an analyst. Resuming it would invoke
            # supervisor_node with no state at all (KeyError: 'transaction').
            # So every non-terminal status loaded from a previous process
            # gets marked unresumable here, not just queued/running.
            if case.get("status") in ("queued", "running", "escalated"):
                case["status"] = "interrupted"
                case.setdefault("audit_trail", []).append({
                    "node": "system",
                    "detail": (
                        "Server restarted before this case finished; "
                        "execution state was not durable, so it cannot be "
                        "resumed or approved/declined. Resubmit as a new "
                        "case if needed."
                    ),
                    "time": _now(),
                })
        _save_registry_locked()


_load_registry()


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _new_case_record(case_id: str, transaction: dict, customer_profile: dict) -> dict:
    return {
        "id": case_id,
        "transaction": transaction,
        "customer_profile": customer_profile,
        "status": "queued",  # queued -> running -> escalated | approve | decline | error
        "route": [],
        "findings": {},
        "risk_score": None,
        "risk_factors": [],
        "decision": None,
        "narrative": None,
        "audit_trail": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


class SubmitTransaction(BaseModel):
    transaction: dict
    customer_profile: dict


class ResumeDecision(BaseModel):
    decision: str  # "approve" | "decline"


@app.get("/api/samples")
def get_samples() -> list[dict]:
    return SAMPLE_CASES


# Reference documents a citation might link to (see fraud_agent/tools.py's
# explain_risk_evidence / explain_compliance_evidence). Allowlisted by name
# -- the path comes from a URL, so no arbitrary filesystem access.
_REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
_REFERENCE_MEDIA_TYPES = {
    "compliance_policy.md": "text/markdown",
    "amount_baseline.json": "application/json",
}


@app.get("/api/reference/{filename}")
def get_reference_file(filename: str):
    if filename not in _REFERENCE_MEDIA_TYPES:
        raise HTTPException(404, "not found")
    path = _REFERENCE_DIR / filename
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type=_REFERENCE_MEDIA_TYPES[filename])


@app.get("/api/cases")
def list_cases() -> list[dict]:
    with _registry_lock:
        return sorted(_registry.values(), key=lambda c: c["created_at"], reverse=True)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict:
    with _registry_lock:
        case = _registry.get(case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return case


@app.post("/api/cases")
def submit_case(payload: SubmitTransaction) -> dict:
    case_id = payload.transaction.get("id") or f"TXN-{uuid.uuid4().hex[:10]}"
    transaction = {**payload.transaction, "id": case_id}
    with _registry_lock:
        if case_id in _registry:
            raise HTTPException(409, f"case {case_id} already exists")
        _registry[case_id] = _new_case_record(case_id, transaction, payload.customer_profile)
        _save_registry_locked()
    return {"id": case_id}


def _apply_update(case_id: str, update: dict) -> None:
    with _registry_lock:
        case = _registry[case_id]
        if "findings" in update:
            case["findings"].update(update["findings"])
        if "route" in update:
            case["route"] = update["route"]
        if "risk_score" in update:
            case["risk_score"] = update["risk_score"]
        if "risk_factors" in update:
            case["risk_factors"] = update["risk_factors"]
        if "decision" in update:
            case["decision"] = update["decision"]
        if "narrative" in update:
            case["narrative"] = update["narrative"]
        if "audit_trail" in update:
            case["audit_trail"].extend(update["audit_trail"])
        case["updated_at"] = _now()
        _save_registry_locked()


def _run_graph_in_thread(case_id: str, out_q: "queue.Queue[tuple[str, Any]]") -> None:
    with _registry_lock:
        case = _registry[case_id]
        case["status"] = "running"
        _save_registry_locked()
        initial_state = {
            "transaction": case["transaction"],
            "customer_profile": case["customer_profile"],
        }
    config = {"configurable": {"thread_id": case_id}}

    try:
        for step in graph.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, update in step.items():
                if node_name == "__interrupt__":
                    interrupt_payload = update[0].value
                    out_q.put(("escalated", interrupt_payload))
                    continue
                _apply_update(case_id, update)
                out_q.put(("update", {"node": node_name, "payload": _jsonable(update)}))

        with _registry_lock:
            case = _registry[case_id]
            paused = bool(graph.get_state(config).next)
            case["status"] = "escalated" if paused else (case["decision"] or "approve")
            final_status = case["status"]
            _save_registry_locked()
        out_q.put(("done", {"status": final_status}))
    except Exception as exc:  # surface to the client instead of hanging the SSE connection
        with _registry_lock:
            _registry[case_id]["status"] = "error"
            _save_registry_locked()
        out_q.put(("error", {"message": str(exc)}))


@app.get("/api/cases/{case_id}/stream")
async def stream_case(case_id: str):
    with _registry_lock:
        if case_id not in _registry:
            raise HTTPException(404, "case not found")
        already_started = _registry[case_id]["status"] != "queued"

    async def event_gen():
        if already_started:
            # Reconnecting to a case that's already running/finished: send
            # its current snapshot instead of starting a second graph run.
            with _registry_lock:
                case = dict(_registry[case_id])
            yield {"event": "snapshot", "data": json.dumps(_jsonable(case))}
            yield {"event": "done", "data": json.dumps({"status": case["status"]})}
            return

        q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        thread = threading.Thread(target=_run_graph_in_thread, args=(case_id, q), daemon=True)
        thread.start()

        while True:
            kind, payload = await run_in_threadpool(q.get)
            yield {"event": kind, "data": json.dumps(payload)}
            if kind in ("done", "error"):
                break

    return EventSourceResponse(event_gen())


@app.post("/api/cases/{case_id}/resume")
async def resume_case(case_id: str, payload: ResumeDecision) -> dict:
    with _registry_lock:
        if case_id not in _registry:
            raise HTTPException(404, "case not found")
        if _registry[case_id]["status"] != "escalated":
            raise HTTPException(409, "case is not awaiting analyst review")

    config = {"configurable": {"thread_id": case_id}}

    def _resume() -> dict:
        # Guard against the checkpointer having no memory of this thread at
        # all -- e.g. the server restarted after this case escalated. Without
        # this check, graph.invoke(Command(resume=...)) would silently
        # invoke supervisor_node with an empty state and crash with
        # KeyError: 'transaction' instead of a clear error.
        snapshot = graph.get_state(config)
        if not snapshot.values.get("transaction"):
            raise RuntimeError(
                "This case's execution state is gone (most likely the server "
                "restarted after it escalated) -- it can no longer be "
                "resumed. Resubmit it as a new case if it still needs review."
            )
        return graph.invoke(Command(resume=payload.decision), config=config)

    try:
        result = await run_in_threadpool(_resume)
    except RuntimeError as exc:
        with _registry_lock:
            _registry[case_id]["status"] = "interrupted"
            _save_registry_locked()
        raise HTTPException(409, str(exc))

    with _registry_lock:
        case = _registry[case_id]
        case["decision"] = result.get("decision")
        case["risk_score"] = result.get("risk_score", case["risk_score"])
        case["risk_factors"] = result.get("risk_factors", case["risk_factors"])
        case["narrative"] = result.get("narrative")
        # graph.invoke(...) after a resume returns the FULL accumulated
        # state, not just the delta -- so this is the complete, canonical
        # trail (human_review + narrative entries included), not something
        # to append to what's already in the registry.
        if result.get("audit_trail"):
            case["audit_trail"] = result["audit_trail"]
        case["status"] = case["decision"] or "error"
        case["updated_at"] = _now()
        _save_registry_locked()
        return dict(case)


# Registered last so the /api/* routes above take precedence over the
# catch-all static mount.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
