# 性能验证记录

日期：2026-07-27

## 编排结构

MockModel 每个调用固定等待约 100ms：

| 模式 | 角色数 | wall latency | 并行加速估算 |
| --- | ---: | ---: | ---: |
| single | 1 | 101.44ms | 1.00× |
| sequential | 3 | 305.89ms | 1.00× |
| parallel | 3 | 102.16ms | 2.98× |
| collaborative | 3 | 203.35ms | 1.49× |

同一 5-worker + Judge、每次 Mock 调用约 50ms：

| 编排器 | wall latency | 调用数 |
| --- | ---: | ---: |
| asyncio Harness | 154.83ms | 6 |
| LangGraph adapter | 170.50ms | 6 |

本次 LangGraph 的本地状态图额外开销约 15.67ms。它换来的能力是显式状态、条件边、
checkpoint、未来的恢复/interrupt 和更清晰的观测；模型网络延迟达到秒级时，该开销
通常不是主要瓶颈。

## 真实七角色运行

运行 `2a111391` 使用 context → action → judge：

- 7 次模型调用，6 个成功，`job_scout` 在原 45 秒上限超时；
- wall latency 111.18 秒，角色耗时合计 215.90 秒；
- 并行加速估算 1.94×；
- `job_scout` 的单角色上限随后提高到 75 秒，避免把路由服务的尾延迟误判为失败。

这个结果证明失败隔离有效：一个角色超时后，其他 5 个工作角色与 Judge 仍输出可用
报告。它不是稳定基线；正式报告应对相同任务至少重复 3 次并给出 p50/p95。

## 模型 A/B smoke test

三模型使用同一 system prompt、同一任务、320 max tokens 与六章节命中规则。完整
机器可读结果见
[`../benchmarks/results/2026-07-27-model-ab.json`](../benchmarks/results/2026-07-27-model-ab.json)。

| 模型 | 成功 | 延迟 | 章节命中 | 输出 token |
| --- | ---: | ---: | ---: | ---: |
| qwen3.5-flash | 1/1 | 47.61s | 4/6 | 4535* |
| Qwen/Qwen2.5-32B-Instruct | 1/1 | 14.25s | 5/6 | 320 |
| doubao-seed-1-6-flash-250715 | 0/1 | 0.18s | — | — |

\* 路由服务的 usage 可能包含 reasoning token，因此会超过 `max_tokens`，需要把
“可见输出长度”和“计费用量”分开监控。

一次样本只能用于连通性和初步选型。当前更稳妥的默认分层是：

- worker：继续使用 qwen3.5-flash，但监控尾延迟；
- reliable/knowledge/judge：Qwen2.5-32B-Instruct；
- 豆包模型：先核对中转服务对该模型的权限或别名，再进入正式 A/B。

## 证据交接复测

JD 与简历角色使用 qwen3.5-flash 时曾同时触发 45 秒超时，未生成可用 handoff。
把 `job_scout`、`jd_analyst`、`resume_strategist` 切换到 `reliable` profile 后，
run `405a5e40` 完成 3/3 调用：

- JD 分析师：19.69 秒；
- 简历策略师：23.53 秒；
- Judge：17.00 秒；
- 总耗时：60.24 秒；
- `jd_analyst → resume_strategist` 的 `handoff_created` 已写入双方记忆流。

### RPG 认知层实跑补充（2026-07-27）

认知层上线后的首次 `portfolio_coach` 单角色任务仍走
`default/qwen3.5-flash`，在 60 秒上限触发 timeout；失败被完整写入该角色的
observation memory，没有伪装成成功结果。结合前述 A/B 中 Qwen2.5-32B 的更低延迟
和更高结构完整度，`portfolio_coach` 与 `interview_coach` 的生产配置已改为
`reliable` profile。`default` profile 继续保留给低风险探索任务和对照实验。

该分层来自同一 API 路由下的实测，不代表模型在所有任务上的绝对排序。
