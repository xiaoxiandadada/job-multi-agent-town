from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .activity import ActivityEvent
from .models import RoleSpec, RunReport, RunRequest
from .runtime import (
    ROOT,
    build_activity_store,
    build_orchestrator,
    build_registry,
)
from .town import TownSnapshot, build_town_snapshot


def create_app() -> FastAPI:
    app = FastAPI(title="Job Agent Studio", version="0.1.0")
    registry = build_registry()
    activity_store = build_activity_store()
    orchestrator = build_orchestrator(registry)

    @app.get("/")
    async def index():
        return FileResponse(Path(ROOT) / "web" / "index.html")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "registry_version": registry.version,
            "orchestrator": type(orchestrator).__name__,
        }

    @app.get("/api/runtime")
    async def runtime_info():
        mermaid = (
            orchestrator.mermaid()
            if hasattr(orchestrator, "mermaid")
            else ""
        )
        return {
            "orchestrator": type(orchestrator).__name__,
            "default_mode": "collaborative",
            "phases": ["route", "context", "action", "judge"],
            "graph_mermaid": mermaid,
        }

    @app.get("/api/activity", response_model=list[ActivityEvent])
    async def activity(
        limit: int = Query(default=300, ge=1, le=2000),
        run_id: str | None = None,
    ):
        return activity_store.read(limit=limit, run_id=run_id)

    @app.get("/api/town", response_model=TownSnapshot)
    async def town():
        return build_town_snapshot(
            registry,
            activity_store.read(limit=1200),
        )

    @app.get("/api/roles", response_model=list[RoleSpec])
    async def list_roles():
        return registry.list_roles(include_disabled=True)

    @app.post("/api/roles", status_code=201)
    async def add_role(role: RoleSpec):
        try:
            version = registry.add(role)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"role": role, "registry_version": version}

    @app.post("/api/runs", response_model=RunReport)
    async def run_agents(request: RunRequest):
        return await orchestrator.run(request)

    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "job_agent_harness.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
