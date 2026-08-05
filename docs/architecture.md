# Multi-Agent 求职 Harness：开源调研与技术设计

日期：2026-07-23

最后更新：2026-07-30

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

- LangGraph `StateGraph`：route/discovery/analysis/action/judge 的状态、边和
  checkpoint。
- Python `asyncio`：阶段内部并发、并发上限和超时控制。
- Pydantic：角色、请求、结果和指标的数据契约。
- FastAPI：网页与 HTTP API。
- 飞书官方 `lark-oapi` SDK：WebSocket 长连接和消息收发。
- Multi-Bot Binding：每个飞书 App ID 绑定零个（总控）或一个 `role_id`；多个
  身份共享同一 Runtime、模型客户端、并发信号量和 Judge。
- `anthropic`：官方 SDK，调 Claude（含服务端 web_search / web_fetch）。
- `httpx`：本地 Tool Executor 抓官方页，以及 OpenAI-compatible 旁路。
- JSON `RoleRegistry`：版本化持久化角色、直接模型覆盖和一键新增。
- JSON `TaskGraphStore`：按 run 保存任务、依赖、验收标准、状态与进度；使用文件锁
  支持 API、Bot 和日报进程共享同一持久卷。
- append-only `MemoryStore`：保存每个角色的 observation、handoff、plan 与
  reflection；运行前按新近度、相关性和重要性检索。
- `DailyPushScheduler`：轮询式每日推送。不睡到某一刻，因为睡过 09:30 的笔记本永远
  醒不到那一刻；补推窗口有上限，已推日期落盘，flock 保证 API 与 watch 进程只有一个
  真的发。缺人工日报时由 `daily_generate` 让角色按章节现场写，产物只落在
  `data/runtime/daily/`，不进 `prepare/`。

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
  +-- job_scout ----------> discovery_phase
  |                                  |
  |                         analysis_phase
  |                         /            \
  |                  jd_analyst     knowledge_curator
  |                         \            /
  +--------------------------> action_phase
                             /      |       \
                         resume  portfolio  interview
                             \      |       /
                                judge --> END
```

- `route`：规则选择角色，不消耗模型。
- `discovery_phase`：Job Scout 先核验中国 2027 届正式校招机会。
- `analysis_phase`：JD Analyst 与 Knowledge Curator 基于侦察结果并行。
- `action_phase`：简历、作品、面试角色基于 discovery + analysis 结果并行。
- `judge`：证据审核与最终排版。
- Graph State：请求、分阶段角色 ID/结果、最终输出和是否调用 Judge。
- `thread_id`：与 run ID 对齐，为 checkpoint、恢复和多轮追问预留。

每个 run 同时创建独立任务 DAG。节点包含 `task_id`、`role_id`、`phase`、
`depends_on`、验收标准、模型、输出摘要、错误和 0–100% 进度。依赖未满足时是
`blocked`，满足后转为 `ready/running`，最终为 `completed/error/timeout`。
RPG 页面直接读取这张任务图，不根据精灵状态猜测依赖。

当前图使用 `InMemorySaver` 保存进程内 checkpoint，并把跨进程可读的
`ActivityEvent` 追加到 `data/runtime/activity.jsonl`，网页每 1.5 秒聚合展示
每个角色的 running/ok/error/timeout、模型、耗时和输出摘要。需要跨重启恢复时应换
SQLite/Postgres checkpointer。需要人工确认投递、修改简历或发送材料时，可以在
副作用节点前用 `interrupt()` 暂停，用户在飞书点击确认后再用相同 `thread_id`
恢复。这样 LangGraph 的价值在“可恢复状态”，而不是简单替代一次
`asyncio.gather`。

LangGraph checkpoint 与角色长期记忆是两个不同层次：

- checkpoint 保存一次 graph/thread 执行到哪个 Node；
- `MemoryStore` 保存角色跨 run 可复用的经验；
- ActivityEvent 是给网页和审计使用的不可变运行轨迹。

角色执行前，Runtime 从 `memories.jsonl` 取 top-k 记忆，score 为
`0.22 × recency + 0.56 × relevance + 0.22 × importance`。检索结果只作为
“可能过时、需要复核”的上下文；每次输出/失败成为 observation，analysis → action
成为 handoff，累积经验形成不含隐藏推理的 reflection。这对应 Generative Agents
论文中的 observation / planning / reflection，但语义被约束为求职任务，不模拟
无关生活行为。

relevance 是**非对称**的 IDF 加权查询覆盖率，而不是余弦式的
`|A∩B| / sqrt(|A|·|B|)`。原因是实测的：对称归一化让分母被文档长度支配，一篇 4000
字、写着岗位 ID 和官方链接的核验报告最高只能拿到 ~0.08，而一条 33 字的
「本轮计划：…」模板能拿到 0.153——检索分数和信息量是反相关的。同时 ascii
标识符（岗位 ID、run ID、模型版本）额外加权 `IDENTIFIER_WEIGHT = 4.0`，因为中文按
n-gram 切分后，一个 30 字的问题会产生 ~80 个 token，其中最稀有的往往是提问措辞
（「之前核」「是什么」）而不是内容，稀有度本身分不开这两类。

检索层还有三条与打分无关的规则，都是因为写这个文件的循环同时也在读它：

- `plan` 不参与检索。它由 `RoleSpec.schedule` 在角色动手之前生成，只能复述
  system prompt 已有的静态配置，却占了真实语料的 40%。
- 近重复抑制（`DUPLICATE_THRESHOLD = 0.82`）。反思每几条观察就写一次，彼此只差一两
  个词；4 个检索槽位装 4 种写法的同一句话，等于没有记忆。
- 排序键是 `(score, timestamp)`，时间戳只用于打平，新记忆不会压过相关记忆。

`benchmarks/eval_memory_retrieval.py` 是这套检索的回归基线，golden set 由语料里
重复出现的岗位 ID 自动构造，因此相关性是可判定的谓词而不是主观判断。修复前后（真实
语料，12 个案例，k=4）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| hit@4 | 0.000 | 1.000 |
| recall@4 | 0.000 | 0.156（上限 0.196） |
| precision@4 | 0.000 | 0.854 |
| boilerplate@4 | 1.000 | 0.000 |

`recall@4` 的上限是 k 决定的：常驻巡检每天重访同一个岗位，一个岗位有 40 条记忆写着
答案，4 个槽位装不下 40 条，所以 0.196 就是算术上限，0.156 是它的 80%。

## 读取路径

网页每 1.5 秒轮询一次，`/api/town` 与 `/api/activity` 都读 append-only 文件，而没有
任何东西会删行，所以“读整个文件再截尾”的成本会随进程运行时长无上限增长（活动流里
66% 是常驻巡检心跳）。两处都改成按需读：

- `ActivityStore.read` 从文件末尾反向按块读，取够 `limit` 条就停；
- `MemoryStore.list_by_role` 一次扫描给出全部角色的尾部记忆，`build_town_snapshot`
  不再按角色各读一遍。

实测（2.03 MB / 3033 条活动，206 条记忆，输出逐字节一致）：

| 路径 | 修复前 | 修复后 |
| --- | --- | --- |
| `ActivityStore.read(limit=300)` | 21.1 ms | 1.5 ms |
| 7 次 `list(role_id=…)` → 1 次 `list_by_role()` | 25.1 ms | 3.6 ms |
| `GET /api/town` | 58 ms | 19.9 ms |

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
   +--> Job Scout                  发现与核验
            |
            +--> JD Analyst -------+
            +--> Knowledge Curator-+  分析阶段并行
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

命令预设只选择所需角色。例如 `/job` 只执行 discovery/analysis 三个角色，`/apply` 先执行
JD 与岗位知识，再把结果交给简历和作品角色；`/team` 才运行六个工作角色。这样能
避免无关 Agent 消耗延迟和额度。

直接 `@` 某个角色或使用 `/ask <role_id>` 时仍可调用 Judge，但最终输出契约不同：
专家完整正文放在前面，Judge 只追加证据纠错，不允许覆盖或压缩专家回答，也不得混入
与当前问题无关的日报内容。消息合并时会自动闭合未成对的 Markdown 代码围栏。

角色配置中的 `tools` 现在是真的会执行的能力，不再只是元数据。`tools` 含
`web_search` 的角色会拿到 Claude 服务端 `web_search_20260209` /
`web_fetch_20260209`；网关不支持（显式 400，或收下工具却一次都不执行）时，同一次
请求会自动降级到本地 Tool Executor—— `list_tracked_jobs` 读岗位表，`fetch_url`
按域名白名单抓官方页，抓到的正文原样进上下文。两条路径都会把真实 URL 汇总进
「联网来源（本次真实抓取）」，抓不到就必须写「页面未标注」或「正文需人工核验」，
不允许用记忆里的内容顶替。本地证据仍由 `RoleContextProvider` 按角色从当天日报、
岗位表、学习计划、简历/作品材料中注入。

## 分角色模型解析

每个角色独立解析最终模型，优先级为：

```text
RoleSpec.model
  > JOB_AGENT_ROLE_MODEL_<ROLE_ID>
  > RoleSpec.model_profile 对应的 profile 模型
```

API `GET /api/models` 返回 profile、角色 override 与最终解析模型；角色卡片通过
`PATCH /api/roles/{role_id}` 写入直接 override。API Key 只由环境变量注入，
`/api/models`、任务图和 ActivityEvent 都不会返回密钥。

当前生产分层是：

- 岗位侦察、JD、简历、作品、面试、Judge：`claude-sonnet-5`；
- Knowledge Curator：`claude-opus-5`（Claude 5）；
- 角色知识 prompt 约束为能力地图、定义/直觉、架构、技术选型、生产故障、评测、
  面试问题、30/60/120 分钟学习路径和 GitHub 作品证据。

模型 A/B 只决定当前角色分层，不应被解释为对所有任务的绝对排名。

## 飞书身份与逻辑角色

逻辑角色与飞书机器人身份分层：

```text
Feishu App: Chief of Staff -> Controller / 自动路由
Feishu App: Job Scout        -> job_scout
Feishu App: JD Analyst          -> jd_analyst
Feishu App: Resume Strategist          -> resume_strategist
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
  "model_profile": "reliable",
  "workflow_stage": "action",
  "town_place": "生信实验室",
  "town_icon": "🧬",
  "schedule": ["解析生信任务", "映射分析流程", "形成可验证证据"]
}
```

网页点击“添加角色”后：

1. API 校验 `role_id`、提示词、工作阶段、建筑位置和模型配置。
2. 角色写入版本化 Registry。
3. Router 下一次请求即可发现新角色，不需要重启；`context` 角色先提供证据，
   `action` 角色接收 handoff 后执行。
4. Run Report 记录角色版本，保证结果可复现。
5. 网页可即时暂停/启用角色，建筑和日程同步更新。

飞书端后续用交互卡片呈现同一表单；卡片回调仍调用这套 API。

## 性能设计

1. **规则先路由**：关键词和显式选择能确定角色时不调用 Router LLM。
2. **按需 fan-out**：单一问题只跑一个专家；跨领域任务才并行多个专家。
3. **上下文隔离**：每个 Agent 只收到自己的 system prompt、相关 JD/简历片段，
   不复制整段历史。
4. **并发上限**：用 semaphore 限制并发，避免 API 限流和尾延迟爆炸。
5. **超时与降级**：角色超时不阻塞整体；Judge 可基于已完成结果生成部分报告。
6. **模型分层**：普通工作角色使用低延迟 reliable 模型；岗位知识角色通过
   role-specific override 使用更强模型；Judge 使用独立 profile。只有知识节点承担
   更强模型成本，不拖慢 discovery 和 action 的其他并行节点。
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
