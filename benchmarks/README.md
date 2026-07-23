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

