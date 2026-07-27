---
title: Sentinel Fraud Review
emoji: 🛡️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Sentinel — payment fraud multi-agent pipeline

A LangGraph/LangChain fraud-review pipeline: a rule-based supervisor triages
each transaction, fans out to two parallel LLM subagents -- **Risk Analysis**
(spending patterns, travel plausibility, device/IP reputation, linked-account
signals) and **Compliance** (sanctions screening + business rules) -- then
aggregates their findings into a composite risk score and routes to
auto-approve, auto-decline, or a human-in-the-loop escalation.

```
START -> supervisor -> [risk_analysis | compliance]
                          (parallel, only the ones the supervisor routes to)
                     -> aggregator
                     -> approve | decline | human_review -> narrative -> END
```

## Project layout

```
fraud_agent/
  state.py       shared FraudCaseState (TypedDict) passed between every node
  tools.py       data-source tools -- real reference data where it exists,
                 clearly-labeled simulation where it doesn't (see below)
  supervisor.py  deterministic routing -- decides which subagents to run
  subagents.py   the 2 LangChain tool-calling agents (prompt + model + tools)
  aggregator.py  weighted scoring + hard overrides (e.g. compliance hit)
  narrative.py   LLM call that writes the analyst-facing case note
  graph.py       StateGraph wiring: fan-out, conditional routing, interrupt()
data/
  sample_transactions.py   3 example cases (fast-path, escalate, full-review)
  reference/
    amount_baseline.json     real percentile/fraud-rate stats, derived from
                              the ULB Credit Card Fraud Detection dataset
    ofac_sdn.csv              the real, current US Treasury OFAC SDN list
    compliance_policy.md      the real policy text the Compliance agent reads
ui/
  server.py            FastAPI service exposing the graph (see "Dashboard UI" below)
  static/index.html    the live dashboard frontend
  static/submit.html   guided case-intake form + requirements chat assistant
  static/logs.html     per-case, timestamped step-by-step agent log
run.py                 launches the dashboard UI (uvicorn + FastAPI)
scripts/run_samples.py   console-only check: runs the 3 sample cases, no UI
```

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # then paste your TOGETHER_API_KEY into .env
python run.py
```

`python run.py` starts the dashboard UI -- it does not run any analysis by
itself. Once it's running, open:

- http://127.0.0.1:8000/ -- the dashboard
- http://127.0.0.1:8000/submit.html -- guided intake form + chat assistant

Leave that process running (Ctrl+C to stop) and use the browser to actually
submit and review cases. If you just want a quick console check that the
pipeline itself works, without touching the UI, run
`python scripts/run_samples.py` instead -- that's the one that runs the 3
sample transactions and prints decisions/narratives to the terminal.

Model: `openai/gpt-oss-120b`, served via [Together AI](https://www.together.ai/),
through LangChain's `ChatTogether` integration (`langchain-together`).

## How the pieces fit together

- **Supervisor** (`supervisor.py`) is plain Python, not an LLM call -- cheap
  and deterministic, so routing decisions are auditable and free. A small,
  routine transaction on a known device only runs Risk Analysis (the
  fast-path); anything with a new device, new payee, or prior flags also
  runs Compliance.
- **Subagents** (`subagents.py`) are real LangChain agents: each is a system
  prompt + `ChatTogether` model (`openai/gpt-oss-120b`) + one tool, built
  with LangGraph's `create_react_agent`. They're dispatched in parallel via
  LangGraph's `Send` API and each writes its finding under its own key in
  `findings`, so the parallel writes never collide.
- **Aggregator** (`aggregator.py`) is plain Python: a weighted composite
  score, with a hard override so a compliance hit isn't averaged down by a
  clean risk-analysis finding.
- **Escalation** (`human_review_node` in `graph.py`) uses LangGraph's
  `interrupt()` with a checkpointer, so a mid-band score durably pauses the
  graph instead of blocking a thread. Resume it with:
  ```python
  graph.invoke(Command(resume="approve"), config={"configurable": {"thread_id": txn_id}})
  ```
  `graph.py` uses `MemorySaver` (in-process only); swap for a Postgres/Redis
  checkpointer before this touches real traffic, so an escalated case
  survives a process restart.
- **Narrative** (`narrative.py`) is the one purely-language node: it turns
  the numeric findings into the case note a human analyst reads.

## Dashboard UI

```
.venv\Scripts\activate
python run.py
```

(equivalent to `uvicorn ui.server:app --reload`, run.py just wraps that with
the API-key check). `ui/server.py` is a small FastAPI service in front of the
same `graph` used by `scripts/run_samples.py`:

- `POST /api/cases` submits a transaction and returns immediately.
- `GET /api/cases/{id}/stream` is a Server-Sent Events feed (via
  `sse-starlette`) that emits one event per node **as the graph actually
  executes it** -- the dashboard's live pipeline view is real telemetry, not
  a client-side animation. The blocking `graph.stream(...)` call runs in a
  background thread and is bridged to the async SSE generator through a
  plain `queue.Queue`, since the subagents make real (blocking) network
  calls to Together AI.
- `POST /api/cases/{id}/resume` supplies an analyst's Approve/Decline
  decision for an escalated case, resuming the graph from its `interrupt()`
  via `Command(resume=...)`.
- Case data (including every case's full `audit_trail` log) lives in an
  in-process registry that's write-through persisted to
  `data/case_registry.json` on every mutation and reloaded on startup, so a
  new session still shows past cases and their logs instead of an empty
  queue. This is a single JSON file, not a real datastore -- fine for one
  process on one machine, not for multiple instances; swap `_save_registry_locked`
  / `_load_registry` in `server.py` for a real DB before that matters. Note
  this is separate from LangGraph's own execution checkpointer (still
  `MemorySaver`, in-process only): a case that was mid-run when the server
  stopped is still viewable (marked "interrupted") but can't actually be
  resumed until the checkpointer itself is durable too (see Next steps).

`ui/static/index.html` is vanilla HTML/CSS/JS (no build step): it submits
cases, opens an `EventSource` per running case, and re-derives each
pipeline node's live status (pending/running/done/skipped/unavailable) from
the state updates as they arrive.

### Guided intake (`submit.html`)

A second page for submitting a transaction through a real form (dropdowns +
text inputs per field) instead of hand-written JSON, plus a chat panel for
questions about what each field means and how the pipeline uses it:

- `POST /api/assistant/chat` is a separate, stateless `gpt-oss-120b` call
  (not the review graph) with a system prompt describing the transaction/
  customer-profile schema and the routing/scoring rules. The current form
  values are sent along with each question so answers are specific to what
  the analyst has typed in, not generic. It never produces a risk score or
  decision itself -- only an actual graph run does that.
- On submit, the form links to `index.html?case=ID`, which the dashboard
  reads on load to auto-select and attach to that case's live stream.

### Agent log (`logs.html`)

A third page: pick a case and see a plain, chronological, timestamped log of
exactly what each node did and why -- built for explaining a decision after
the fact, not for live monitoring. It reads the same `audit_trail` every
node already writes (`fraud_agent/state.py`'s `log_entry` helper stamps each
line with a `time.time()` value), and re-sorts by that timestamp before
rendering: LangGraph merges same-superstep updates from parallel branches
(Risk Analysis, Compliance) in node-registration order, not actual finish
order, so a naive render can show them out of sequence. Each row also shows
the elapsed time since the previous step. The `/api/cases/{id}/resume`
endpoint writes the graph's full post-resume `audit_trail` back into the
registry (not just the pre-escalation entries), so the analyst's decision
and the narrative generation both show up here too.

## What's real vs. simulated in `tools.py`

- **Real**: the amount-risk signal (`_amount_risk_profile`) is looked up
  against `data/reference/amount_baseline.json` -- percentiles and
  fraud-rate-by-amount-bucket computed from the actual ULB "Credit Card
  Fraud Detection" dataset (284,807 real anonymized transactions, 492
  confirmed frauds, Dal Pozzolo et al., European cardholders, September
  2013). The sanctions screen (`_ofac_sanctions_check`) does real
  fuzzy-name matching (`difflib`) against `data/reference/ofac_sdn.csv`,
  the actual current US Treasury OFAC SDN list (~19,000 entries,
  downloaded from `sanctionslistservice.ofac.treas.gov`). The jurisdiction
  check and structuring/CTR thresholds are grounded in
  `data/reference/compliance_policy.md`, which the Compliance agent's
  prompt includes in full -- real FATF grey/black-list countries (June
  2026 update) and real Bank Secrecy Act figures (31 CFR 1010.311, 31
  U.S.C. Sec. 5324), not invented ones. The agent cites specific policy
  sections in its reasoning as a result.
- **Still simulated**: recent transaction velocity, device-fingerprint
  history, IP reputation, and the linked-account graph
  (`_txn_velocity_lookup`, `_device_fingerprint_lookup`, `_ip_reputation`,
  `_shared_attribute_graph_query`) are still deterministic hashes of the
  input, not lookups against anything real. No public dataset can
  legitimately expose real per-account device/session history -- a real
  deployment sources these from its own device-intelligence vendor,
  session logs, and entity graph. The function signatures and return
  shapes are already the contract the subagents rely on, so swapping the
  body for a real API/DB call is a self-contained change.

To refresh `ofac_sdn.csv`, re-download it -- it's public and
unauthenticated at `sanctionslistservice.ofac.treas.gov` (follow the
redirect from `treasury.gov/ofac/downloads/sdn.csv`). To refresh
`amount_baseline.json`, recompute percentiles and fraud-rate-by-bucket
(plain `csv`/`statistics`, no pandas needed) over any CSV with `Amount`/
`Class` columns, such as the ULB dataset above.

## Deploying to HuggingFace Spaces

This repo's YAML header (top of this file) declares `sdk: docker`, so a
Space built from it runs the *actual* app -- the Dockerfile builds this
image and starts `uvicorn ui.server:app`, the same FastAPI service `run.py`
launches locally. No rebuild, no separate demo app: what you see locally is
what the Space serves, live SSE streaming included.

```
hf auth login
hf repo create <your-username>/<space-name> --type space --sdk docker
git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
git push space main
```

Then, on the Space's page: **Settings -> Variables and secrets -> New
secret**, add `TOGETHER_API_KEY`. Never commit it or paste it into a
terminal command that lands in shell history someone else can see.

Note: HF's free CPU tier Spaces have ephemeral storage -- `data/case_registry.json`
(see "What's real vs. simulated" above and `ui/server.py`) may not survive a
Space restart/rebuild there. That's a real limitation for a demo Space, same
as the `MemorySaver` checkpointer limitation below.

## Next steps

1. Swap `MemorySaver` for a durable checkpointer, and the JSON-file case
   registry in `ui/server.py` for a real datastore, before handling real
   traffic.
2. Add an offline evaluation harness against labeled fraud data before
   tuning `APPROVE_MAX` / `DECLINE_MIN` in `graph.py`.
3. Replace the still-simulated signals (velocity, device, IP, network) with
   real vendor/DB integrations when you have them.
