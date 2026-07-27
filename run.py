"""Entry point: launches the Sentinel dashboard UI (FastAPI + web frontend).

    python run.py

Then open:
    http://127.0.0.1:8000/               dashboard
    http://127.0.0.1:8000/submit.html    guided case intake + chat assistant

Requires TOGETHER_API_KEY (see .env.example) -- the subagents and the
narrative node are real LLM calls (openai/gpt-oss-120b via Together AI).

For a quick console-only check without the browser, see
scripts/run_samples.py instead.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

if not os.getenv("TOGETHER_API_KEY"):
    sys.exit(
        "TOGETHER_API_KEY is not set.\n"
        "Copy .env.example to .env and add your key, then re-run:\n"
        "  cp .env.example .env"
    )

import uvicorn

HOST = "127.0.0.1"
PORT = 8000

if __name__ == "__main__":
    print("Starting Sentinel...")
    print(f"  Dashboard:  http://{HOST}:{PORT}/")
    print(f"  New case:   http://{HOST}:{PORT}/submit.html")
    print("Press CTRL+C to stop.\n")
    uvicorn.run("ui.server:app", host=HOST, port=PORT, reload=True)
