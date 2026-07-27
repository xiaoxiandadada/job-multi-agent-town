from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

from dotenv import load_dotenv

from job_agent_harness.benchmarking import score_model_output
from job_agent_harness.model_client import OpenAICompatibleClient
from job_agent_harness.models import RoleSpec


load_dotenv()

DEFAULT_QUERY = (
    "目标是中国 2027 届 AI Agent / 大模型应用正式校招。"
    "请补充这个方向的岗位知识并给出可执行准备动作；"
    "没有具体公司 JD 的内容必须标记待核验。"
)


async def run_once(model: str, query: str, max_tokens: int) -> dict:
    role = RoleSpec(
        role_id="model_benchmark",
        display_name="岗位知识基准角色",
        goal="生成可核验的 AI 求职知识与行动建议",
        system_prompt=(
            "你是求职岗位知识分析师。固定使用以下六个二级标题："
            "关键能力、工程难点、作品证据、面试问题、学习优先级、待核验。"
            "不要编造公司要求、指标或链接。每节保持简洁。"
        ),
        timeout_seconds=120,
    )
    client = OpenAICompatibleClient(
        default_model=model,
        judge_model=model,
        knowledge_model=model,
        max_output_tokens=max_tokens,
    )
    started = time.perf_counter()
    try:
        reply = await client.complete(role, query)
    except Exception as exc:
        return {
            "model": model,
            "status": "error",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_type": type(exc).__name__,
        }
    return {
        "model": model,
        "status": "ok",
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        **score_model_output(reply.content),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare OpenAI-compatible models on one fixed job task."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=[
            os.getenv("JOB_AGENT_MODEL", "qwen3.5-flash"),
            "Qwen/Qwen2.5-32B-Instruct",
            "doubao-seed-1-6-flash-250715",
        ],
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=320)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()

    rows = []
    for model in dict.fromkeys(args.models):
        samples = [
            await run_once(model, args.query, args.max_tokens)
            for _ in range(max(1, args.runs))
        ]
        successful = [row for row in samples if row["status"] == "ok"]
        summary = {
            "model": model,
            "runs": len(samples),
            "successful_runs": len(successful),
            "samples": samples,
        }
        if successful:
            summary.update(
                {
                    "median_latency_ms": round(
                        statistics.median(
                            row["latency_ms"] for row in successful
                        ),
                        2,
                    ),
                    "median_section_hits": statistics.median(
                        row["section_hits"] for row in successful
                    ),
                    "median_output_tokens": statistics.median(
                        row["output_tokens"] for row in successful
                    ),
                }
            )
        rows.append(summary)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
