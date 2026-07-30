from types import SimpleNamespace

import job_agent_harness.push_daily as push_module
from job_agent_harness.activity import ActivityStore
from job_agent_harness.cognition import MemoryStore
from job_agent_harness.feishu_channel import FeishuBotBinding
from job_agent_harness.models import RoleSpec
from job_agent_harness.tasks import TaskGraphStore


class FakeFeishuChannel:
    def __init__(self, *, app_id: str, app_secret: str):
        self.app_id = app_id

    async def send(self, chat_id, message):
        return SimpleNamespace(success=True, error=None)


async def test_role_daily_push_becomes_town_activity_and_memory(
    tmp_path,
    monkeypatch,
):
    activity = ActivityStore(tmp_path / "activity.jsonl")
    memories = MemoryStore(tmp_path / "memories.jsonl")
    task_graphs = TaskGraphStore(tmp_path / "task_graphs")
    binding = FeishuBotBinding(
        app_id="cli_test",
        app_secret="secret",
        display_name="作品教练",
        role_id="portfolio_coach",
    )
    monkeypatch.setenv("JOB_AGENT_FEISHU_CHAT_ID", "oc_test")
    monkeypatch.setattr(
        push_module,
        "load_daily_messages",
        lambda *args, **kwargs: ["综合日报"],
    )
    monkeypatch.setattr(
        push_module,
        "load_role_daily_messages",
        lambda *args, **kwargs: {
            "portfolio_coach": ["作品推进：完成 RPG 小镇记忆流"]
        },
    )
    monkeypatch.setattr(
        push_module,
        "load_bot_bindings",
        lambda registry: [binding],
    )
    role = RoleSpec(
        role_id="portfolio_coach",
        display_name="作品教练",
        goal="把岗位缺口转成可展示的作品动作",
        system_prompt="输出可验证的最小作品动作和验收指标。",
    )
    registry = SimpleNamespace(get=lambda role_id: role)
    monkeypatch.setattr(push_module, "build_registry", lambda: registry)
    monkeypatch.setattr(push_module, "prepare_directory", lambda: tmp_path)
    monkeypatch.setattr(push_module, "build_activity_store", lambda: activity)
    monkeypatch.setattr(push_module, "build_memory_store", lambda: memories)
    monkeypatch.setattr(
        push_module,
        "build_task_graph_store",
        lambda: task_graphs,
    )
    monkeypatch.setattr(push_module, "FeishuChannel", FakeFeishuChannel)

    await push_module.push_daily(
        "2026-07-27",
        full=False,
    )

    events = activity.read(limit=100)
    assert [event.kind for event in events] == [
        "run_started",
        "task_graph_created",
        "route_completed",
        "phase_started",
        "task_started",
        "daily_push_started",
        "task_completed",
        "daily_push_completed",
        "run_completed",
    ]
    assert events[7].role_id == "portfolio_coach"
    graph = task_graphs.get(events[0].run_id)
    assert graph.status == "completed"
    assert graph.tasks[0].role_id == "portfolio_coach"
    role_memories = memories.list(role_id="portfolio_coach")
    assert role_memories[-1].kind == "observation"
    assert "RPG 小镇记忆流" in role_memories[-1].text
