from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .activity import ActivityEvent
from .cognition import AgentMemory, RetrievedMemory
from .models import RolePatch, RoleSpec, RunReport, RunRequest
from .runtime import (
    ROOT,
    build_activity_store,
    build_memory_store,
    build_orchestrator,
    build_registry,
)
from .town import TownSnapshot, build_town_snapshot


def create_app() -> FastAPI:
    app = FastAPI(title="Job Agent Studio", version="0.1.0")
    registry = build_registry()
    activity_store = build_activity_store()
    memory_store = build_memory_store()
    orchestrator = build_orchestrator(registry, memory_store)

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
    async def town(
        run_id: str | None = None,
        step: int | None = Query(default=None, ge=1, le=2000),
    ):
        events = activity_store.read(limit=2000)
        if run_id is not None:
            run_events = [
                event for event in events if event.run_id == run_id
            ]
            if not run_events:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown run: {run_id}",
                )
            total_steps = len(run_events)
            replay_step = min(step or total_steps, total_steps)
            return build_town_snapshot(
                registry,
                run_events[:replay_step],
                memory_store=None,
                replay_step=replay_step,
                replay_total_steps=total_steps,
            )
        return build_town_snapshot(
            registry,
            events,
            memory_store=memory_store,
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

    @app.patch("/api/roles/{role_id}")
    async def update_role(role_id: str, patch: RolePatch):
        try:
            role, version = registry.update(
                role_id,
                **patch.model_dump(exclude_unset=True),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"role": role, "registry_version": version}

    @app.get(
        "/api/agents/{role_id}/memories",
        response_model=list[AgentMemory],
    )
    async def agent_memories(
        role_id: str,
        limit: int = Query(default=100, ge=1, le=2000),
    ):
        try:
            registry.get(role_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return memory_store.list(role_id=role_id, limit=limit)

    @app.get(
        "/api/agents/{role_id}/memories/search",
        response_model=list[RetrievedMemory],
    )
    async def search_agent_memories(
        role_id: str,
        query: str = Query(min_length=1, max_length=2000),
        limit: int = Query(default=4, ge=1, le=20),
    ):
        try:
            registry.get(role_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return memory_store.retrieve(
            role_id=role_id,
            query=query,
            limit=limit,
        )

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
