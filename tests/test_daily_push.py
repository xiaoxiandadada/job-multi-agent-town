from types import SimpleNamespace

import job_agent_harness.push_daily as push_module
from job_agent_harness.activity import ActivityStore
from job_agent_harness.cognition import MemoryStore
from job_agent_harness.feishu_channel import FeishuBotBinding
from job_agent_harness.models import AgentResult, RoleSpec, RunMetrics, RunReport
from job_agent_harness.registry import RoleRegistry
from job_agent_harness.tasks import TaskGraphStore


class FakeFeishuChannel:
    def __init__(self, *, app_id: str, app_secret: str):
        self.app_id = app_id

    async def send(self, chat_id, message):
        return SimpleNamespace(success=True, error=None)


class SectionWritingOrchestrator:
    """Answers each daily section with content naming the role that was asked."""

    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        role_id = request.requested_roles[0]
        return RunReport(
            run_id=f"brief-{len(self.requests)}",
            query=request.query,
            mode=request.mode,
            role_registry_version=1,
            results=[
                AgentResult(
                    role_id=role_id,
                    display_name=role_id,
                    output=f"{role_id} 的可核验产出",
                    status="ok",
                )
            ],
            final_output=(
                f"- 今日主题：{role_id} 负责的内容\n"
                "- 最优先投递：ShadowWeave Agent Memory 岗位，截止 08-20\n"
                "- 具体题目：LeetCode 146 LRU Cache"
            ),
            metrics=RunMetrics(
                selected_roles=1,
                completed_roles=1,
                failed_roles=0,
                model_calls=1,
                wall_latency_ms=1200,
                sum_agent_latency_ms=1100,
                input_tokens=100,
                output_tokens=200,
                parallel_speedup_estimate=1.0,
            ),
        )


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
        display_name="Portfolio Coach",
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
        display_name="Portfolio Coach",
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


async def test_a_scheduled_push_writes_the_brief_when_nobody_did(
    tmp_path,
    monkeypatch,
):
    """The whole morning, end to end, on a day with no hand-written brief.

    This is the case that used to end the push with ``FileNotFoundError`` — the
    reason "every day" quietly meant "the days somebody remembered". The brief has
    to be produced, parsed into role messages and delivered in one go.
    """

    prepare = tmp_path / "prepare"
    (prepare / "daily").mkdir(parents=True)
    data_dir = tmp_path / "runtime"
    data_dir.mkdir()
    registry = RoleRegistry(data_dir / "roles.json")
    registry.replace_all(
        [
            RoleSpec(
                role_id=role_id,
                display_name=role_id,
                goal=f"{role_id} 的可核验目标",
                system_prompt="只输出可核验内容。",
            )
            for role_id in [
                "job_scout",
                "job_knowledge_curator",
                "resume_strategist",
                "portfolio_coach",
                "interview_coach",
                "judge",
            ]
        ]
    )
    sent: list[tuple[str, str]] = []

    class RecordingChannel:
        def __init__(self, *, app_id: str, app_secret: str):
            self.app_id = app_id

        async def send(self, chat_id, message):
            sent.append((self.app_id, message["markdown"]))
            return SimpleNamespace(success=True, error=None)

    orchestrator = SectionWritingOrchestrator()
    monkeypatch.setenv("JOB_AGENT_FEISHU_CHAT_ID", "oc_test")
    monkeypatch.setattr(push_module, "prepare_directory", lambda: prepare)
    monkeypatch.setattr(push_module, "runtime_data_dir", lambda: data_dir)
    monkeypatch.setattr(push_module, "build_registry", lambda: registry)
    monkeypatch.setattr(
        push_module,
        "build_orchestrator",
        lambda registry, memory_store: orchestrator,
    )
    monkeypatch.setattr(
        push_module,
        "build_activity_store",
        lambda: ActivityStore(data_dir / "activity.jsonl"),
    )
    monkeypatch.setattr(
        push_module,
        "build_memory_store",
        lambda: MemoryStore(data_dir / "memories.jsonl"),
    )
    monkeypatch.setattr(
        push_module,
        "build_task_graph_store",
        lambda: TaskGraphStore(data_dir / "task_graphs"),
    )
    monkeypatch.setattr(
        push_module,
        "load_bot_bindings",
        lambda registry: [
            FeishuBotBinding(
                app_id="cli_controller",
                app_secret="secret",
                display_name="Chief of Staff",
            ),
            FeishuBotBinding(
                app_id="cli_scout",
                app_secret="secret",
                display_name="Job Scout",
                role_id="job_scout",
            ),
        ],
    )
    monkeypatch.setattr(push_module, "FeishuChannel", RecordingChannel)

    await push_module.push_daily("2026-08-05", full=False, ensure_brief=True)

    # Written to our own runtime dir, never into the user's prepare tree.
    assert (data_dir / "daily" / "2026-08-05.md").exists()
    assert list((prepare / "daily").iterdir()) == []
    # The controller sent something, and so did the one bound role bot.
    assert {app_id for app_id, _ in sent} == {"cli_controller", "cli_scout"}
    scout_messages = [text for app_id, text in sent if app_id == "cli_scout"]
    assert scout_messages, "角色机器人没有收到任何内容"
    assert "没有识别到该角色对应的新内容" not in "\n".join(scout_messages)
