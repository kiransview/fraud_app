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
import re
import sys
import tempfile
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
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_together import ChatTogether

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.sample_transactions import SAMPLE_CASES  # noqa: E402
from fraud_agent.graph import graph  # noqa: E402

app = FastAPI(title="Sentinel Fraud Review API")

# -------------------------------------------------------- chat assistant --
# Chat is the app's primary interface: it can hold a normal conversation AND
# trigger real actions via tool-calling. The LLM only decides *intent* and
# extracts *parameters* here -- it never scores or decides anything about
# fraud itself, and it never touches the graph directly. Every action it
# proposes is carried out by the frontend calling the exact same REST/SSE
# endpoints a human clicking buttons would use (submit_case, the SSE stream,
# resume_case), so there's exactly one code path for "a case actually runs."
_ASSISTANT_MODEL_NAME = "openai/gpt-oss-120b"
_assistant_model: ChatTogether | None = None


class SubmitTransactionAction(BaseModel):
    """Submit a new transaction for fraud review. Use this whenever the user
    describes a transaction/payment they want reviewed."""

    reply: str = Field(description="A short, natural confirmation to show right away, e.g. 'Submitting that $5,000 wire transfer now...'")
    amount: float = Field(description="Transaction amount")
    currency: str = Field(default="USD", description="ISO currency code")
    merchant: str = Field(default="Unspecified merchant", description="Merchant or payee name")
    category: str = Field(default="other", description="One of: electronics, grocery, travel, dining, wire, jewelry, other")
    channel: str = Field(default="ecommerce", description="One of: ecommerce, card_present, wire_transfer")
    geo: str = Field(default="Unknown", description="Where the transaction originated, e.g. 'Lagos, NG'")
    home_geo: str = Field(default="", description="The account's usual home location; leave empty to default to geo (no travel signal)")
    device_new: bool = Field(default=False, description="True only if the user says this is a new/unrecognized device")
    new_payee: bool = Field(default=False, description="True only if the user says this is a new payee/recipient")
    account_holder_name: str = Field(default="Unspecified", description="The customer or business name on the account")
    prior_flag_count: int = Field(default=0, description="How many times this account was previously flagged, if mentioned")


class LookupCaseAction(BaseModel):
    """Look up the status, score, decision, or narrative of an existing case."""

    reply: str = Field(description="Short message, e.g. 'Let me check on that.'")
    case_id: str = Field(default="", description="Exact case ID (e.g. TXN-xxxx) if the user gave one; else leave empty")
    merchant_hint: str = Field(default="", description="Merchant/payee name if the user referred to the case that way instead of by ID")


class ResumeCaseAction(BaseModel):
    """Approve or decline a case that is escalated and awaiting analyst review."""

    reply: str = Field(description="Short confirmation, e.g. 'Declining that case now.'")
    decision: str = Field(description="Either 'approve' or 'decline'")
    case_id: str = Field(default="", description="Case ID if the user gave one; else leave empty to mean the most recently discussed case")


class ListCasesAction(BaseModel):
    """List recent cases, optionally filtered by status."""

    reply: str = Field(description="Short message, e.g. 'Here's what's in the queue.'")
    status_filter: str = Field(default="", description="One of: queued, running, escalated, approve, decline -- or empty for all")


_ASSISTANT_TOOLS = [SubmitTransactionAction, LookupCaseAction, ResumeCaseAction, ListCasesAction]

_ASSISTANT_SYSTEM_PROMPT = """You are Sentinel's chat assistant -- payment fraud review, \
by chat. You can hold a normal conversation AND trigger real actions:
- submit a new transaction for review
- look up an existing case
- approve or decline an escalated case
- list recent cases

Call the matching tool when the user's message clearly asks for one of those actions. \
If they're just asking a question (how does X work, what does field Y mean), do NOT call \
a tool -- just answer directly in plain text, concisely.

FIELD NOTES (for submitting a transaction)
- category: electronics, grocery, travel, dining, wire, jewelry, or other.
- channel: ecommerce, card_present, or wire_transfer -- infer wire_transfer if they say "wire".
- geo / home_geo: home_geo defaults to geo if not mentioned (implies no travel risk signal).
- device_new / new_payee: only set true if the user actually says so.
- Never invent a risk score or decision yourself in your reply text -- only an actual \
pipeline run produces that. If you called an action tool, keep `reply` to a short \
in-progress confirmation; the real result gets shown separately once the action completes."""


def _get_assistant_model() -> ChatTogether:
    global _assistant_model
    if _assistant_model is None:
        _assistant_model = ChatTogether(model=_ASSISTANT_MODEL_NAME, temperature=0.2).bind_tools(_ASSISTANT_TOOLS)
    return _assistant_model


# gpt-oss-120b occasionally leaks its raw harmony-format tool-call tokens
# into the response *content* instead of a parsed `tool_calls` entry (e.g.
# "<|start|>assistant<|channel|>commentary to=functions.ResumeCase
# <|constrain|>json<|message|>{...}<|call|>..."), and also sometimes drops
# the "Action" suffix from the tool's real class name. Recover the intended
# call by regex rather than ever showing raw tokens to the user -- the same
# "don't trust the model to self-report cleanly" lesson as
# fraud_agent/subagents.py's JSON extraction.
_HARMONY_LEAK_PATTERN = re.compile(r"to=functions\.(\w+).*?<\|message\|>(\{.*?\})\s*<\|call\|>", re.DOTALL)
_TOOL_NAME_ALIASES = {t.__name__.replace("Action", "").lower(): t.__name__ for t in _ASSISTANT_TOOLS}


def _recover_leaked_tool_call(text: str) -> tuple[str, dict] | None:
    match = _HARMONY_LEAK_PATTERN.search(text)
    if not match:
        return None
    name = _TOOL_NAME_ALIASES.get(match.group(1).lower())
    if not name:
        return None
    try:
        args = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    return name, args


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn]


@app.post("/api/assistant/chat")
async def assistant_chat(payload: ChatRequest) -> dict:
    lc_messages: list[Any] = [SystemMessage(content=_ASSISTANT_SYSTEM_PROMPT)]
    for turn in payload.messages:
        if turn.role == "user":
            lc_messages.append(HumanMessage(content=turn.content))
        else:
            lc_messages.append(AIMessage(content=turn.content))

    model = _get_assistant_model()

    def _call():
        return model.invoke(lc_messages)

    response = await run_in_threadpool(_call)

    if response.tool_calls:
        call = response.tool_calls[0]
        name, args = call["name"], call["args"]
    else:
        recovered = _recover_leaked_tool_call(response.content or "")
        if recovered:
            name, args = recovered
        else:
            reply_text = response.content or "Sorry, I didn't catch that -- could you rephrase?"
            if "<|" in reply_text:  # still-garbled harmony tokens we couldn't recover -- never show raw
                reply_text = "Sorry, I had trouble with that -- could you try rephrasing?"
            return {"reply": reply_text, "action": None}

    if name == "SubmitTransactionAction":
        action = {
            "type": "submit",
            "transaction": {
                "amount": args.get("amount", 0),
                "currency": args.get("currency", "USD"),
                "merchant": args.get("merchant", "Unspecified merchant"),
                "category": args.get("category", "other"),
                "channel": args.get("channel", "ecommerce"),
                "geo": args.get("geo", "Unknown"),
                "home_geo": args.get("home_geo") or args.get("geo", "Unknown"),
                "device_id": f"dev-{uuid.uuid4().hex[:8]}",
                "device_new": bool(args.get("device_new", False)),
                "ip_address": f"203.0.113.{uuid.uuid4().int % 254 + 1}",
                "new_payee": bool(args.get("new_payee", False)),
            },
            "customer_profile": {
                "account_id": f"acct-{uuid.uuid4().hex[:8]}",
                "name": args.get("account_holder_name", "Unspecified"),
                "prior_flag_count": int(args.get("prior_flag_count", 0)),
            },
        }
    elif name == "LookupCaseAction":
        action = {"type": "lookup", "case_id": args.get("case_id", ""), "merchant_hint": args.get("merchant_hint", "")}
    elif name == "ResumeCaseAction":
        action = {"type": "resume", "case_id": args.get("case_id", ""), "decision": args.get("decision", "")}
    elif name == "ListCasesAction":
        action = {"type": "list", "status_filter": args.get("status_filter", "")}
    else:
        action = None

    return {"reply": args.get("reply", "On it."), "action": action}

STATIC_DIR = Path(__file__).parent / "static"

# The case registry (queue/detail views, and every case's full audit_trail
# log) is persisted to this JSON file and reloaded on startup, so a new
# session still shows past cases and their logs instead of an empty queue.
# LangGraph's own execution checkpointer (fraud_agent/graph.py) is still
# MemorySaver (in-process only) -- see _load_registry() for what that means
# for a case that was mid-run when the server last stopped.
_REPO_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "case_registry.json"


def _resolve_registry_path() -> Path:
    """Prefer the repo-relative path (writable locally and in the Docker
    image). Some hosts -- notably Vercel's serverless filesystem -- only
    allow writes under a temp directory; probing here and falling back
    avoids a 500 on every request. This does NOT make state durable across
    separate serverless invocations (each can be a fresh instance with an
    empty /tmp) -- see README's "Deploying to HuggingFace Spaces" section
    for why this app needs a real persistently-running process, not a
    serverless one, to behave correctly."""
    try:
        _REPO_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe = _REPO_REGISTRY_PATH.parent / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return _REPO_REGISTRY_PATH
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "sentinel_case_registry.json"
        print(
            f"[sentinel] {_REPO_REGISTRY_PATH.parent} isn't writable "
            f"(read-only filesystem?) -- falling back to {fallback}. "
            "Case history will not survive a restart on this host.",
            file=sys.stderr,
        )
        return fallback


REGISTRY_PATH = _resolve_registry_path()

_registry_lock = threading.Lock()
_registry: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _save_registry_locked() -> None:
    """Writes `_registry` to disk. Caller must already hold `_registry_lock`.
    Best-effort: a write failure here degrades to "this update isn't
    persisted" rather than crashing the request that triggered it."""
    try:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = REGISTRY_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_registry, f)
        os.replace(tmp_path, REGISTRY_PATH)  # atomic replace on both Windows and POSIX
    except OSError as exc:
        print(f"[sentinel] could not persist case registry: {exc}", file=sys.stderr)


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


# Same allowlist pattern as above, for repo docs the UI renders for viewing
# (currently just the ontology) rather than citation evidence.
_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
_DOCS_MEDIA_TYPES = {
    "ontology.md": "text/markdown",
}


@app.get("/api/docs/{filename}")
def get_doc_file(filename: str):
    if filename not in _DOCS_MEDIA_TYPES:
        raise HTTPException(404, "not found")
    path = _DOCS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type=_DOCS_MEDIA_TYPES[filename])


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
