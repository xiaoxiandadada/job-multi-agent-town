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
