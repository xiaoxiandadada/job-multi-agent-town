# 2026-07-30：任务图、分角色模型与飞书日报验收

## 结论

本次验收覆盖当前版本的四条主线：

1. 总控先发送每日求职总报，再把新增岗位拆给七个角色；
2. 每个角色可独立解析模型，岗位知识补充员使用更强模型；
3. RPG 页面展示真实任务、依赖、状态、进度和 LangGraph 阶段；
4. 飞书中可以直接 `@` 角色，角色正文不会被 Judge 替换。

凭证和模型 API Key 只存在本地 `.env`/部署环境变量，没有写入本文件或 Git。

## LangGraph 七角色协作

本地验收 run：`c91ddebd-6142-41e7-8f0b-873a3c31c269`。

- 图：`route → discovery → analysis → action → judge`；
- 岗位侦察员先完成；
- JD 分析师与岗位知识补充员在侦察证据完成后并行；
- 简历、作品、面试三个角色等待前述依赖后并行；
- Judge 等待全部六个工作角色；
- 最终状态：7/7 完成，任务图 100%；
- wall latency：169.9 秒；
- 并行加速估算：1.54×；
- 岗位知识补充员模型：`Qwen/Qwen3.5-397B-A17B`，该次角色耗时 14.5 秒；
- 其他角色和 Judge：`Qwen/Qwen2.5-32B-Instruct`。

RPG 页面在运行中实际显示过 `running → completed`、`blocked → running` 与
`DEPENDS ← <task_id>`，完成后仍可从历史 run 回放。

## 每日日报和任务分发

Docker/飞书验收 run：
`daily-b431c9c8-d742-49ce-b216-54605bc46a48`。

- 总控发送综合日报与拆解说明；
- 岗位侦察、JD、知识、简历、作品、面试、Judge 分别由独立机器人推送；
- 命令返回：9 条消息，身份为 controller + 7 个角色；
- 任务图：总控 1 个根节点、6 个角色分工节点、Judge 终审节点；
- 最终状态：8/8 节点完成，100%；
- 飞书网页端可见总控日报、七个角色标题和独立机器人发送身份；
- 群头部显示 1 个用户与 8 个机器人身份。

## 直接 @ 角色

飞书实测 run：`4f87def0-b990-412c-b4b7-f5c4368c9a88`。

问题直接 `@岗位知识补充员`，要求解释 Agent Evaluation 的指标公式、六类生产
故障、Golden Set 样例结构和 GitHub MVP。

- 工作角色：`job_knowledge_curator`；
- 专家模型：`Qwen/Qwen3.5-397B-A17B`；
- 审核模型：`Qwen/Qwen2.5-32B-Instruct`；
- 两个任务节点均完成，任务图 100%；
- 飞书收到完整专业正文，包含指标、故障表和 JSON 样例；
- Judge 以“证据审核员补充”追加，不再替换专家正文；
- 该次视觉验收暴露了模型漏写 Markdown 代码围栏的问题；随后在合并层加入自动闭合，
  由 `test_close_unbalanced_code_fence` 覆盖，避免最终版本的后续标题进入代码块。

## Docker 运行证据

`docker compose --profile api up -d --no-build` 运行两个服务：

- `bot`：总控 + 七个角色的八条飞书 WebSocket；
- `api`：FastAPI、RPG 页面、任务图和角色/模型配置 API。

容器内健康检查返回 `LangGraphOrchestrator`，运行阶段为
`route/discovery/analysis/action/judge`；八个飞书身份均产生
`Feishu identity connected` 日志。

## 自动化验收

```text
uv run pytest -q
node --check <extracted web script>
git diff --check
docker compose ps
```

测试同时覆盖任务依赖推进、回放时不泄露未来状态、角色模型覆盖、直接点名角色时保留
专家正文、Judge 追加审核和未闭合 Markdown 围栏修复。
