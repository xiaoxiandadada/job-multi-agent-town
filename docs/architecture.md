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
| Job Analyst | JD、目标方向 | 关键词、硬要求、缺口，以及缺口对应的知识地图、技术栈与面试题 | JD、职责、要求、技术栈、能力地图 |
| Material Builder | JD、简历与项目证据 | 简历版本与 bullet；作品 MVP、证据、指标 | 简历、bullet、作品、项目 |
| Interview Coach | JD、缺口 | 问题、追问、评分表 | 面试、题目 |
| Judge | 所有结构化结果 | 冲突、证据、最终建议 | 多角色产出需交叉核对时 |

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
执行角色或确定性逻辑，Edge 决定下一步。官方 Persistence/Checkpoint 支持对话记忆、
故障恢复、回放和人工审批 interrupt。

### 真实的图结构

下面这张图是 `LangGraphOrchestrator.mermaid()` 的直接输出（`graph.get_graph()
.draw_mermaid()`），不是手画的示意图。**虚线是条件边，实线是固定边**：

```text
__start__ ──▶ route
route           ┈▶ discovery_phase │ analysis_phase │ action_phase │ judge
discovery_phase ┈▶ analysis_phase  │ action_phase   │ judge
analysis_phase  ┈▶ action_phase    │ judge
action_phase    ──▶ judge
judge           ──▶ __end__
```

注意这不是一条流水线：`route` 可以**直接跳到任何阶段**。空阶段被跳过而不是跑一个
什么都不做的节点——只派了 Material Builder 的一次运行，`route` 直接进 `action_phase`，
`discovery_phase` 和 `analysis_phase` 从未执行。

- `route`：消费 Chief of Staff 的拆解结果选角色；拆解失败时回退关键词。不消耗模型。
- `discovery_phase`：Job Scout 核验岗位。
- `analysis_phase`：Job Analyst 基于侦察结果拆要求并补知识。
- `action_phase`：材料产出与面试角色基于 discovery + analysis 的结果工作。
- `judge`：按需触发的证据审核与最终排版（见「Judge 按需触发」）。

### 三种机制，都不是 callback

**这个项目没有使用 LangChain 的 callback handler 体系**——没有 `BaseCallbackHandler`，
没有 `callbacks=[...]`。`langgraph_orchestrator.py` 里 grep 不到 `callback`。
它靠三样东西串起来：

**一、条件边 + 路由函数。** 这是最接近「回调」的东西：`add_conditional_edges`
注册一个纯函数，LangGraph 在节点执行完后调用它，用返回的字符串查映射表决定下一跳。

```python
builder.add_conditional_edges(
    "route", self._after_route,
    {"discovery_phase": "discovery_phase", "analysis_phase": "analysis_phase",
     "action_phase": "action_phase", "judge": "judge"},
)

@staticmethod
def _after_route(state) -> str:
    if state.get("discovery_role_ids"): return "discovery_phase"
    if state.get("analysis_role_ids"):  return "analysis_phase"
    if state.get("action_role_ids"):    return "action_phase"
    return "judge"
```

三个路由函数（`_after_route` / `_after_discovery` / `_after_analysis`）都是
`@staticmethod` 纯函数，只读 state 不写、不调模型、不产生副作用。

**二、状态合并。** 节点返回 dict，LangGraph 合并进 state：

```python
async def _discovery_phase(self, state) -> dict:
    ...
    return {"discovery_results": [r.model_dump() for r in results]}
```

`JobAgentGraphState` 是 `TypedDict, total=False`，字段全是普通类型——**没有一个
`Annotated[list, add]` 这样的 reducer**，所以合并语义是**覆盖**而不是累加。这安全的
唯一原因是下一节：没有两个分支会同时写同一个 key。

**三、观测靠 `_record()`，不靠 LangGraph。** 每个节点直接调
`self.base._record(...)` 往 `data/runtime/activity.jsonl` 追加 `ActivityEvent`。
这是网页、回放、小镇连线的唯一数据源。选它而不选 callback handler 的实际后果：事件
**跨进程可读**（API、飞书 bot、日报进程共享同一个文件），而 callback handler 只活在
自己进程的内存里。

### LangGraph 在这里不做并行

看图就知道：**全部是串行**。没有 fan-out，没有一个节点分裂成多个并发分支。

阶段内部的并发是在**节点函数内部**用 `asyncio.gather` 做的（`_run_parallel`），
LangGraph 完全不知道有这回事——它只看到「一个节点，跑了一段时间，返回一个 dict」。
所以文档里说 `action_phase` 的两个角色「并行」，指的是 asyncio 的并行，不是 LangGraph
的 super-step 并行。后者在本项目**没有被用到**。

这解释了两件事：为什么不需要 reducer（不会有并发写冲突），以及「为什么用 LangGraph」
的真实答案——不是为了并行（`asyncio.gather` 已经够了），而是为了 checkpoint 和显式的
状态图。实测编排开销约 10.6 ms（见下方性能对照），纯 asyncio baseline 保留着做对照。

### 跨节点只传 role_id，不传对象

`JobAgentGraphState` 的字段全是 `list[str]` 和 `list[dict]`——没有 `RoleSpec` 对象。
所以每个节点都要重新从 registry 读角色：

```python
roles = [self.base.role_for_run(role_id, request)
         for role_id in state.get("action_role_ids", [])]
```

**这意味着 per-run 的覆盖必须在每个节点重新施加一遍**，`apply_run_overrides` 的
docstring 记的就是这个坑：常驻巡检把超时从 45 s 放宽到 300 s，如果 action 节点重读
角色时不重新施加，覆盖会在 route 和 action 之间静默消失，巡检仍然死在角色自己的超时上。

`/today` 的 300 s 超时能穿透整条链路，靠的正是这个机制。

### checkpoint 的现状与限制

当前用 `InMemorySaver`，**进程重启即失效**。`thread_id` 与 `run_id` 对齐，为跨重启
恢复和多轮追问预留了接口，但要真正做到需要换 SQLite/Postgres checkpointer。

需要人工确认投递、修改简历或发送材料时，可以在副作用节点前用 `interrupt()` 暂停，
用户在飞书点击确认后再用相同 `thread_id` 恢复——这是 LangGraph 相对
`asyncio.gather` 的真实增量，也是投递 Agent 落地时会用到的那条路。目前这条路还没打通，
因为它依赖上面那个持久化 checkpointer。

### 四套状态，容易混在一起看

同一次运行里有四种「状态」，生命周期和存储位置都不同。混淆它们是读这套代码最容易走
偏的地方：

| 状态 | 存在哪 | 活多久 | 谁读它 |
| --- | --- | --- | --- |
| **图状态** `JobAgentGraphState` | 进程内存 + `InMemorySaver` checkpoint | 一次运行，进程重启即失效 | LangGraph 自己，节点之间 |
| **任务图** `TaskNode.status` | `data/runtime/task_graphs/<run_id>.json` | 永久 | `#graph` 页、小镇依赖连线 |
| **事件流** `ActivityEvent` | `data/runtime/activity.jsonl`（append-only） | 永久，不可变 | `#town` / `#replay`、审计 |
| **长期记忆** `AgentMemory` | `data/runtime/memories.jsonl` | 跨运行 | 角色执行前的检索 |

前两个是**可变的当前状态**，后两个是**不可变的历史**。具体说：

- checkpoint 记「这次图跑到哪个 Node」；
- 任务图记「哪个任务的依赖满足了、能开始了」；
- 事件流记「发生过什么」，永不删行；
- `MemoryStore` 记「这个角色跨运行可复用的经验」。

**任务图的状态机**（`tasks.py`）是用户在 `#graph` 页看到的那个：

```text
blocked ──依赖全部 completed──▶ ready ──开始执行──▶ running ──▶ completed
                                                          └──▶ error / timeout
```

`TaskGraphStore._recompute()` 在每次写入时重算：遍历所有节点，把「依赖全部
completed」的从 `blocked` 提成 `ready`，再按状态集合推导整图的
`queued / running / partial / completed / error`。节点带 `task_id`、`role_id`、
`phase`、`depends_on`、验收标准、模型、输出摘要、错误和 0–100% 进度。RPG 页面直接读
这张图，**不根据精灵状态猜依赖**。

存储用 fcntl 排他锁 + 临时文件原子替换，因为 API 进程、飞书 bot、日报进程共享同一个
持久卷。

**任务图只画运行真正会遵守的依赖。** Planner 能表达 `depends_on`，但 collaborative 的
执行顺序是按 `workflow_stage` 硬分三阶段的，所以 `build_run_task_graph` 用
`PHASE_ORDER` 过滤，只保留**跨阶段**的边。两个同阶段角色之间的 planner 边不会被画出来
——它们实际是并发跑的，画上去等于告诉用户 A 等了 B，而 A 根本没等。

事件流的读取是从文件**尾部反向按块读**（`ActivityStore.read`），取够 `limit` 条就停。
原因很实际：常驻巡检心跳占了行数的 66%，「读整个文件再截尾」的成本会随进程运行时长
无上限增长。

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
            +--> Job Analyst           要求拆解 + 知识补充
                           |
                 上游结果合并为带证据上下文
                           |
   +--> Material Builder -+
   +--> Interview Coach --+  第二阶段并行
                           |
                    Judge（按需触发）
                           |
                   最终回复 + Run Metrics
```

命令预设只选择所需角色。例如 `/job` 只执行 discovery/analysis 两个角色，`/apply` 先做
岗位分析，再把结果交给材料产出角色；`/team` 才运行四个工作角色。普通消息则由
Chief of Staff 的拆解决定，通常比任何预设都窄。这样能避免无关 Agent 消耗延迟和额度。

直接 `@` 某个角色或使用 `/ask <role_id>` 时仍可调用 Judge，但最终输出契约不同：
专家完整正文放在前面，Judge 只追加证据纠错，不允许覆盖或压缩专家回答，也不得混入
与当前问题无关的日报内容。消息合并时会自动闭合未成对的 Markdown 代码围栏。

角色配置中的 `tools` 现在是真的会执行的能力，不再只是元数据，而且可以按工具名单独授予：

| `tools` 写法 | 角色拿到什么 |
| --- | --- |
| `web_search` | 广义授权：Claude 服务端 `web_search_20260209` / `web_fetch_20260209`；网关不支持（显式 400，或收下工具却一次都不执行）时，同一次请求自动降级到本地 Tool Executor 的**全套**两个工具 |
| `list_tracked_jobs` | 只读岗位表。能拿到真实公司、岗位全名、官方链接和投递状态，但不能上网 |
| `fetch_url` | 只按域名白名单抓页面。用来核验别人给出的链接，不能自己发现新岗位 |
| `local_docs` | 不是可调用工具，而是让 `RoleContextProvider` 注入该角色的静态资料包 |

当前分配：

| 角色 | 工具 | 为什么 |
| --- | --- | --- |
| Job Scout | `web_search` + `local_docs` | 发现就是它的全部工作，需要开放搜索 |
| Job Analyst | `web_search` + `local_docs` | 读上游找到的页面，并核查技术栈事实 |
| Material Builder | `list_tracked_jobs` + `local_docs` | 按真实岗位关键词写 bullet，但没有理由去搜新岗位 |
| Interview Coach | `local_docs` | 题目来自 JD 和用户项目，都已在资料包里；给它搜索会招来假「真题」 |
| Evidence Judge | `fetch_url` + `local_docs` | **能真的打开上游引用的链接核验断言**，但拿不到 `list_tracked_jobs`——那会让它引入没人提过的岗位，正好违反它自己的契约 |

窄授权会连带改变提示词，这不是可选的润色：

- 只有 `fetch_url` 时，工具说明改为「抓取**上文已经出现过**的链接」。默认措辞写的是「只能抓 `list_tracked_jobs` 返回的链接」，而 Judge 没有那个工具——照抄会让它唯一的来源指向一个拿不到的东西。
- 「输出具体度要求」（必须逐条给出公司+岗位全名+岗位 ID+JD 原文摘录）**只在角色同时持有两个工具时**追加。持有全套意味着它的任务是产出岗位条目；窄授权意味着它在核验或读表，已经有自己的输出契约——给 Judge 拼上这段会和「输出审核结论、被删除的说法、行动清单」直接冲突，模型只能二选一。

两条联网路径都会把真实 URL 汇总进「联网来源（本次真实抓取）」，抓不到就必须写「页面未标注」或
「正文需人工核验」，不允许用记忆里的内容顶替。

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

- 岗位侦察、材料产出、面试、Chief of Staff 的三个环节：`claude-sonnet-5`；
- Job Analyst 与 Judge：`claude-opus-5`；
- 角色知识 prompt 约束为能力地图、定义/直觉、架构、技术选型、生产故障、评测、
  面试问题、30/60/120 分钟学习路径和 GitHub 作品证据。

模型 A/B 只决定当前角色分层，不应被解释为对所有任务的绝对排名。

## 飞书身份与逻辑角色

逻辑角色与飞书机器人身份分层：

```text
Feishu App: Chief of Staff -> Controller / 自动路由
Feishu App: Job Scout        -> job_scout
Feishu App: Job Analyst      -> job_analyst
Feishu App: Material Builder -> material_builder
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

1. **拆解先于派单**：Chief of Staff 一次模型调用把请求拆成最多 3 个子任务，
   决定派谁、谁依赖谁、要不要 Judge。显式指定角色（角色机器人、`/ask <role_id>`）
   跳过拆解；拆解失败退回关键词路由，两者都会在事件流里标明来源。
2. **按需 fan-out**：单一问题只跑一个专家；跨领域任务才并行多个专家。
   关键词路由做不到这点——`trigger_keywords` 天然重叠，"这个岗位的 JD" 会同时命中
   Job Scout 和 Job Analyst，每个角色各写一份全长回答。
3. **Judge 按需触发**：多个角色的产出需要交叉核对时必审；单角色回答由拆解决定，
   因为那种情况 Judge 只是追加一段证据补充（见 `merge_judged_output`）。
4. **上下文隔离**：每个 Agent 只收到自己的 system prompt、相关 JD/简历片段，
   不复制整段历史。
5. **并发上限**：用 semaphore 限制并发，避免 API 限流和尾延迟爆炸。
6. **超时与降级**：角色超时不阻塞整体；Judge 可基于已完成结果生成部分报告。
7. **模型分层**：普通工作角色使用低延迟 reliable 模型；岗位分析角色使用更强模型；
   Judge 使用独立 profile。只有这两个节点承担更强模型成本，不拖慢同阶段内由
   `asyncio.gather` 并发执行的其他角色（注意这是 asyncio 的并发，不是 LangGraph 的
   super-step——见「LangGraph 在这里不做并行」）。
8. **缓存**：按规范化 JD hash 缓存解析；相同简历/JD 组合复用证据抽取。
9. **结构化输出**：Pydantic/JSON Schema 降低重试和解析失败。
10. **可观测性**：每个 run 记录 wall latency、agent latency、token、错误、重试和
   model profile，不记录 API Key。
11. **可恢复执行**：LangGraph 模式用 checkpointer 保存阶段状态；后续把飞书会话
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
