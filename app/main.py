"""FastAPI app — mounts the environment's REST surface and static/.
ARCHITECTURE.md §8. Run with:

    uvicorn app.main:app --reload --port 8080

engine/, audit/, llm/, api/ (build-order items 2-13) mount here as they land;
none of them exist yet, so only the environment router is included so far.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config  # noqa: F401 — importing loads .env before anything else needs it
from app.environment.routes import router as environment_router

app = FastAPI(title="TRACE — Supply Chain Disruption Control Agent")

app.include_router(environment_router, tags=["environment"])
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_enabled": config.TRACE_LLM_ENABLED}
