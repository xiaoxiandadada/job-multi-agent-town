# 飞书部署与验收

本项目把飞书作为消息 Channel，Multi-Agent 编排和模型调用都运行在自己的
Python Runtime 中，不使用飞书 AI。

## 1. 飞书自建应用

在正确企业主体下创建企业自建应用并启用“机器人”能力。只申请以下应用身份权限：

| 权限 | Scope | 用途 |
| --- | --- | --- |
| 以应用身份发消息 | `im:message:send_as_bot` | 回复用户 |
| 读取用户发给机器人的单聊消息 | `im:message.p2p_msg:readonly` | 接收单聊 |
| 获取群组中用户 @ 机器人消息 | `im:message.group_at_msg:readonly` | 接收群聊提及 |

Interview Coach（`interview_coach`）身份如果要支持语音模拟面试，额外申请：

| 权限 | Scope | 用途 |
| --- | --- | --- |
| 识别语音文件 | `speech_to_text:speech` | 把候选人的语音回答转成文字 |
| 上传图片或文件 | `im:resource:upload` | 上传教练出题的 opus 语音（`im:resource` 亦可） |

这两项只有教练身份需要，而且缺哪一项的表现不一样：

- 缺 `speech_to_text:speech`：语音回答收不到转写，机器人回复缺哪个 scope，不会
  静默丢掉这一轮；
- 缺 `im:resource:upload`：文字问答完全正常，只有语音上传被拒
  （`code=99991672`），机器人降级为文字版并提示一次要开通的 scope。

教练的语音回复还需要本机安装一个 opus 编码器
（`brew install ffmpeg` 或 `brew install opus-tools`），否则同样降级为文字版并
提示一次安装命令。这两类降级各自只提示一次，不会每题重复。

事件配置：

1. 订阅方式选择“使用长连接接收事件”。
2. 先启动 `job-agent-feishu`，再点击“验证”，确认显示“连接成功”。
3. 添加应用身份事件 `im.message.receive_v1`（接收消息 v2.0）。
4. 创建并发布版本。首次验证建议只把应用开放给应用所有者，关闭外部群和外部
   用户单聊。

如果需要在同一个群里分别 `@Job Scout`、`@Resume Strategist` 等身份，每个名称需要
一个独立飞书自建应用，并重复以上最小权限和事件配置。它们不需要分别部署后端：
同一个 `job-agent-feishu` 进程可以托管全部身份。配置方式见
[`feishu-multi-bot.md`](feishu-multi-bot.md)。

## 2. 本地运行

```bash
uv sync --extra dev
cp .env.example .env
# 在 .env 中写入模型 API 和飞书自建应用凭证
uv run job-agent-feishu
```

飞书结果使用 Post/Markdown 富文本，不再把 `##`、列表和链接作为普通文本显示。
直接 `@` 一个角色时，专家完整正文先展示，Judge 只追加证据审核；消息合并层会自动
闭合未成对的代码围栏，避免长回答中的后续标题进入代码块。
`/daily` 会从 `JOB_AGENT_PREPARE_DIR` 读取当天日报，删除本地绝对路径后直接展示
岗位、具体学习内容、创意、作品进展和三件优先事项。

另开一个终端启动网页/API：

```bash
uv run job-agent-api
```

打开 `http://127.0.0.1:8000`，可一键添加角色并运行
single、sequential、parallel、collaborative 四种模式。

## 3. 容器部署

```bash
docker compose up --build -d bot
docker compose logs -f bot
```

如需同时启动本地 API/UI：

```bash
docker compose --profile api up --build -d
```

生产环境应在平台的 Secret/Environment Variables 中注入 `.env.example` 所列
变量，不要上传 `.env`。`/data` 用于保存动态角色注册表，部署平台应为它挂载持久卷。
`JOB_AGENT_FEISHU_MAX_CHARS` 控制飞书最终消息的长度保护；角色 timeout 覆盖等待
并发槽位和模型请求的完整生命周期，避免多条团队命令同时进入后无限排队。

这里建议保留两个容器服务，而不是为七个角色启动七套后端：

- `bot`：一个 supervisor 托管总控和七个角色的八条 WebSocket；
- `api`：LangGraph、角色/模型配置、任务图和 RPG 页面；
- 共享 `/data`：角色注册表、ActivityEvent、Memory Stream 和任务图；
- `.env`：只作为容器 Secret 输入，不进入镜像和 Git。

Docker 不是 LangGraph 的一部分，也不会加速模型；它解决环境一致、常驻重启、密钥
注入、持久卷和从本地迁移到云服务器的问题。

长连接需要常驻进程，适合云服务器或支持常驻容器的 PaaS。GitHub Pages 只能展示
静态说明和演示，不能保持飞书 WebSocket；GitHub 仓库可以保存源码、测试、
benchmark 和演示 GIF，再由容器平台拉取仓库部署。

本机临时演示若必须合盖运行，macOS 需要电源并建议连接外接显示器。没有外接显示器
时可显式关闭合盖睡眠，但这会增加发热、电池与物理安全风险：

```bash
./scripts/clamshell-work.sh enable
# 演示完成后恢复
./scripts/clamshell-work.sh disable
```

脚本会调用 `sudo pmset`，必须由电脑所有者在终端亲自输入 Mac 密码；项目不会读取或
保存该密码。生产 Bot 更适合放在常驻容器，而不是长期依赖笔记本合盖运行。

## 4. 一键添加角色

网页表单会调用 `POST /api/roles`，新角色写入版本化注册表后立即参与下一次路由，
无需重启服务。飞书内也可以发送：

```text
/role-add {"role_id":"bioinformatics_coach","display_name":"生信算法教练","goal":"把生信岗位要求映射到可验证的项目证据","system_prompt":"只依据用户提供的岗位和项目证据输出，不虚构指标。","trigger_keywords":["生信","GWAS","单细胞"]}
```

用 `/help` 查看帮助，主要命令如下：

| 命令 | 工作角色 | 模型调用数 |
| --- | ---: | ---: |
| `/help` | 0 | 0 |
| `/roles` | 0 | 0 |
| `/group-create` | 创建/绑定私有 Agent 小镇群 | 0 |
| `/daily [YYYY-MM-DD] [full]` | 读取并推送日报 | 0 |
| `/knowledge <任务>` | Knowledge Curator | 2（含 Judge） |
| `/job <任务>` | 岗位侦察、JD 分析、岗位知识 | 4（含 Judge） |
| `/apply <任务>` | JD/知识 → 简历/作品 | 5（含 Judge） |
| `/interview <任务>` | JD/知识 → 面试 | 4（含 Judge） |
| `/team <任务>` | 六个工作角色按依赖分阶段协作 | 7（含 Judge） |
| `/ask <role_id> <问题>` | 指定一个角色 | 2（含 Judge） |
| `/agent <role_id> <任务>` | 指定一个角色 | 2（含 Judge） |
| `/role-add <JSON>` | 0 | 0 |

普通消息按触发词自动路由；匹配到的工作角色并行执行后交给 Judge 汇总。以上调用数
按全部成功且启用 Judge 计算；某个角色超时或未匹配时以 Run Report 为准。
已绑定独立飞书身份的角色不需要 `/ask`：单聊直接提问，群聊使用
`@角色机器人 <问题>`。其普通消息会绕过自动路由，固定进入绑定 role；角色正文
保留，Judge 以附录形式核验。

### 发一个 `/` 展开命令面板

群里或单聊里只发一个 `/`（群里仍需 `@Chief of Staff /`），机器人会把可用命令按
「求职流程 / 工具命令」两组展开，并标出每个命令会唤起几个角色——那是用户在按下
回车前唯一能看到的耗时信号。

面板由 `commands.command_palette_markdown()` 从 `command_catalog()` 现场生成，
和网页输入框读的是同一个真源。**上面那张手写表格会随代码漂移，`/` 的输出不会，
两处对不上时以 `/` 为准。**

### 让飞书原生的 `/` 面板也列出这些命令

上一节说的是机器人自己回一张表。飞书输入框里输 `/` 唤起的那个原生面板是另一回事：
它只列开放平台里配置的**快捷指令**，代码创建不了，得在
[开放平台](https://open.feishu.cn/app) → 选择应用 → 「机器人」→「快捷指令」里逐条添加。

指令内容必须和命令字面量完全一致，否则点击后发出的文本会被解析器拒绝：

| 指令名 | 指令内容 | 是否需要补参数 |
| --- | --- | --- |
| 今日流程 | `/today` | 否 |
| 匹配度 | `/match ` | 是，接岗位或 JD |
| 岗位侦察 | `/job ` | 是 |
| 投递材料 | `/apply ` | 是 |
| 面试准备 | `/interview ` | 是 |
| 知识拆解 | `/knowledge ` | 是 |
| 全员协作 | `/team ` | 是 |
| 问一个角色 | `/ask job_analyst ` | 是，接问题 |
| 模拟面试 | `/mock start ` | 是，接主题 |
| 查看角色 | `/roles` | 否 |
| 今日日报 | `/daily` | 否 |

需要补参数的那几条，指令内容末尾留一个空格，点击后光标就停在参数位置。

`/mock` 与 `/match` 是本文上面表格漏掉的两条，配置时不要照那张表填。

`/group-create` 只发送给总控机器人。除上面的三项消息权限外，总控还需要
`im:chat:create` 与 `im:chat.members:write_only`；七个角色应用不需要这两项。
命令把发起人设为群主，加入总控和已配置角色，并以本地状态文件保证幂等。只有全部
成员加入成功后才会保存群状态并把日报推送目标切到该群。

## 5. 性能与模型对比

系统记录每次运行的 wall latency、各 Agent latency、模型调用数、token 和并行
加速估计。建议用同一批 golden tasks 比较：

- 单一 Generalist；
- 多 Agent 顺序执行；
- 多 Agent 并行执行；
- 动态路由 + 并行 + Judge；
- 在同一 Harness 下切换 worker/judge 模型。

模型默认分为 `default`、`knowledge`、`judge` 三个 profile。岗位知识角色的
reasoning 尾延迟若明显偏高，可单独设置 `JOB_AGENT_KNOWLEDGE_MODEL`；未设置时
自动复用 `JOB_AGENT_JUDGE_MODEL`，不会影响其他工作角色的模型选择。

还可对任意角色做更高优先级的直接覆盖：

```dotenv
JOB_AGENT_ROLE_MODEL_JOB_KNOWLEDGE_CURATOR=claude-opus-5
```

解析顺序为 `RoleSpec.model > JOB_AGENT_ROLE_MODEL_<ROLE_ID> > model_profile`。
网页角色卡和 `PATCH /api/roles/{role_id}` 可以修改 `RoleSpec.model`，无需更改
其他角色。`GET /api/models` 只返回模型名称与解析结果，不返回 API Key。

`benchmarks/run_mock_benchmark.py` 可先验证并行收益，再用真实模型跑小规模样本，
避免把网络波动误当作架构收益。

自动化执行完日报后运行：

```bash
uv run job-agent-push-daily
```

Bot 会使用最近一次收到消息的 `chat_id`，以机器人身份把日报富文本推回该会话。
也可以通过 `JOB_AGENT_FEISHU_CHAT_ID` 显式指定目标。完整日报使用：

```bash
uv run job-agent-push-daily --full
```

默认分发顺序是：

1. 总控发送综合日报；
2. 总控发送新增岗位的角色任务拆解；
3. 七个角色分别发送自己的日报内容；
4. 任务图记录总控根节点、角色节点与 Judge 终审依赖。

因此完整配置下通常是 9 条飞书消息、8 个任务节点。若只要角色消息，使用
`--roles-only`。

### 每天自动推

上面那条命令由定时器接管，不需要每天手敲。`job-agent-api` 与 `job-agent-watch`
启动时都会尝试拉起它，由 `data/runtime/daily_push.lock` 决定谁真的发：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JOB_AGENT_DAILY_PUSH` | `1` | 关掉就退回手动 |
| `JOB_AGENT_DAILY_PUSH_AT` | `09:30` | 只接受 `HH:MM` |
| `JOB_AGENT_DAILY_PUSH_TZ` | `Asia/Shanghai` | |
| `JOB_AGENT_DAILY_PUSH_MAX_DELAY_HOURS` | `6` | 超窗判定过期，当天不推 |
| `JOB_AGENT_DAILY_PUSH_FULL` | `0` | 默认摘要，`1` 发完整日报 |
| `JOB_AGENT_DAILY_PUSH_POLL_SECONDS` | `120` | |
| `JOB_AGENT_DAILY_PUSH_RETRY_SECONDS` | `600` | 失败退避，窗口内继续重试 |

定时器是轮询而不是睡到某一刻：合盖睡过 09:30 的机器醒来后仍会补推。已推日期写在
`data/runtime/daily_push_state.json`，重启不会重复推。运行状态见
`GET /api/always-on` 响应里的 `daily_push` 字段（`next_push_at`、
`last_pushed_date`、`consecutive_failures`、`last_error`）。

当天没有人工日报时，定时路径会让七个角色按章节各写一段兜底日报，落到
`data/runtime/daily/<YYYY-MM-DD>.md`，不写进 `prepare/`；`/daily` 命令也会读这个缓存。
人工日报存在时永远优先。手动等价命令：

```bash
uv run job-agent-write-daily --date 2026-08-05
uv run job-agent-push-daily --generate
```

### 角色资料包放在哪

所有角色的 `local_docs` 都从**仓库外**的一个目录读取。`role_context.default_prepare_dir`
解析的是 `role_context.py` 往上四级再加 `prepare`，也就是把仓库整个放进
`.../interview/projects/ai-job-agent` 时，资料包在 `.../interview/prepare`——
不是仓库根下的 `prepare/`。这个位置看着是刻意的：简历和岗位表是个人材料，不该进
公开仓库。用 `JOB_AGENT_PREPARE_DIR` 可以显式指定。

目录不存在或为空时不会报错，只是每个角色的资料上下文都是 0 字符，表现为：Job Scout
说岗位表里什么都没有、Match Scorer 打不出分（于是投递提醒永远不触发）、Material
Builder 无从改简历。想确认当前读到了什么：

```bash
uv run python -c "
from job_agent_harness.role_context import build_role_context, default_prepare_dir
from job_agent_harness.runtime import build_registry
print(default_prepare_dir())
reg = build_registry()
for rid in ('job_scout', 'match_scorer', 'job_analyst'):
    print(rid, len(build_role_context(reg.get(rid)) or ''))
"
```

需要的 10 个文件见 `role_context.ROLE_CONTEXT_FILES`，按角色分组。

### 巡检发现岗位后自动接着跑

Job Scout 的巡检原本只写事件流：搜完就停，凌晨发现的强匹配要等你下次打开飞书才看见。
现在一次成功巡检会把证据交给下游继续跑，并在分数达标时推提醒。

链条停在打分，不继续改简历：那之后是「要不要投」的人工决策。想要材料和面试准备，
用 `/apply`、`/interview` 显式触发。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JOB_AGENT_PATROL_FOLLOWUP` | `1` | 关掉就退回「只巡检、只记事件流」 |
| `JOB_AGENT_PATROL_FOLLOWUP_ROLES` | `job_analyst,match_scorer` | 按顺序 collaborative 执行 |

两个刹车。一是**没带回官方链接就不触发**：Scout 的契约是「没有官方链接的岗位不要
输出」，所以链接是最省事又可靠的「真有岗位」信号；少了这道判断，每次「找不到」都要
白花两个角色去分析这三个字。日志里会写明「巡检没有带回官方链接，未触发下游分析」。

二是**同一个岗位只提醒一次**：巡检 15 分钟一轮，而岗位表变化慢得多，否则同一个 82%
会一小时推四遍，最后训练出的结果是你忽略这类通知。已推记录在
`data/runtime/match_alerts.json`，按「公司 + 岗位名」去重而不是按 URL——同一个岗位
换条链接找到还是同一个岗位。

下游失败只记日志、不影响巡检本身：巡检已经成功且产出已入库，让一个纯增益的步骤把
巡检标记成失败，会触发指数退避、直接把常驻巡检停掉。

### 匹配度达标就提醒投递

Match Scorer 本来只把报告放回你问它的那个会话里，所以常驻巡检时段跑出来的高分要等
你下次打开飞书才看见。达到阈值时它现在会自己再发一条短提醒：分数、公司、岗位、
官方链接、最强项与最弱项，以及投递前顺手能做的动作。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JOB_AGENT_MATCH_ALERT` | `1` | 默认开；关掉只影响提醒，报告照发 |
| `JOB_AGENT_MATCH_ALERT_THRESHOLD` | `70` | 达到或超过就推，含等号；填错退回 70 |

三个条件必须同时成立才推，各自挡掉一种不该打扰你的情况：分数过线、`blockers`
为空、`verdict` 不是「不建议」。第二条不与第一条重复——`MatchReport.recomputed()`
虽然会把有硬阻断的报告封顶到 40，但那是评分算法的实现细节，靠它兜底意味着改一次
权重就可能开始推你根本不满足门槛的岗位。

提醒发送独立于报告本身：报告因为过长而回包失败时，这条短提醒仍会送到——那正是它
最该出现的时候。提醒发送失败只记日志，不影响运行。

LangGraph 对照运行：

```bash
uv sync
JOB_AGENT_ORCHESTRATOR=langgraph uv run job-agent-feishu
uv run python benchmarks/compare_harness_langgraph.py
```

2026-07-26 的飞书端到端验收使用“岗位准备 + 作品动作”跨领域任务，触发两个工作
角色和一个 Judge，共 3 次模型调用；飞书收到完整回复，wall latency 为
71.3 秒。该数值只证明链路可用，不代表稳定性能基线。

2026-07-30 的完整验收见
[`acceptance-2026-07-30.md`](acceptance-2026-07-30.md)：日报向 Agent 小镇群发送
9 条消息，八个任务节点 100%；真实 `@Knowledge Curator` 使用 397B 专家模型并由 32B
Judge 追加审核。

## 6. 发布前检查

```bash
uv run pytest -q
git check-ignore .env
git grep -n -E 'sk-[A-Za-z0-9_-]{12,}|LARK_APP_SECRET=.+' -- .
```

预期 `.env` 被忽略，仓库中只有占位凭证。若公开仓库，建议再启用 GitHub secret
scanning，并把网页/API 放在鉴权或内网入口之后。
