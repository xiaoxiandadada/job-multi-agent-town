# Job Agent Studio

一个面向 2027 届秋招的、可审计的 Multi-Agent 求职系统。

飞书只负责消息入口和交互展示；模型推理由用户自己的 OpenAI-compatible API
完成。项目不会调用飞书 AI。

## 核心能力

- 角色注册表：通过网页/API 一键添加角色，无需改代码或重启服务。
- 动态路由：只选择与当前任务相关的角色，避免所有 Agent 每次都运行。
- 并行执行：岗位、简历、作品和面试等独立任务并发完成。
- 审核闭环：Judge 汇总结果并保留角色输出、耗时和 token 证据。
- 模型可替换：同一套角色可切换不同模型，支持单 Agent、多 Agent和模型 A/B。
- 飞书接入：使用飞书官方 Python SDK 的长连接模式，不要求飞书 AI。

## 架构

```text
Feishu Bot / Web UI
        |
Channel Adapter
        |
Deterministic Router
        |
Role Registry -> Parallel Workers -> Judge -> Run Report
        |                |             |
   role config      user model API   eval metrics
```

详细设计与开源调研见
[`docs/architecture.md`](docs/architecture.md)。

## 本地启动

```bash
uv sync --extra dev
cp .env.example .env
uv run job-agent-api
```

打开 `http://127.0.0.1:8000`。未配置模型 API 时，API 会明确报配置缺失；
单元测试使用 Mock Model，不消耗模型额度。

飞书 Bot 使用长连接启动：

```bash
export LARK_APP_ID=cli_xxx
export LARK_APP_SECRET=...
export JOB_AGENT_API_BASE=https://your-api.example/v1
export JOB_AGENT_API_KEY=...
export JOB_AGENT_MODEL=...
uv run job-agent-feishu
```

## GitHub 展示与线上运行

GitHub 保存源码、架构图、benchmark 数据和演示 GIF。GitHub Pages 只能托管静态
页面，不能持续运行 Agent 后端；线上 Bot 需要一个常驻 Python 服务，或改用公开
HTTPS webhook 部署到支持后端的服务。

建议交付结构：

1. GitHub：源码、角色模板、benchmark、CI 和演示材料。
2. 飞书自建应用：机器人、消息事件和交互卡片。
3. Runtime：Docker/云服务器/Render/Railway/Fly.io 或妙搭全栈托管。
4. 模型：用户提供的 API，通过环境变量注入。

