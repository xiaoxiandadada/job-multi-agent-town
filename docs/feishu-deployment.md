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

事件配置：

1. 订阅方式选择“使用长连接接收事件”。
2. 先启动 `job-agent-feishu`，再点击“验证”，确认显示“连接成功”。
3. 添加应用身份事件 `im.message.receive_v1`（接收消息 v2.0）。
4. 创建并发布版本。首次验证建议只把应用开放给应用所有者，关闭外部群和外部
   用户单聊。

如果需要在同一个群里分别 `@岗位侦察员`、`@简历策略师` 等身份，每个名称需要
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
| `/knowledge <任务>` | 岗位知识补充员 | 2（含 Judge） |
| `/job <任务>` | 岗位侦察、JD 分析、岗位知识 | 4（含 Judge） |
| `/apply <任务>` | JD/知识 → 简历/作品 | 5（含 Judge） |
| `/interview <任务>` | JD/知识 → 面试 | 4（含 Judge） |
| `/team <任务>` | 六个工作角色分两阶段协作 | 7（含 Judge） |
| `/ask <role_id> <问题>` | 指定一个角色 | 2（含 Judge） |
| `/agent <role_id> <任务>` | 指定一个角色 | 2（含 Judge） |
| `/role-add <JSON>` | 0 | 0 |

普通消息按触发词自动路由；匹配到的工作角色并行执行后交给 Judge 汇总。以上调用数
按全部成功且启用 Judge 计算；某个角色超时或未匹配时以 Run Report 为准。
已绑定独立飞书身份的角色不需要 `/ask`：单聊直接提问，群聊使用
`@角色机器人 <问题>`。其普通消息会绕过自动路由，固定进入绑定 role。

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

LangGraph 对照运行：

```bash
uv sync
JOB_AGENT_ORCHESTRATOR=langgraph uv run job-agent-feishu
uv run python benchmarks/compare_harness_langgraph.py
```

2026-07-26 的飞书端到端验收使用“岗位准备 + 作品动作”跨领域任务，触发两个工作
角色和一个 Judge，共 3 次模型调用；飞书收到完整回复，wall latency 为
71.3 秒。该数值只证明链路可用，不代表稳定性能基线。

## 6. 发布前检查

```bash
uv run pytest -q
git check-ignore .env
git grep -n -E 'sk-[A-Za-z0-9_-]{12,}|LARK_APP_SECRET=.+' -- .
```

预期 `.env` 被忽略，仓库中只有占位凭证。若公开仓库，建议再启用 GitHub secret
scanning，并把网页/API 放在鉴权或内网入口之后。
