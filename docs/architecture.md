# Multi-Agent 求职 Harness：开源调研与技术设计

日期：2026-07-23

## 调研结论

项目应把飞书视为 Channel，而不是 Agent Runtime：

```text
飞书消息/卡片
  -> 一个总控机器人 + 多个可选角色机器人
  -> 飞书官方 SDK（每个应用身份一条 WebSocket 或 webhook）
  -> 独立 Multi-Agent Runtime
  -> 用户提供的模型 API
  -> 飞书结果卡片
```

### 参考实现

1. [Edict](https://github.com/cft0808/edict) 展示了“分拣 → 规划 → 审核 →
   并行执行 → 回报”的多角色治理、实时看板、角色/模型配置和审计轨迹。我们借鉴
   它的 review gate 与并行 dispatch，不照搬 OpenClaw 运行时。
2. [飞书官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
   提供消息归一化、WebSocket/webhook、消息回复、卡片回调和 token 管理；
   `FeishuChannel` 适合作为对话 Bot 的单一入口。
3. [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
   定义 State、Node、Edge、super-step 和 StateGraph 的运行模型。
4. [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
   说明 checkpoint、thread、回放、故障恢复和跨轮记忆。
5. [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
   提供人工审批暂停与使用同一 `thread_id` 恢复执行的机制。
6. [飞书 OpenAPI MCP](https://github.com/larksuite/lark-openapi-mcp)
   可作为后续工具层，让 Agent 操作文档、消息和日历；它不是模型服务。

## 求职角色

| 角色 | 输入 | 输出 | 默认触发 |
| --- | --- | --- | --- |
| Job Scout | 方向、城市、届别 | 可核验岗位候选 | 岗位、秋招、校招 |
| JD Analyst | JD | 关键词、硬要求、缺口 | JD、职责、要求 |
| Job Knowledge Curator | JD、目标方向 | 知识地图、技术栈、面试题与学习优先级 | 岗位知识、技术栈、Agent、AI for Science、生信 |
| Resume Strategist | JD、简历证据 | 简历版本、bullet | 简历、bullet |
| Portfolio Coach | 目标岗位、项目 | MVP、证据、指标 | 作品、项目 |
| Interview Coach | JD、缺口 | 问题、追问、评分表 | 面试、题目 |
| Judge | 所有结构化结果 | 冲突、证据、最终建议 | 每次聚合 |

## 当前框架与调用链

默认运行时是 LangGraph，不以 CrewAI 或 AutoGen 作为运行时：

- LangGraph `StateGraph`：route/context/action/judge 的状态、边和 checkpoint。
- Python `asyncio`：阶段内部并发、并发上限和超时控制。
- Pydantic：角色、请求、结果和指标的数据契约。
- FastAPI：网页与 HTTP API。
- 飞书官方 `lark-oapi` SDK：WebSocket 长连接和消息收发。
- Multi-Bot Binding：每个飞书 App ID 绑定零个（总控）或一个 `role_id`；多个
  身份共享同一 Runtime、模型客户端、并发信号量和 Judge。
- `httpx`：调用用户自己的 OpenAI-compatible API。
- JSON `RoleRegistry`：版本化持久化角色，一键新增后立即参与路由。

项目保留纯 `asyncio` baseline。两者复用同一个
`RoleSpec`、RoleRegistry、模型客户端和 RunReport，因此可以在不改变角色提示词的
情况下做公平对照。

LangGraph 把工作流建模成 `State + Nodes + Edges`：State 保存当前任务快照，Node
执行角色或确定性逻辑，Edge 决定下一步；并行节点处在同一个 super-step。官方
Persistence/Checkpoint 还能支持对话记忆、故障恢复、回放和人工审批 interrupt。

当前求职图实现为：

```text
START
  |
 route
  |
  +-- 有上游角色 --> context_phase --+
  |                                   |
  +-- 无上游角色 ---------------------+--> action_phase
                                      |        |
                                      +------> judge --> END
```

- `route`：规则选择角色，不消耗模型。
- `context_phase`：岗位侦察、JD、岗位知识并行。
- `action_phase`：简历、作品、面试基于上游结果并行。
- `judge`：证据审核与最终排版。
- Graph State：请求、角色 ID、两阶段结果、最终输出和是否调用 Judge。
- `thread_id`：与 run ID 对齐，为 checkpoint、恢复和多轮追问预留。

当前图使用 `InMemorySaver` 保存进程内 checkpoint，并把跨进程可读的
`ActivityEvent` 追加到 `data/runtime/activity.jsonl`，网页每 1.5 秒聚合展示
每个角色的 running/ok/error/timeout、模型、耗时和输出摘要。需要跨重启恢复时应换
SQLite/Postgres checkpointer。需要人工确认投递、修改简历或发送材料时，可以在
副作用节点前用 `interrupt()` 暂停，用户在飞书点击确认后再用相同 `thread_id`
恢复。这样 LangGraph 的价值在“可恢复状态”，而不是简单替代一次
`asyncio.gather`。

切换方式：

```bash
uv sync
JOB_AGENT_ORCHESTRATOR=langgraph uv run job-agent-feishu
```

Mock 对照中，同一 5-worker + Judge 任务的最新一次结果为：`asyncio` baseline
157.5 ms，LangGraph adapter 168.1 ms；额外开销约 10.6 ms。这个结果只衡量本地编排
开销，真实任务仍应比较质量、p50/p95、token、失败恢复率与人工审批能力。

`collaborative` 模式的真实数据流是：

```text
用户任务
   |
   +--> Job Scout --------+
   +--> JD Analyst -------+  第一阶段并行
   +--> Knowledge Curator-+
                           |
                上游结果合并为带证据上下文
                           |
   +--> Resume Strategist-+
   +--> Portfolio Coach --+  第二阶段并行
   +--> Interview Coach --+
                           |
                         Judge
                           |
                   最终回复 + Run Metrics
```

命令预设只选择所需角色。例如 `/job` 只执行第一阶段三个角色，`/apply` 先执行
JD 与岗位知识，再把结果交给简历和作品角色；`/team` 才运行六个工作角色。这样能
避免无关 Agent 消耗延迟和额度。

角色配置中的 `tools` 当前只是能力元数据，尚未自动执行联网搜索或本地文档检索。
需要真实工具调用时，应新增受控 Tool Executor，并把来源证据返回给 Judge，不能
仅凭 `tools: ["web_search"]` 认为已经联网核验。

## 飞书身份与逻辑角色

逻辑角色与飞书机器人身份分层：

```text
Feishu App: AI 求职 Multi-Agent -> Controller / 自动路由
Feishu App: 岗位侦察员        -> job_scout
Feishu App: JD 分析师          -> jd_analyst
Feishu App: 简历策略师          -> resume_strategist
...
                                  |
                                  v
                         shared orchestrator
```

独立 App 解决“可分别搜索、加群和 @”的问题；共享后端避免为每个 Agent 各自部署一套
服务。角色机器人绕过关键词路由，普通问题直接进入其绑定 role；工作角色完成后仍调用
共享 Judge。`judge` 自己作为独立机器人时只执行一次审查，避免重复 Judge 调用。

新增 RoleSpec 可以立即参与总控路由；要拥有新的可 @ 身份，还需要在飞书开发者后台
创建同名应用、发布机器人能力并注入凭证。完整配置见
[`feishu-multi-bot.md`](feishu-multi-bot.md)。

## 一键添加角色

角色不是硬编码的 graph node，而是持久化的 `RoleSpec`：

```json
{
  "role_id": "bioinformatics_coach",
  "display_name": "生信算法教练",
  "goal": "把岗位要求映射到生信分析项目证据",
  "system_prompt": "...",
  "trigger_keywords": ["生信", "GWAS", "单细胞"],
  "tools": ["local_docs"],
  "model_profile": "default"
}
```

网页点击“添加角色”后：

1. API 校验 `role_id`、提示词、工具 allowlist 和模型配置。
2. 角色写入版本化 Registry。
3. Router 下一次请求即可发现新角色，不需要重启。
4. Run Report 记录角色版本，保证结果可复现。

飞书端后续用交互卡片呈现同一表单；卡片回调仍调用这套 API。

## 性能设计

1. **规则先路由**：关键词和显式选择能确定角色时不调用 Router LLM。
2. **按需 fan-out**：单一问题只跑一个专家；跨领域任务才并行多个专家。
3. **上下文隔离**：每个 Agent 只收到自己的 system prompt、相关 JD/简历片段，
   不复制整段历史。
4. **并发上限**：用 semaphore 限制并发，避免 API 限流和尾延迟爆炸。
5. **超时与降级**：角色超时不阻塞整体；Judge 可基于已完成结果生成部分报告。
6. **模型分层**：普通工作角色使用 worker 模型；岗位知识角色使用独立 knowledge
   profile（未配置时复用 judge 模型），证据链关键角色使用 `reliable` profile，
   综合判断使用 judge 模型。这样可以把 reasoning 尾延迟高的角色路由到更稳定的
   指令模型，而不拖慢其他角色。
7. **缓存**：按规范化 JD hash 缓存解析；相同简历/JD 组合复用证据抽取。
8. **结构化输出**：Pydantic/JSON Schema 降低重试和解析失败。
9. **可观测性**：每个 run 记录 wall latency、agent latency、token、错误、重试和
   model profile，不记录 API Key。
10. **可恢复执行**：LangGraph 模式用 checkpointer 保存阶段状态；后续把飞书会话
    映射为 `thread_id`，支持断点恢复和多轮追问。

## Benchmark 设计

### 对照组

- B0：单一 Generalist Agent。
- B1：多 Agent 顺序执行。
- B2：多 Agent 并行执行。
- B3：动态路由 + 并行 + Judge。
- M1/M2：同一 harness 下切换不同模型或 API。
- F1/F2：`asyncio` baseline 与 LangGraph adapter。

### 数据集

首批 30 条 golden tasks：

- 10 条 JD 解析/真实性核验。
- 8 条 JD—简历证据映射。
- 6 条作品规划。
- 6 条面试问题与学习计划。

### 指标

- 任务完成率、结构化输出通过率。
- JD 证据引用正确率、岗位类型误收率。
- Judge/人工偏好胜率。
- p50/p95 端到端延迟。
- 总 token、模型调用数、估算成本。
- 并行加速比：`sequential_latency / parallel_latency`。
- 失败隔离率：单个 Agent 失败时仍能生成可用报告的比例。

## 部署选择

### 本地开发

飞书官方 SDK 支持 WebSocket 长连接，本地或普通云服务器无需公网 webhook。

### 生产

- 常驻容器：继续使用 WebSocket，配置最简单。
- Serverless：使用飞书 HTTPS webhook；事件快速确认后，把任务投入队列。
- GitHub Pages：仅展示 dashboard/demo，不能运行持续在线 Bot。

仓库公开时只提交 `.env.example`；真实模型 API 和飞书 `App Secret` 放运行环境变量。
