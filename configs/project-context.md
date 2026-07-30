# Job Agent Studio 项目事实表

本文件只记录可由当前仓库直接复核的事实，供带 `local_docs` 能力的角色和 Judge
回答“本项目/Agent 小镇/GitHub 展示”类问题时使用。

- 技术栈：Python、FastAPI、Pydantic、LangGraph、`asyncio`、飞书官方
  `lark-oapi` SDK、`httpx` 和 Docker Compose；项目不是 Flask 应用。
- 编排：默认 LangGraph 状态图为
  `route → discovery_phase → analysis_phase → action_phase → judge`。岗位侦察
  先执行，JD/岗位知识与简历/作品/面试分别在两个并行层运行；另保留 `asyncio`
  baseline 用于性能对照和降级。
- 角色：7 个独立业务角色为岗位侦察员、JD 分析师、岗位知识补充员、
  简历策略师、作品教练、面试教练和证据审核员；另有 1 个飞书总控入口。
- 飞书：每个角色可绑定独立飞书自建应用，在同一群中被单独 `@`。模型通过用户
  自己的 OpenAI-compatible API 调用，不使用飞书 AI。直接 `@` 一个角色时会保留
  专家完整正文，Judge 只追加证据审核。
- RPG 小镇：角色拥有独立建筑、坐标、图标和日程；页面状态由真实
  `ActivityEvent` 驱动，展示五阶段、handoff、状态和延迟，不是定时伪动画。
- 任务图：每个 run 持久化任务详情、依赖、验收标准、模型、状态和进度；RPG 页面
  读取真实 `TaskGraphStore`，历史回放不会泄露未来任务状态。
- 生成式认知：持久化 observation、handoff、plan 和 reflection；检索长期记忆时
  组合 relevance、importance 和 recency。
- 可审计回放：`GET /api/town?run_id=<id>&step=<n>` 可按事件时间步回放历史运行，
  回放不会显示未来事件或未来长期记忆。
- 动态角色：网页和 `POST /api/roles` 可新增逻辑角色；飞书 `/role-add <JSON>`
  使用同一注册表。新增独立可 `@` 身份仍需创建并发布对应飞书应用。
- 日报：`job-agent-push-daily` 默认由总控先发综合日报和任务拆解，再把同一日报按
  职责拆给七个角色机器人推送。
- 分角色模型：解析顺序为 `RoleSpec.model`、角色专属环境变量、model profile。
  当前岗位知识补充员使用 `Qwen/Qwen3.5-397B-A17B`，其他内置角色使用
  `Qwen/Qwen2.5-32B-Instruct`。
- 可复核材料：`README.md` 包含真实回放截图；`docs/rpg-agent-town.md` 说明认知
  和回放映射；`docs/performance-report.md` 说明 baseline、LangGraph 与模型对比。
- 本地复核命令：`uv run pytest -q`、从 `web/index.html` 提取 `<script>` 后运行
  `node --check`、`docker compose --profile api ps`。

除非当前问题提供了额外证据，否则不要声称项目使用 Flask、已有外部贡献者或 PR、
已经公开到某个 GitHub URL、已经部署到公网，或给出未经测试支持的准确率/性能提升。
也不要把 FastAPI 自动扩写成用户注册、登录、通用 CRUD、鉴权或其他本表未列出的
业务功能。

当用户要求“GitHub 上可验证的项目亮点”时，只从以下三组中选择并附证据路径：

1. LangGraph 五阶段协作与任务 DAG：
   `src/job_agent_harness/langgraph_orchestrator.py`、
   `src/job_agent_harness/tasks.py`，由
   `tests/test_langgraph_orchestrator.py` 与 `tests/test_tasks.py` 复核。
2. 真实事件驱动的 RPG 小镇、认知记忆和历史回放：
   `src/job_agent_harness/town.py`、`src/job_agent_harness/cognition.py`、
   `docs/assets/agent-town-replay.jpg`，由 `tests/test_town.py` 与
   `tests/test_cognition.py` 复核。
3. 飞书多机器人、动态角色和分角色日报：
   `src/job_agent_harness/feishu_channel.py`、
   `src/job_agent_harness/feishu_group.py`、
   `src/job_agent_harness/push_daily.py`，由对应 `tests/test_feishu_*.py` 和
   `tests/test_daily_push.py` 复核。
