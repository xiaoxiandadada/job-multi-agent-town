from __future__ import annotations

import os
from pathlib import Path

from .model_client import OpenAICompatibleClient
from .orchestrator import MultiAgentOrchestrator
from .registry import RoleRegistry


ROOT = Path(__file__).resolve().parents[2]


def build_registry() -> RoleRegistry:
    data_dir = Path(os.getenv("JOB_AGENT_DATA_DIR", ROOT / "data" / "runtime"))
    return RoleRegistry.from_seed(
        runtime_path=data_dir / "roles.json",
        seed_path=ROOT / "configs" / "roles.json",
    )


def build_orchestrator() -> MultiAgentOrchestrator:
    return MultiAgentOrchestrator(
        registry=build_registry(),
        model_client=OpenAICompatibleClient(),
        max_concurrency=int(os.getenv("JOB_AGENT_MAX_CONCURRENCY", "4")),
    )

