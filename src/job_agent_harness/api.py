from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .activity import ActivityEvent
from .always_on import always_on_enabled, build_watcher
from .chief_of_staff import ChiefOfStaff
from .commands import command_catalog
from .cognition import AgentMemory, RetrievedMemory
from .daily_schedule import DailyPushScheduler
from .models import RolePatch, RoleSpec, RunReport, RunRequest
from .runtime import (
    ROOT,
    build_activity_store,
    build_memory_store,
    build_orchestrator,
    build_registry,
    build_task_graph_store,
)
from .tasks import TaskGraph
from .town import (
    TownSnapshot,
    build_town_snapshot,
    current_run_id_for_events,
    shifts_for_events,
)


def create_app() -> FastAPI:
    registry = build_registry()
    activity_store = build_activity_store()
    memory_store = build_memory_store()
    task_graph_store = build_task_graph_store()
    orchestrator = build_orchestrator(registry, memory_store)
    base_orchestrator = getattr(orchestrator, "base", orchestrator)
    model_client = base_orchestrator.model_client
    chief = ChiefOfStaff(orchestrator)
    watcher = build_watcher(orchestrator, activity_store)
    daily_scheduler = DailyPushScheduler()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # The always-on shift lives with the server process, so opening the
        # dashboard is enough to see the agents working. `job-agent-watch`
        # holds the same lock, so running both never doubles the patrols.
        if always_on_enabled():
            watcher.start()
        # Same deal for the morning brief: it used to need somebody to type
        # `job-agent-push-daily`, so "every day" quietly meant "never".
        daily_scheduler.start()
        try:
            yield
        finally:
            await watcher.stop()
            await daily_scheduler.stop()

    app = FastAPI(title="Job Agent Studio", version="0.1.0", lifespan=lifespan)

    @app.get("/api/always-on")
    async def always_on_status():
        # `watcher.status()` only knows about this process. The patrol usually
        # runs in `job-agent-watch`, so the duty report has to come from the
        # shared event log — otherwise a working watcher reads as "not running".
        shifts = shifts_for_events(
            activity_store.read(limit=600),
            display_names={
                role.role_id: role.display_name
                for role in registry.list_roles(include_disabled=True)
            },
            now=datetime.now(timezone.utc),
        )
        return {
            "configured": always_on_enabled(),
            **watcher.status(),
            "shifts": [shift.model_dump() for shift in shifts],
            "daily_push": daily_scheduler.status(),
        }

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
            "phases": [
                "route",
                "discovery",
                "analysis",
                "action",
                "judge",
            ],
            "graph_mermaid": mermaid,
        }

    @app.get("/api/models")
    async def models(catalog: bool = False):
        available: list[str] = []
        catalog_error: str | None = None
        if catalog and hasattr(model_client, "available_models"):
            try:
                available = await model_client.available_models()
            except Exception as exc:
                catalog_error = type(exc).__name__
        roles = registry.list_roles(include_disabled=True)
        return {
            "profiles": (
                model_client.profile_models()
                if hasattr(model_client, "profile_models")
                else {}
            ),
            "roles": [
                {
                    "role_id": role.role_id,
                    "model_profile": role.model_profile,
                    "model_override": role.model,
                    "resolved_model": (
                        model_client.model_for(role)
                        if hasattr(model_client, "model_for")
                        else "unknown"
                    ),
                }
                for role in roles
            ],
            "catalog": available,
            "catalog_error": catalog_error,
        }

    @app.get("/api/task-graphs", response_model=list[TaskGraph])
    async def task_graphs(
        limit: int = Query(default=50, ge=1, le=500),
    ):
        return task_graph_store.list(limit=limit)

    @app.get("/api/task-graphs/{run_id}", response_model=TaskGraph)
    async def task_graph(run_id: str):
        graph = task_graph_store.get(run_id)
        if graph is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown task graph: {run_id}",
            )
        return graph

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
                task_graph=task_graph_store.get(run_id),
            )
        current_run_id = current_run_id_for_events(events)
        return build_town_snapshot(
            registry,
            events,
            memory_store=memory_store,
            task_graph=(
                task_graph_store.get(current_run_id)
                if current_run_id
                else None
            ),
        )

    @app.get("/api/commands")
    async def commands():
        """Feeds the web input's slash palette.

        Served rather than duplicated in JavaScript so the palette can never
        offer a command the parser does not implement.
        """

        return [item.__dict__ for item in command_catalog()]

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
        # The web chat window is the controller's other front door, so it gets
        # the same Chief of Staff treatment as the Feishu controller bot: the
        # receipt and the closing arrive as activity events the page is already
        # polling, and attached pictures are read once here rather than by every
        # selected role.
        return await chief.run(request)

    # Mounted last so the routes above keep priority: the page is no longer one
    # file but a directory (styles, app.js, the Phaser scene, the tilesheets),
    # and every one of them has to be reachable for the town to render at all.
    app.mount(
        "/",
        StaticFiles(directory=Path(ROOT) / "web", html=True),
        name="web",
    )

    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "job_agent_harness.api:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
