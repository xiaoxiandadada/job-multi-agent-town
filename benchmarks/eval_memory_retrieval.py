"""Baseline and regression numbers for role memory retrieval.

Run against the live store to see what the roles are actually getting:

```bash
uv run python benchmarks/eval_memory_retrieval.py
uv run python benchmarks/eval_memory_retrieval.py --k 6 --json
```

Run against a generated corpus when there is no local data — same code path, so a
reviewer with a fresh clone gets a comparable number:

```bash
uv run python benchmarks/eval_memory_retrieval.py --synthetic
```

The synthetic corpus is built to the shape of the real one rather than to flatter
the retriever: a handful of long evidence memories carrying job ids, and a much
larger pile of templated plans and near-identical reflections, because that ratio
(40% bookkeeping in the real store) is exactly what a retriever has to see past.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_agent_harness.cognition import MemoryStore  # noqa: E402
from job_agent_harness.memory_eval import (  # noqa: E402
    RetrievalCase,
    discover_cases,
    evaluate_retrieval,
)
from job_agent_harness.runtime import runtime_data_dir  # noqa: E402


SYNTHETIC_JOBS = [
    ("J100679", "百度", "大模型研发工程师", "talent.baidu.com"),
    ("J99974", "百度", "机器学习工程师", "talent.baidu.com"),
    ("J102506", "百度", "数据挖掘工程师", "talent.baidu.com"),
    ("J101017", "拼多多", "算法工程师", "careers.pddglobalhr.com"),
]


def build_synthetic_store(path: Path) -> MemoryStore:
    store = MemoryStore(path)
    for index, (job_id, company, title, host) in enumerate(SYNTHETIC_JOBS):
        # Long, information-dense, and the only thing that can answer a query —
        # the exact profile the length-normalised similarity used to punish.
        store.append(
            role_id="job_scout",
            run_id=f"seed-{index}",
            kind="observation",
            text=(
                f"### 官方岗位证据 - **{company} {title}（{job_id}）** "
                f"官方链接：https://{host}/jd/{job_id} "
                "届别：2027 届校园招聘（页面原文「2027届」）。"
                "投递截止：页面未标注具体日期，标注“正文需人工核验”。"
                "职责原文摘录：> 参与大规模预训练与后训练流程的工程实现 "
                "> 负责推理侧的吞吐与延迟优化。"
                "要求原文摘录：> 熟悉 PyTorch 或同类深度学习框架 "
                "> 有分布式训练或推理部署经验者优先。"
                "匹配点：候选人有多智能体编排与本地记忆检索的可展示作品。"
                "缺口：缺少大规模分布式训练的直接经验，需要用小规模复现补齐。"
                + "补充说明：以上内容全部来自官方页面正文，未做推测。" * 6
            ),
        )
        for round_index in range(3):
            store.append(
                role_id="job_scout",
                run_id=f"seed-{index}-{round_index}",
                kind="plan",
                text="本轮计划：核验届别与岗位类型 → 检查官方来源 → 更新机会优先级",
            )
        store.append(
            role_id="job_scout",
            run_id=f"seed-{index}-r",
            kind="reflection",
            text=(
                "阶段反思：近期重点为 岗位、大模型、agent、校园招聘、"
                f"{host}。代表性经验：# 未投递岗位巡检报告（本轮共核验若干岗位，"
                "官方页面均可访问）。下一轮复用已验证方法并补充量化证据。"
            ),
        )
    return store


def synthetic_cases() -> list[RetrievalCase]:
    return [
        RetrievalCase(
            query=(
                f"岗位 {job_id} 的官方链接、届别和截止时间是什么？"
                "之前核验过的结论直接给我。"
            ),
            required=(job_id,),
            note=f"{company} {title}",
        )
        for job_id, company, title, _ in SYNTHETIC_JOBS
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--role", default="job_scout")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="用合成语料跑，不读本机 data/runtime",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.synthetic:
        with tempfile.TemporaryDirectory() as directory:
            store = build_synthetic_store(Path(directory) / "memories.jsonl")
            report = evaluate_retrieval(store, synthetic_cases(), k=args.k)
            label = "synthetic"
            emit(report, label, args.json)
        return

    path = runtime_data_dir() / "memories.jsonl"
    if not path.exists():
        raise SystemExit(f"没有找到记忆文件：{path}（可用 --synthetic）")
    store = MemoryStore(path)
    cases = discover_cases(store, role_id=args.role)
    if not cases:
        raise SystemExit(
            f"{args.role} 的记忆里找不到重复出现的岗位 ID，无法自动构造 golden set"
        )
    report = evaluate_retrieval(store, cases, k=args.k)
    emit(report, f"{path.name}:{args.role}", args.json)


def emit(report, label: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"corpus": label, **report.as_dict()}, ensure_ascii=False))
        return
    summary = report.as_dict()
    print(f"corpus={label}  k={summary['k']}  cases={summary['cases']}", end="")
    if summary["skipped"]:
        print(f"  skipped={summary['skipped']}", end="")
    print()
    print(f"  hit@{summary['k']}         {summary['hit_at_k']:.3f}")
    print(
        f"  recall@{summary['k']}      {summary['recall_at_k']:.3f}"
        f"   (ceiling {summary['recall_ceiling_at_k']:.3f}"
        f" —— k 个槽位装不下更多)"
    )
    print(f"  precision@{summary['k']}   {summary['precision_at_k']:.3f}")
    print(f"  boilerplate@{summary['k']} {summary['boilerplate_at_k']:.3f}")
    print()
    for score in report.scores:
        kinds = ",".join(score.top_kinds) or "（空）"
        print(
            f"  {score.case.required[0]:>9}"
            f"  relevant={score.relevant_total:<3}"
            f"  hit={int(score.hit)}"
            f"  recall={score.recall:.2f}"
            f"  top={kinds}"
        )


if __name__ == "__main__":
    main()
