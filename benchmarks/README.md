# Benchmark Protocol

真实模型接入后，固定同一批 golden tasks，分别运行：

```text
B0 single
B1 sequential multi-agent
B2 parallel multi-agent
B3 parallel multi-agent + judge
```

每种模式至少重复 3 次，报告：

- task success / structured-output pass rate
- p50 / p95 wall latency
- input/output tokens and model calls
- estimated API cost
- human or judge preference win rate
- parallel speedup and failure-isolation rate

`run_mock_benchmark.py` 只验证编排开销和并行路径，不代表真实模型质量。

框架开销对照：

```bash
uv run python benchmarks/compare_harness_langgraph.py
```

同一提示词、输出上限和评分规则下的真实模型 A/B：

```bash
uv run python benchmarks/compare_models.py \
  --models qwen3.5-flash Qwen/Qwen2.5-32B-Instruct \
  doubao-seed-1-6-flash-250715 \
  --runs 3
```

脚本只输出模型名、成功率、延迟、token 和固定章节命中，不输出 API Key 或完整模型
回复。正式报告至少运行 3 次并报告中位数；若要判断内容质量，还需对盲测样本做人工
或独立 Judge 评分，不能把章节命中当成最终质量。

## 记忆检索

上面几个脚本量的都是 harness：跑得多快、并行了几个、Judge 改没改答案。检索没有被量
过，而检索出错是唯一不可见的失败——角色拿到 4 条无关记忆，照样写出一份自信的回答，
表现出来是「昨天核验过的百度岗位它忘了」，看起来像模型问题，其实不是。

```bash
uv run python benchmarks/eval_memory_retrieval.py            # 读本机 data/runtime
uv run python benchmarks/eval_memory_retrieval.py --k 6 --json
uv run python benchmarks/eval_memory_retrieval.py --synthetic  # 无本地数据时
```

golden set 不手写：语料里的岗位 ID（`J100679`）是最具体的标识符，所以「这条记忆是否
回答了关于该岗位的问题」是一个可判定的谓词，而不是主观判断。`discover_cases` 从出现
过两次以上的岗位 ID 自动构造案例——出现一次的跳过，否则 recall 是在单个文档上抛硬币。
这比 LoCoMo 那种人工标注弱，选它的理由要说清楚：替代方案是完全不量，而标识符匹配不会
像 Judge 模型那样自我恭维。

指标里 `boilerplate@k` 不是标准 IR 指标，它在这里是因为每轮都写一条「本轮计划：…」的
存储可以在 recall 上拿到不错的分数，同时把四条同一句话交给模型。`recall@k` 一并报告
k 决定的上限：一个岗位有 40 条记忆写着答案时，4 个槽位装不下 40 条。

`--synthetic` 的语料按真实语料的形状生成而不是按讨好检索器的形状：少量长证据 +
大量模板计划和近重复反思，因为 40% 记账占比正是检索器必须看穿的东西。
