from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from job_agent_harness.anthropic_client import (
    WEB_FETCH_TOOL_TYPE,
    WEB_SEARCH_TOOL_TYPE,
    AnthropicClient,
)
from job_agent_harness.models import RoleSpec


def make_role(**changes) -> RoleSpec:
    base = {
        "role_id": "job_scout",
        "display_name": "Job Scout",
        "goal": "发现并核验中国 2027 届正式校招岗位",
        "system_prompt": "只输出有官方来源的岗位。",
        "model_profile": "reliable",
        "tools": ["web_search"],
    }
    base.update(changes)
    return RoleSpec(**base)


def text_block(text: str, citations=None):
    return SimpleNamespace(type="text", text=text, citations=citations)


def search_result_block(results):
    return SimpleNamespace(
        type="web_search_tool_result",
        content=[
            SimpleNamespace(type="web_search_result", url=url, title=title)
            for url, title in results
        ],
    )


def search_error_block(code: str):
    return SimpleNamespace(
        type="web_search_tool_result",
        content=SimpleNamespace(type="web_search_tool_result_error", error_code=code),
    )


def fetch_block(url: str):
    return SimpleNamespace(
        type="web_fetch_tool_result",
        content=SimpleNamespace(type="web_fetch_result", url=url),
    )


def tool_use_block(name: str, payload: dict, *, block_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=payload, id=block_id)


def bad_request(message: str):
    """A real anthropic.BadRequestError, so the matcher is tested for real."""

    import httpx
    from anthropic import BadRequestError

    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    return BadRequestError(
        message,
        response=httpx.Response(400, request=request),
        body=None,
    )


class FakeStream:
    def __init__(self, message):
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, replies, *, broken_stream=False):
        self._replies = list(replies)
        self.broken_stream = broken_stream
        self.calls: list[dict] = []
        self.stream_calls = 0
        self.create_calls = 0

    def _next(self):
        nxt = self._replies.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        self.stream_calls += 1
        if self.broken_stream:
            # What a proxy with malformed SSE does inside the SDK accumulator.
            raise IndexError("list index out of range")
        return FakeStream(self._next())

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.create_calls += 1
        return self._next()


class FakeClient:
    """Stands in for AsyncAnthropic; records the exact request shape."""

    def __init__(self, replies, *, broken_stream=False):
        self.messages = FakeMessages(replies, broken_stream=broken_stream)
        self.options: dict = {}

    def with_options(self, **kwargs):
        self.options = kwargs
        return self


def reply(content, *, stop_reason="end_turn", stop_details=None, model="claude-sonnet-5"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=40),
    )


def build_client(replies, tmp_path, *, broken_stream=False, **changes):
    options = {
        "api_key": "test-key",
        "prepare_dir": tmp_path,
        "project_context_path": tmp_path / "missing.md",
        "seed_roles_path": tmp_path / "missing.json",
        "client": FakeClient(replies, broken_stream=broken_stream),
    }
    options.update(changes)
    return AnthropicClient(**options)


def test_knowledge_role_defaults_to_claude_5_and_others_to_sonnet(monkeypatch):
    for name in (
        "JOB_AGENT_MODEL",
        "JOB_AGENT_JUDGE_MODEL",
        "JOB_AGENT_KNOWLEDGE_MODEL",
        "JOB_AGENT_RELIABLE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    client = AnthropicClient(api_key="test-key")

    assert client.model_for(make_role(model_profile="knowledge")) == "claude-opus-5"
    assert client.model_for(make_role(model_profile="reliable")) == "claude-sonnet-5"
    assert client.model_for(make_role(model_profile="judge")) == "claude-sonnet-5"
    assert client.model_for(make_role(model_profile="default")) == "claude-sonnet-5"


def test_knowledge_role_gets_a_larger_output_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_AGENT_KNOWLEDGE_MAX_OUTPUT_TOKENS", raising=False)
    client = build_client([], tmp_path, max_output_tokens=10_000)

    assert client.max_tokens_for(make_role(model_profile="reliable")) == 10_000
    assert client.max_tokens_for(make_role(model_profile="knowledge")) == 20_000


def test_web_search_roles_get_both_server_tools(tmp_path):
    client = build_client([], tmp_path)

    tools = client.server_tools_for(make_role())

    assert [tool["type"] for tool in tools] == [
        WEB_SEARCH_TOOL_TYPE,
        WEB_FETCH_TOOL_TYPE,
    ]
    # Dynamic filtering is built into these versions; a separate code
    # execution environment would confuse the model.
    assert not any(tool["type"].startswith("code_execution") for tool in tools)
    assert tools[1]["citations"] == {"enabled": True}


def test_roles_without_web_search_get_no_server_tools(tmp_path):
    client = build_client([], tmp_path)

    assert client.server_tools_for(make_role(tools=["local_docs"])) == []


def test_web_tool_roles_are_told_to_quote_the_real_jd(tmp_path):
    client = build_client([], tmp_path)

    prompt = client.system_blocks_for(make_role())[0]["text"]

    assert "web_fetch 只能抓取本对话里已经出现过的 URL" in prompt
    assert "JD 原文摘录" in prompt


async def test_request_never_sends_temperature_and_uses_adaptive_thinking(tmp_path):
    client = build_client(
        [reply([text_block("ok")])], tmp_path, web_tools="server"
    )

    await client.complete(make_role(), "找 2027 届 AI Agent 岗位")

    sent = client.client().messages.calls[0]
    assert "temperature" not in sent
    assert "top_p" not in sent
    assert sent["thinking"] == {"type": "adaptive"}
    assert isinstance(sent["system"], list)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert client.client().options["timeout"] == make_role().timeout_seconds


async def test_effort_is_only_sent_when_configured(tmp_path):
    client = build_client(
        [reply([text_block("ok")])], tmp_path, effort="medium", web_tools="server"
    )

    await client.complete(make_role(), "查岗位")

    assert client.client().messages.calls[0]["output_config"] == {"effort": "medium"}


async def test_a_role_can_set_its_own_effort(tmp_path):
    # A finer lever than swapping models: the planner routes at "low" while the
    # analyst reasons at "high", on the same deployment.
    client = build_client(
        [reply([text_block("ok")])], tmp_path, effort="medium", web_tools="server"
    )

    await client.complete(make_role(effort="high"), "拆解这个 JD")

    assert client.client().messages.calls[0]["output_config"] == {"effort": "high"}


async def test_a_role_with_no_effort_inherits_the_deployment_default(tmp_path):
    client = build_client(
        [reply([text_block("ok")])], tmp_path, effort="medium", web_tools="server"
    )

    await client.complete(make_role(effort=None), "查岗位")

    assert client.client().messages.calls[0]["output_config"] == {"effort": "medium"}


async def test_no_effort_anywhere_sends_no_output_config(tmp_path):
    """Absent is not the same as "high" — Haiku 4.5 rejects the parameter.

    A role pinned to a model that has no effort support has to be able to end up
    with no ``output_config`` at all; inheriting a level from the environment
    would turn every one of its calls into a 400.
    """

    client = build_client(
        [reply([text_block("ok")])], tmp_path, effort="", web_tools="server"
    )

    await client.complete(make_role(effort=None), "查岗位")

    assert "output_config" not in client.client().messages.calls[0]


def test_effort_rejects_a_level_the_api_does_not_have():
    # Catch the typo when roles.json loads, not as a provider 400 mid-run.
    with pytest.raises(ValidationError):
        make_role(effort="very-high")


def test_web_search_still_grants_both_local_tools(tmp_path):
    client = build_client([reply([text_block("ok")])], tmp_path, web_tools="local")
    role = make_role(tools=["web_search"])

    assert client.local_tool_names_for(role) == ["list_tracked_jobs", "fetch_url"]
    names = [tool["name"] for tool in client.local_tools_for(role)]
    assert names == ["list_tracked_jobs", "fetch_url"]


def test_a_role_can_be_given_fetch_url_without_search(tmp_path):
    """The judge's case: verify a cited link, don't go discover new jobs.

    ``list_tracked_jobs`` would let it introduce jobs no upstream role mentioned,
    which its own prompt forbids — so the narrow grant has to be expressible.
    """

    client = build_client([reply([text_block("ok")])], tmp_path)
    role = make_role(role_id="judge", tools=["local_docs", "fetch_url"])

    assert client.local_tool_names_for(role) == ["fetch_url"]
    assert [tool["name"] for tool in client.local_tools_for(role)] == ["fetch_url"]
    # Naming a local tool pins the local executor: there is no server-side
    # "fetch but never search" pair to fall back to.
    assert client.web_mode_for(role) == "local"


def test_a_narrow_grant_does_not_advertise_the_other_tool(tmp_path):
    """A described-but-uncallable tool makes the model try it and then apologise.

    The judge only gets ``fetch_url``, so the guidance must point it at links
    already in the conversation — the default wording points at
    ``list_tracked_jobs``, a tool it does not have.
    """

    client = build_client([reply([text_block("ok")])], tmp_path)
    prompt = client.system_blocks_for(
        make_role(role_id="judge", tools=["fetch_url"])
    )[0]["text"]

    assert "fetch_url" in prompt
    assert "list_tracked_jobs" not in prompt
    assert "上文已经出现过" in prompt


def test_a_verifier_does_not_inherit_the_job_entry_output_contract(tmp_path):
    """The judge returns an audit verdict, not a list of companies and JD quotes.

    Appending the discovery output contract to a verifier contradicts its own
    system prompt, and the model then has to pick one.
    """

    client = build_client([reply([text_block("ok")])], tmp_path)

    verifier = client.system_blocks_for(
        make_role(role_id="judge", tools=["fetch_url"])
    )[0]["text"]
    scout = client.system_blocks_for(
        make_role(tools=["web_search"])
    )[0]["text"]

    assert "输出具体度要求" not in verifier
    # The role whose job *is* producing entries still gets it.
    assert "输出具体度要求" in scout


def test_list_tracked_jobs_alone_reads_the_table_without_web_access(tmp_path):
    client = build_client([reply([text_block("ok")])], tmp_path)
    role = make_role(role_id="material_builder", tools=["list_tracked_jobs"])

    assert client.local_tool_names_for(role) == ["list_tracked_jobs"]
    prompt = client.system_blocks_for(role)[0]["text"]
    assert "list_tracked_jobs" in prompt
    assert "fetch_url" not in prompt


def test_a_role_with_no_evidence_tools_gets_none(tmp_path):
    client = build_client([reply([text_block("ok")])], tmp_path)
    role = make_role(role_id="interview_coach", tools=["local_docs"])

    assert client.local_tool_names_for(role) == []
    assert client.web_mode_for(role) == "off"
    assert client.local_tools_for(role) == []


async def test_fetched_urls_are_reported_as_real_sources(tmp_path):
    content = [
        search_result_block(
            [("https://talent.baidu.com/jobs/1", "百度 2027 届算法")]
        ),
        fetch_block("https://careers.pddglobalhr.com/campus/grad/detail"),
        text_block(
            "拼多多 AI Agent 研发工程师",
            citations=[
                SimpleNamespace(
                    type="char_location",
                    url="https://careers.pddglobalhr.com/campus/grad/detail",
                    title="拼多多提前批",
                )
            ],
        ),
    ]
    client = build_client([reply(content)], tmp_path)

    result = await client.complete(make_role(), "查岗位")

    assert "拼多多 AI Agent 研发工程师" in result.content
    assert "联网来源（本次真实抓取）" in result.content
    assert "https://talent.baidu.com/jobs/1" in result.content
    assert "https://careers.pddglobalhr.com/campus/grad/detail" in result.content
    assert result.input_tokens == 100
    assert result.output_tokens == 40


async def test_server_tool_errors_surface_instead_of_silently_degrading(tmp_path):
    content = [search_error_block("max_uses_exceeded"), text_block("部分结论")]
    client = build_client([reply(content)], tmp_path)

    result = await client.complete(make_role(), "查岗位")

    assert "web_search 未完成：max_uses_exceeded" in result.content


async def test_pause_turn_is_resumed_without_a_continue_message(tmp_path):
    paused = reply([text_block("第一段")], stop_reason="pause_turn")
    finished = reply([text_block("第二段")])
    client = build_client([paused, finished], tmp_path, web_tools="server")

    result = await client.complete(make_role(), "查岗位")

    calls = client.client().messages.calls
    assert len(calls) == 2
    resumed = calls[1]["messages"]
    assert [item["role"] for item in resumed] == ["user", "assistant"]
    assert resumed[1]["content"] is paused.content
    assert "第一段" in result.content and "第二段" in result.content
    assert result.input_tokens == 200


async def test_pause_turn_is_capped_and_reported(tmp_path):
    paused = [
        reply([text_block(f"片段{index}")], stop_reason="pause_turn")
        for index in range(3)
    ]
    client = build_client(paused, tmp_path, max_continuations=2, web_tools="server")

    result = await client.complete(make_role(), "查岗位")

    assert len(client.client().messages.calls) == 3
    assert "JOB_AGENT_CLAUDE_MAX_CONTINUATIONS" in result.content


async def test_refusal_is_returned_as_readable_text(tmp_path):
    refused = reply(
        [],
        stop_reason="refusal",
        stop_details=SimpleNamespace(
            category="policy", explanation="请求涉及受限内容"
        ),
    )
    client = build_client([refused], tmp_path, web_tools="server")

    result = await client.complete(make_role(), "查岗位")

    assert "模型拒绝回答" in result.content
    assert "policy" in result.content
    assert "请求涉及受限内容" in result.content


async def test_output_cap_triggers_an_automatic_continuation(tmp_path):
    client = build_client(
        [
            reply([text_block("岗位一：拼多多")], stop_reason="max_tokens"),
            reply([text_block("岗位二：百度")]),
        ],
        tmp_path,
        web_tools="server",
    )

    result = await client.complete(make_role(), "查岗位")

    calls = client.client().messages.calls
    assert len(calls) == 2
    followup = calls[1]["messages"]
    assert [item["role"] for item in followup] == ["user", "assistant", "user"]
    assert "从截断的位置继续写完" in followup[2]["content"]
    assert "岗位一：拼多多" in result.content
    assert "岗位二：百度" in result.content
    # It finished on the second round, so no truncation warning.
    assert "JOB_AGENT_MAX_OUTPUT_TOKENS" not in result.content


async def test_truncated_output_says_so_after_continuations_run_out(tmp_path):
    client = build_client(
        [
            reply([text_block(f"第{index}段")], stop_reason="max_tokens")
            for index in range(3)
        ],
        tmp_path,
        max_continuations=2,
        web_tools="server",
    )

    result = await client.complete(make_role(), "查岗位")

    assert len(client.client().messages.calls) == 3
    assert "JOB_AGENT_MAX_OUTPUT_TOKENS" in result.content


async def test_web_tools_can_be_disabled(tmp_path):
    client = build_client([reply([text_block("ok")])], tmp_path, web_tools=False)

    await client.complete(make_role(), "查岗位")

    assert "tools" not in client.client().messages.calls[0]


def test_missing_model_configuration_fails_loudly(tmp_path):
    client = build_client([], tmp_path, default_model="x")
    client.default_model = ""
    client.reliable_model = ""

    with pytest.raises(RuntimeError, match="Claude 模型未配置"):
        import asyncio

        asyncio.run(client.complete(make_role(), "查岗位"))


# --------------------------------------------------------------- local tools


def write_job_table(tmp_path):
    table = tmp_path / "jobs"
    table.mkdir(exist_ok=True)
    (table / "autumn_job_tracker.md").write_text(
        "# 岗位表\n\n"
        "| 公司 | 岗位 | 岗位方向 | JD链接 | 缺口技能 | 是否投递 | 备注 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 拼多多 | AI Agent 研发工程师 | Agent | "
        "https://careers.pddglobalhr.com/campus/1 | LangGraph | 否 | 提前批 |\n",
        encoding="utf-8",
    )
    return tmp_path


async def test_local_tool_loop_executes_and_reports_the_fetched_page(tmp_path):
    write_job_table(tmp_path)
    asking = reply(
        [tool_use_block("list_tracked_jobs", {}, block_id="t1")],
        stop_reason="tool_use",
    )
    finished = reply([text_block("拼多多 AI Agent 研发工程师，官方链接已核对")])
    client = build_client([asking, finished], tmp_path, web_tools="local")

    result = await client.complete(make_role(), "查岗位")

    calls = client.client().messages.calls
    assert len(calls) == 2
    assert [tool["name"] for tool in calls[0]["tools"]] == [
        "list_tracked_jobs",
        "fetch_url",
    ]
    followup = calls[1]["messages"]
    assert [item["role"] for item in followup] == ["user", "assistant", "user"]
    tool_result = followup[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "t1"
    assert "careers.pddglobalhr.com" in tool_result["content"]
    assert "官方链接已核对" in result.content


async def test_local_tool_loop_is_capped(tmp_path):
    write_job_table(tmp_path)
    asking = [
        reply(
            [tool_use_block("list_tracked_jobs", {}, block_id=f"t{index}")],
            stop_reason="tool_use",
        )
        for index in range(3)
    ]
    client = build_client(
        asking, tmp_path, web_tools="local", max_tool_iterations=2
    )

    result = await client.complete(make_role(), "查岗位")

    assert len(client.client().messages.calls) == 3
    assert "JOB_AGENT_CLAUDE_MAX_TOOL_ITERATIONS" in result.content


async def test_unsupported_server_tools_downgrade_to_the_local_executor(tmp_path):
    write_job_table(tmp_path)
    rejected = bad_request(
        "ValidationException: tool type 'web_search_20260209' "
        "is not supported for this model"
    )
    finished = reply([text_block("已用本地工具核对岗位表")])
    client = build_client([rejected, finished], tmp_path)

    result = await client.complete(make_role(), "查岗位")

    calls = client.client().messages.calls
    assert [tool["type"] for tool in calls[0]["tools"]] == [
        WEB_SEARCH_TOOL_TYPE,
        WEB_FETCH_TOOL_TYPE,
    ]
    # Second attempt swaps in the locally implemented tools.
    assert [tool["name"] for tool in calls[1]["tools"]] == [
        "list_tracked_jobs",
        "fetch_url",
    ]
    assert "list_tracked_jobs" in calls[1]["system"][0]["text"]
    assert client._server_tools_supported is False
    assert "已自动切换到本地 Tool Executor" in result.content
    # The downgrade is remembered, so later roles skip the failed attempt.
    assert client.web_mode_for(make_role()) == "local"


async def test_other_bad_requests_are_not_swallowed(tmp_path):
    from anthropic import BadRequestError

    client = build_client([bad_request("max_tokens: must be > 0")], tmp_path)

    with pytest.raises(BadRequestError):
        await client.complete(make_role(), "查岗位")


async def test_pinned_server_mode_does_not_downgrade(tmp_path):
    from anthropic import BadRequestError

    rejected = bad_request("tool type 'web_search_20260209' is not supported")
    client = build_client([rejected], tmp_path, web_tools="server")

    with pytest.raises(BadRequestError):
        await client.complete(make_role(), "查岗位")


async def test_silently_dropped_server_tools_also_downgrade(tmp_path):
    """A gateway that accepts the tools but never runs them.

    Observed on a Bedrock-backed gateway: the request succeeds, no
    ``server_tool_use`` block comes back, and the model prints
    ``{"query": ...}`` as prose instead of searching. The answer looks fine and
    is entirely unsourced, so it has to be caught.
    """

    write_job_table(tmp_path)
    pretended = reply([text_block('{"query": "百度 2027届 大模型研发工程师"}')])
    grounded = reply([text_block("百度 大模型研发工程师，已抓官方页")])
    client = build_client([pretended, grounded], tmp_path)

    result = await client.complete(make_role(), "查岗位")

    calls = client.client().messages.calls
    assert len(calls) == 2
    assert [tool["name"] for tool in calls[1]["tools"]] == [
        "list_tracked_jobs",
        "fetch_url",
    ]
    assert client._server_tools_supported is False
    assert "一次都没执行" in result.content


async def test_a_role_that_simply_did_not_search_only_probes_once(tmp_path):
    write_job_table(tmp_path)
    client = build_client(
        [
            reply([text_block("直接回答")]),
            reply([text_block("本地兜底回答")]),
            reply([text_block("第二次提问的回答")]),
        ],
        tmp_path,
    )

    await client.complete(make_role(), "你好")
    await client.complete(make_role(), "再问一次")

    # Three calls total, not four: the downgrade decision is remembered.
    assert len(client.client().messages.calls) == 3


async def test_malformed_sse_falls_back_to_a_non_streaming_request(tmp_path):
    """A gateway whose stream events the SDK accumulator cannot replay.

    Seen live: content_block_delta with no matching content_block_start makes
    the SDK raise a bare IndexError. Streaming is still the right default, so
    the client downgrades for the rest of the process rather than giving up.
    """

    client = build_client(
        [reply([text_block("非流式拿到的答案")])],
        tmp_path,
        web_tools="server",
        broken_stream=True,
    )

    result = await client.complete(make_role(), "查岗位")

    messages = client.client().messages
    assert messages.stream_calls == 1
    assert messages.create_calls == 1
    assert "非流式拿到的答案" in result.content
    assert client._streaming_supported is False


async def test_streaming_can_be_switched_off_explicitly(tmp_path):
    client = build_client(
        [reply([text_block("ok")])], tmp_path, web_tools="server", stream=False
    )

    await client.complete(make_role(), "查岗位")

    messages = client.client().messages
    assert messages.stream_calls == 0
    assert messages.create_calls == 1
