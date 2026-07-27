"""CLI check: run the 3 sample transactions through the fraud-review graph
and print each decision/narrative to the console -- no server, no browser.

    python scripts/run_samples.py

Requires TOGETHER_API_KEY (see .env.example) -- the subagents and the
narrative node are real LLM calls (openai/gpt-oss-120b via Together AI), not
stubs. For the actual dashboard UI, run `python run.py` instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to a legacy codepage that can't print every
# character an LLM might produce (en-dashes, curly quotes, ...). Force
# stdout to UTF-8 so a narrative with those characters doesn't crash the CLI.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

if not os.getenv("TOGETHER_API_KEY"):
    sys.exit(
        "TOGETHER_API_KEY is not set.\n"
        "Copy .env.example to .env and add your key, then re-run:\n"
        "  cp .env.example .env"
    )

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.types import Command  # noqa: E402  (import after the key check)

from data.sample_transactions import SAMPLE_CASES  # noqa: E402
from fraud_agent.graph import graph  # noqa: E402


def run_case(case: dict) -> None:
    thread_id = case["transaction"]["id"]
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(case, config=config)

    if "__interrupt__" in result:
        print(f"\n[{thread_id}] ESCALATED -- awaiting analyst decision")
        print(f"  risk_score={result['risk_score']}  factors={result['risk_factors']}")
        # A real dashboard/API would call this same graph.invoke(Command(...))
        # once an analyst clicks Approve/Decline. Here we simulate approval.
        result = graph.invoke(Command(resume="approve"), config=config)

    print(f"\n[{thread_id}] decision={result['decision']}  risk_score={result['risk_score']}")
    print(f"  narrative: {result.get('narrative', '(no narrative generated -- auto-approved fast-path)')}")


if __name__ == "__main__":
    for case in SAMPLE_CASES:
        run_case(case)
