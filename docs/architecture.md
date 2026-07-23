# Multi-Agent 求职 Harness：开源调研与技术设计

日期：2026-07-23

## 调研结论

项目应把飞书视为 Channel，而不是 Agent Runtime：

```text
飞书消息/卡片
  -> 飞书官方 SDK（WebSocket 或 webhook）
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
3. [LangGraph 多 Agent 指南](https://langchain-ai.github.io/langgraph/tutorials/multi_agent/multi-agent-collaboration/)
   指出并非每个任务都需要多 Agent；Router/Subagents 更适合多领域并行和上下文隔离。
4. [LangGraph workflow 指南](https://langchain-ai.github.io/langgraph/agents/tools/)
   的 orchestrator-worker 与 `Send` API 支持运行时动态创建并行 worker。
5. [飞书 OpenAPI MCP](https://github.com/larksuite/lark-openapi-mcp)
   可作为后续工具层，让 Agent 操作文档、消息和日历；它不是模型服务。
6. AutoGen 当前已进入维护模式，新项目不以它为核心；若以后需要企业级跨语言
   orchestration，可评估其继任者 Microsoft Agent Framework。

## 求职角色

| 角色 | 输入 | 输出 | 默认触发 |
| --- | --- | --- | --- |
| Job Scout | 方向、城市、届别 | 可核验岗位候选 | 岗位、秋招、校招 |
| JD Analyst | JD | 关键词、硬要求、缺口 | JD、职责、要求 |
| Resume Strategist | JD、简历证据 | 简历版本、bullet | 简历、bullet |
| Portfolio Coach | 目标岗位、项目 | MVP、证据、指标 | 作品、项目 |
| Interview Coach | JD、缺口 | 问题、追问、评分表 | 面试、题目 |
| Judge | 所有结构化结果 | 冲突、证据、最终建议 | 每次聚合 |

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
6. **便宜模型分层**：路由、结构化提取用低成本模型；综合判断和 Judge 用强模型。
7. **缓存**：按规范化 JD hash 缓存解析；相同简历/JD 组合复用证据抽取。
8. **结构化输出**：Pydantic/JSON Schema 降低重试和解析失败。
9. **可观测性**：每个 run 记录 wall latency、agent latency、token、错误、重试和
   model profile，不记录 API Key。

## Benchmark 设计

### 对照组

- B0：单一 Generalist Agent。
- B1：多 Agent 顺序执行。
- B2：多 Agent 并行执行。
- B3：动态路由 + 并行 + Judge。
- M1/M2：同一 harness 下切换不同模型或 API。

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

