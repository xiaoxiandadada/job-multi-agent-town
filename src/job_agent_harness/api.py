from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .models import RoleSpec, RunReport, RunRequest
from .runtime import ROOT, build_orchestrator, build_registry


def create_app() -> FastAPI:
    app = FastAPI(title="Job Agent Studio", version="0.1.0")
    registry = build_registry()
    orchestrator = build_orchestrator()

    @app.get("/")
    async def index():
        return FileResponse(Path(ROOT) / "web" / "index.html")

    @app.get("/health")
    async def health():
        return {"status": "ok", "registry_version": registry.version}

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

