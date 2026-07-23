"""Provider-neutral multi-agent harness for job preparation."""

from .models import AgentResult, RoleSpec, RunReport, RunRequest
from .orchestrator import MultiAgentOrchestrator
from .registry import RoleRegistry

__all__ = [
    "AgentResult",
    "MultiAgentOrchestrator",
    "RoleRegistry",
    "RoleSpec",
    "RunReport",
    "RunRequest",
]

