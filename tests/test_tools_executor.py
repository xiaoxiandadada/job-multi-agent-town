import json

import httpx

from job_agent_harness.tools_executor import LocalJobTools, html_to_text


JOB_TABLE = """# 2027 届秋招岗位表

| 公司 | 岗位 | 岗位方向 | JD链接 | 缺口技能 | 是否投递 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 拼多多 | AI Agent 研发工程师 | Agent | https://careers.pddglobalhr.com/campus/1 | LangGraph | 否 | 提前批 |
| 百度 | 大模型算法工程师 | LLM | https://talent.baidu.com/jobs/2 | RLHF | 是 | 已投 |
"""


def build_tools(tmp_path, **changes):
    (tmp_path / "jobs").mkdir(exist_ok=True)
    (tmp_path / "jobs" / "autumn_job_tracker.md").write_text(
        JOB_TABLE, encoding="utf-8"
    )
    options = {"prepare_dir": tmp_path, "allowed_domains": []}
    options.update(changes)
    return LocalJobTools(**options)


def test_tracked_jobs_parses_real_rows(tmp_path):
    tools = build_tools(tmp_path)

    jobs = tools.tracked_jobs()

    assert [job["公司"] for job in jobs] == ["拼多多", "百度"]
    assert jobs[0]["岗位"] == "AI Agent 研发工程师"
    assert jobs[0]["官方链接"] == "https://careers.pddglobalhr.com/campus/1"
    assert jobs[1]["是否投递"] == "是"


def test_missing_job_table_is_reported_not_faked(tmp_path):
    tools = LocalJobTools(prepare_dir=tmp_path / "nope", allowed_domains=[])

    assert tools.tracked_jobs() == []


async def test_list_tracked_jobs_returns_json(tmp_path):
    tools = build_tools(tmp_path)

    payload = json.loads(await tools.run("list_tracked_jobs", {}))

    assert payload[0]["公司"] == "拼多多"


def test_allowlist_comes_from_the_job_table(tmp_path):
    tools = build_tools(tmp_path)

    assert tools.allowed_domains() == {
        "careers.pddglobalhr.com",
        "talent.baidu.com",
    }
    assert tools.is_allowed("https://careers.pddglobalhr.com/campus/1")
    assert tools.is_allowed("https://sub.talent.baidu.com/jobs/2")
    assert not tools.is_allowed("https://evil.example.com/jobs")


def test_operator_can_add_domains(tmp_path):
    tools = build_tools(tmp_path, allowed_domains=["jobs.bytedance.com"])

    assert tools.is_allowed("https://jobs.bytedance.com/campus")


def test_private_and_non_http_targets_are_refused(tmp_path):
    tools = build_tools(tmp_path, allowed_domains=["127.0.0.1", "169.254.169.254"])

    assert not tools.is_allowed("http://127.0.0.1:8000/api/roles")
    assert not tools.is_allowed("http://169.254.169.254/latest/meta-data/")
    assert not tools.is_allowed("file:///etc/passwd")


async def test_fetch_refuses_unlisted_domains(tmp_path):
    tools = build_tools(tmp_path)

    output = await tools.run("fetch_url", {"url": "https://evil.example.com/jd"})

    assert output.startswith("抓取被拒绝：域名未授权")
    assert "JOB_AGENT_FETCH_ALLOWED_DOMAINS" in output
    assert tools.fetched_urls == []


async def test_fetch_returns_quotable_text_and_records_the_source(tmp_path):
    page = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<h1>AI Agent 研发工程师</h1>"
        "<p>岗位职责：搭建多智能体编排框架。</p>"
        "<script>track()</script>"
        "<p>任职要求：熟悉 LangGraph &amp; 检索增强。</p>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Language"].startswith("zh-CN")
        return httpx.Response(
            200,
            content=page.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    tools = build_tools(tmp_path, transport=httpx.MockTransport(handler))
    url = "https://careers.pddglobalhr.com/campus/1"

    output = await tools.run("fetch_url", {"url": url})

    assert output.startswith(f"来源：{url}")
    assert "岗位职责：搭建多智能体编排框架。" in output
    assert "任职要求：熟悉 LangGraph & 检索增强。" in output
    assert "track()" not in output
    assert "color:red" not in output
    assert tools.fetched_urls == [url]


async def test_http_errors_are_reported_verbatim(tmp_path):
    tools = build_tools(
        tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )

    output = await tools.run(
        "fetch_url", {"url": "https://talent.baidu.com/jobs/2"}
    )

    assert output == "抓取失败：HTTP 404 https://talent.baidu.com/jobs/2"
    assert tools.fetched_urls == []


async def test_network_errors_are_reported_not_hidden(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    tools = build_tools(tmp_path, transport=httpx.MockTransport(handler))

    output = await tools.run(
        "fetch_url", {"url": "https://talent.baidu.com/jobs/2"}
    )

    assert output.startswith("抓取失败：ConnectTimeout")


async def test_javascript_only_pages_ask_for_human_verification(tmp_path):
    tools = build_tools(
        tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"<html><body><div id='app'></div></body></html>",
                headers={"Content-Type": "text/html"},
            )
        ),
    )

    output = await tools.run(
        "fetch_url", {"url": "https://talent.baidu.com/jobs/2"}
    )

    assert "正文需人工核验" in output


async def test_unknown_tool_names_are_reported(tmp_path):
    tools = build_tools(tmp_path)

    assert await tools.run("rm_rf", {}) == "未知工具：rm_rf"


def test_html_to_text_truncates_long_pages():
    payload = "<p>" + ("岗" * 500) + "</p>"

    text = html_to_text(payload, max_chars=100)

    assert len(text) < 200
    assert text.endswith("[…正文已截断…]")


def test_tool_definitions_expose_both_tools(tmp_path):
    tools = build_tools(tmp_path)

    assert tools.tool_names == {"list_tracked_jobs", "fetch_url"}
    definitions = {item["name"]: item for item in tools.definitions()}
    assert definitions["fetch_url"]["input_schema"]["required"] == ["url"]
