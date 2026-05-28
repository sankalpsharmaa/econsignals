"""FastAPI backend for the EconSignals dashboard.

Endpoints
    GET  /api/feed        live dashboard snapshot (same shape as feed.json)
    GET  /api/health      liveness probe
    POST /api/feedback    record a thumbs up/down on a paper (retrains JEL weights)

When a built SPA exists at webapp/dist it is served at / so `econsignals-web`
gives a complete local app. The static GitHub Pages deployment uses the baked
feed.json and does not need this server.

Run:  econsignals-web   (or  python -m econsignals.web.app)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from econsignals.lib.db import PROJ_ROOT, init_db
from econsignals.lib.relevance import update_jel_weights
from econsignals.lib.snapshot import build_snapshot

_FEEDBACK_LOG: Path = PROJ_ROOT / "data" / "feedback.jsonl"
_DIST_DIR: Path = PROJ_ROOT / "webapp" / "dist"

app = FastAPI(title="EconSignals", version="1.0")

# Permit the Vite dev server (and the local file) to call the API during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Feedback(BaseModel):
    paper_id: int
    interesting: bool


@app.get("/api/health")
def health() -> dict:
    """Liveness probe."""
    return {"ok": True, "service": "econsignals"}


@app.get("/api/feed")
def feed() -> dict:
    """Return the dashboard snapshot built live from the database."""
    return build_snapshot()


@app.post("/api/feedback")
def feedback(item: Feedback) -> dict:
    """Record a relevance vote and nudge JEL weights toward the user's taste.

    Appends to data/feedback.jsonl (an exportable audit trail) and runs the
    existing EMA update on profile/jel_weights.json, wiring the feedback loop
    that previously existed in code but was never called.
    """
    _FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "paper_id": item.paper_id,
        "interesting": item.interesting,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _FEEDBACK_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    update_jel_weights(item.paper_id, item.interesting)
    return {"ok": True, "recorded": record}


# Serve the built SPA at / when present (optional; dev uses the Vite server).
if _DIST_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="spa")
else:
    @app.get("/")
    def _root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "econsignals",
                "hint": "Build the SPA (cd webapp && npm run build) to serve it here, "
                "or run the Vite dev server against /api.",
                "endpoints": ["/api/feed", "/api/health", "/api/feedback"],
            }
        )


def run() -> None:
    """Console-script entry point: start the dev server."""
    import uvicorn

    init_db()
    host = os.environ.get("ECONSIGNALS_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("ECONSIGNALS_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
