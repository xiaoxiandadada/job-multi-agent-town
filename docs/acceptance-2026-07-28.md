# Agent 小镇端到端验收（2026-07-28）

本记录只保存公开仓库可以安全展示的结果，不包含 App ID、App Secret、API Key、
用户 ID、群 ID 或租户信息。

## 验收结论

`AI 求职 Agent 小镇` 已完成本地 Docker、LangGraph、RPG 可视化、飞书多机器人、
分角色日报和真实角色问答的端到端验收。

## 可复核证据

| 检查项 | 结果 | 复核入口 |
| --- | --- | --- |
| 独立飞书身份 | 1 个总控 + 7 个角色，8/8 长连接在线 | `docker compose logs bot` |
| 角色范围 | 岗位侦察、JD、岗位知识、简历、作品、面试、Judge | `configs/roles.json` |
| 编排器 | `LangGraphOrchestrator` 健康 | `GET /health` |
| 状态图 | route → context → action → judge | `src/job_agent_harness/langgraph_orchestrator.py` |
| 角色小镇 | 7 个建筑、状态、日程、长期记忆和真实轨迹回放 | `GET /api/town` |
| 动态角色 | 网页/API/飞书命令共用版本化 Role Registry | `POST /api/roles`、`/role-add` |
| 分角色日报 | 7 个角色机器人分别收到对应日报内容 | `job-agent-push-daily` |
| 飞书群问答 | `@作品教练` → 作品 Agent → Judge → 原群回复 | `run=301c699e` |
| 自动化测试 | 72 项通过 | `uv run pytest -q` |
| 容器稳定性 | bot 与 API 均运行，bot restart count 为 0 | `docker compose ps` |

## 真实问答验收

在小镇群中向 `@作品教练` 提问：

> 请用三点说明这个 Agent 小镇在 GitHub 上的可验证亮点，控制在 150 字。

最终回复经过 1 个作品角色与 Judge 两次模型调用，耗时 17.0 秒、0 失败。回复只保留
了三类可由仓库复核的事实，并给出对应源码/测试路径：

1. LangGraph 分阶段协作；
2. 真实 ActivityEvent 驱动的 RPG 小镇与认知记忆；
3. 飞书多机器人、动态角色与分角色推送。

这次验收同时发现并修复了一个质量问题：初次回答曾把项目错误扩写为 Flask、登录、
CRUD 和外部 PR。现在 `configs/project-context.md` 作为版本化事实表注入
`local_docs` 角色与 Judge；Judge 输入明确把角色输出视为不可信草稿，要求项目断言
必须有仓库路径或复核命令。相关约束由 `tests/test_model_client.py`、
`tests/test_orchestrator.py` 和 `tests/test_langgraph_orchestrator.py` 覆盖。

## RPG 回放验收

真实 `portfolio_coach` 运行产生 7 个 ActivityEvent：

1. `run_started`
2. `route_completed`
3. `phase_started`
4. `plan_updated`
5. `agent_started`
6. `agent_completed`
7. `run_completed`

历史回放在第 5/7 步只显示当时已经发生的 ACTION 阶段、Agent 工作状态和事件时间，
不会泄漏未来完成状态或未来长期记忆。演示图见
`docs/assets/agent-town-replay.jpg`。

## 公开仓库复核命令

```bash
uv sync --extra dev --locked
uv run pytest -q
sed -n '/<script>/,/<\/script>/p' web/index.html \
  | sed '1d;$d' \
  | node --check
docker compose --profile api up --build -d
curl http://127.0.0.1:8000/health
```

公开前仍应在 GitHub 开启 secret scanning，并通过部署平台的 Secret/Environment
Variables 注入模型 API 与飞书凭证，绝不提交 `.env`。
