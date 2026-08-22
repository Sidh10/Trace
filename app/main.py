"""FastAPI app — mounts the simulated environment's REST surface, the
orchestrator, and static/. ARCHITECTURE.md §9. Run with:

    uvicorn app.main:app --reload --port 8080

Two routers, in this order:
  * `environment_router` — the problem statement's own §5/§6 tool surface
    (inventory, POs, suppliers, RFQ, approval/check, erp/update, tracking,
    production-schedule, clock). The simulator.
  * `api_router` — the orchestrator (`/agent/*`): the assembled item-1-to-7
    pipeline and the ERP-write boundary. The agent.

Registration order matters: the environment's `POST /approval/check` (§5.8)
is a literal path, and the orchestrator's approval endpoint is namespaced
`/agent/approval/{plan_id}` rather than a bare `/approval/{plan_id}`
specifically so the two never share a path space. See `app/api/routes.py`.

Items 8-13 mount here as they land.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config  # noqa: F401 — importing loads .env before anything else needs it
from app.api.routes import router as api_router
from app.environment.routes import router as environment_router

app = FastAPI(title="TRACE — Supply Chain Disruption Control Agent")

app.include_router(environment_router, tags=["environment"])
app.include_router(api_router, tags=["agent"])
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
def read_root() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_enabled": config.TRACE_LLM_ENABLED}

