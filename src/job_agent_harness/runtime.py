from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from .activity import ActivityStore
from .cognition import MemoryStore
from .model_client import OpenAICompatibleClient
from .orchestrator import MultiAgentOrchestrator
from .registry import RoleRegistry


ROOT = Path(
    os.getenv("JOB_AGENT_PROJECT_ROOT", Path(__file__).resolve().parents[2])
).resolve()
load_dotenv(ROOT / ".env")


def build_registry() -> RoleRegistry:
    data_dir = runtime_data_dir()
    return RoleRegistry.from_seed(
        runtime_path=data_dir / "roles.json",
        seed_path=ROOT / "configs" / "roles.json",
    )


def runtime_data_dir() -> Path:
    return Path(os.getenv("JOB_AGENT_DATA_DIR", ROOT / "data" / "runtime"))


def prepare_directory() -> Path:
    configured = os.getenv("JOB_AGENT_PREPARE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if len(ROOT.parents) >= 2:
        return (ROOT.parents[1] / "prepare").resolve()
    return (ROOT / "prepare").resolve()


def build_activity_store() -> ActivityStore:
    return ActivityStore(runtime_data_dir() / "activity.jsonl")


def build_memory_store() -> MemoryStore:
    return MemoryStore(
        runtime_data_dir() / "memories.jsonl",
        reflection_interval=int(
            os.getenv("JOB_AGENT_REFLECTION_INTERVAL", "3")
        ),
    )


def build_orchestrator(
    registry: RoleRegistry | None = None,
    memory_store: MemoryStore | None = None,
):
    baseline = MultiAgentOrchestrator(
        registry=registry or build_registry(),
        model_client=OpenAICompatibleClient(),
        max_concurrency=int(os.getenv("JOB_AGENT_MAX_CONCURRENCY", "4")),
        activity_store=build_activity_store(),
        memory_store=memory_store or build_memory_store(),
    )
    orchestrator_kind = os.getenv(
        "JOB_AGENT_ORCHESTRATOR",
        "langgraph",
    ).strip().lower()
    if orchestrator_kind == "asyncio":
        return baseline
    if orchestrator_kind == "langgraph":
        try:
            from .langgraph_orchestrator import LangGraphOrchestrator
        except ImportError as exc:
            raise RuntimeError(
                "LangGraph 未安装；请运行 uv sync"
            ) from exc
        return LangGraphOrchestrator(baseline)
    raise ValueError(
        "JOB_AGENT_ORCHESTRATOR 必须是 asyncio 或 langgraph"
    )
