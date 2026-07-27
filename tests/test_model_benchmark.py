from job_agent_harness.benchmarking import score_model_output


def test_model_benchmark_score_is_deterministic():
    content = """
## 关键能力
工具调用
## 工程难点
失败恢复
## 作品证据
trace
## 面试问题
如何评测 Agent？
## 学习优先级
先做 golden set
## 待核验
具体公司要求
"""

    score = score_model_output(content)

    assert score["section_hits"] == 6
    assert score["missing_sections"] == []
    assert score["has_verification_marker"] is True
