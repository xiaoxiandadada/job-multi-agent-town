# 飞书多机器人身份部署

## 目标

在同一个群里提供可分别提及的机器人：

| 飞书应用名称 | 绑定 role_id | 作用 |
| --- | --- | --- |
| Chief of Staff | 无（总控） | 自动路由、团队命令、日报 |
| Job Scout | `job_scout` | 岗位真实性、届别、截止日期和来源 |
| JD Analyst | `jd_analyst` | JD 关键词、硬要求和缺口 |
| Knowledge Curator | `job_knowledge_curator` | 技术栈、评测知识和学习路线 |
| Resume Strategist | `resume_strategist` | 简历版本和 bullet |
| Portfolio Coach | `portfolio_coach` | MVP、作品证据和指标 |
| Interview Coach | `interview_coach` | 题目、追问和评分 |
| Evidence Judge | `judge` | 可选的直接证据审查入口 |

飞书应用名称决定群聊中显示和可搜索的机器人名称。Role Registry 的
`display_name` 应与应用名称保持一致。

## 为什么需要多个应用

飞书中的 bot identity 隶属于应用凭证。一个逻辑 role 可以在同一后端运行，但要在
客户端里出现多个可分别添加、搜索和 `@` 的机器人，就需要为这些身份分别创建企业
自建应用、启用机器人能力并发布。

这不会复制 Multi-Agent Runtime：所有机器人长连接仍由一个 Python 进程托管，
共享 Role Registry、模型 API、并发上限、LangGraph/asyncio 编排器和 Judge。

```text
@Job Scout --------\
@JD Analyst ----------\
@Resume Strategist ----------> 同一个 Feishu Channel 进程
@Portfolio Coach ------------/          |
@Interview Coach -----------/       Role Binding
                                |
                     Multi-Agent Orchestrator
                                |
                         用户的模型 API
```

## 每个角色应用的飞书后台配置

每个应用都使用最小且相同的消息配置：

1. 在正确企业主体中创建企业自建应用，应用名称使用上表中文名。
2. 启用“机器人”能力。
3. 开通应用身份权限：
   - `im:message:send_as_bot`
   - `im:message.p2p_msg:readonly`
   - `im:message.group_at_msg:readonly`
4. 事件订阅选择“使用长连接接收事件”。
5. 添加 `im.message.receive_v1`。
6. 创建并发布版本，把应用加入可用范围。
7. 将 App ID 和 App Secret 写入本地或部署平台的环境变量，不提交 Git。

## 后端配置

```dotenv
JOB_AGENT_FEISHU_ROLE_BOTS=job_scout,jd_analyst,job_knowledge_curator,resume_strategist,portfolio_coach,interview_coach,judge

LARK_ROLE_JOB_SCOUT_APP_ID=cli_xxx
LARK_ROLE_JOB_SCOUT_APP_SECRET=...
LARK_ROLE_JD_ANALYST_APP_ID=cli_xxx
LARK_ROLE_JD_ANALYST_APP_SECRET=...
LARK_ROLE_JOB_KNOWLEDGE_CURATOR_APP_ID=cli_xxx
LARK_ROLE_JOB_KNOWLEDGE_CURATOR_APP_SECRET=...
LARK_ROLE_RESUME_STRATEGIST_APP_ID=cli_xxx
LARK_ROLE_RESUME_STRATEGIST_APP_SECRET=...
LARK_ROLE_PORTFOLIO_COACH_APP_ID=cli_xxx
LARK_ROLE_PORTFOLIO_COACH_APP_SECRET=...
LARK_ROLE_INTERVIEW_COACH_APP_ID=cli_xxx
LARK_ROLE_INTERVIEW_COACH_APP_SECRET=...
LARK_ROLE_JUDGE_APP_ID=cli_xxx
LARK_ROLE_JUDGE_APP_SECRET=...
```

启动命令不变：

```bash
uv run job-agent-feishu
```

启动器会验证 role 是否存在、凭证是否完整、App ID 是否重复，然后为总控和每个角色
建立独立长连接。任一角色机器人收到普通问题后，会绕过自动路由，直接调用绑定角色；
除 `judge` 外，回答仍自动交给 Judge 复核。

## 新增可 @ 角色

逻辑角色仍可通过网页或 `/role-add` 一键加入 Role Registry。要让新角色同时拥有独立
飞书身份，还需要完成一次应用接入：

1. 创建与 `display_name` 同名的飞书应用。
2. 按上面的最小权限发布应用。
3. 把新 `role_id` 加入 `JOB_AGENT_FEISHU_ROLE_BOTS`。
4. 写入由 role_id 自动生成的 App ID/App Secret 环境变量并重启。

例如 `bioinformatics_coach` 对应：

```dotenv
LARK_ROLE_BIOINFORMATICS_COACH_APP_ID=cli_xxx
LARK_ROLE_BIOINFORMATICS_COACH_APP_SECRET=...
```

应用创建和 Secret 获取属于飞书开发者后台操作，不能仅靠 Role Registry 自动完成。
