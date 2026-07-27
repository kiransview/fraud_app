# HF Spaces (Docker SDK) builds this and runs the container, routing traffic
# to the port declared by `app_port` in README.md's YAML header (7860).
# This runs the exact same FastAPI app (ui/server.py) that `run.py` launches
# locally -- the real 3-page dashboard with live SSE, not a rebuilt demo.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# TOGETHER_API_KEY is injected by HF as a Space secret at runtime -- never
# baked into the image. Set it under Settings -> Variables and secrets.
EXPOSE 7860

CMD ["uvicorn", "ui.server:app", "--host", "0.0.0.0", "--port", "7860"]
