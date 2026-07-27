from __future__ import annotations


REQUIRED_MODEL_BENCHMARK_SECTIONS = (
    "关键能力",
    "工程难点",
    "作品证据",
    "面试问题",
    "学习优先级",
    "待核验",
)


def score_model_output(content: str) -> dict[str, object]:
    hits = [
        section
        for section in REQUIRED_MODEL_BENCHMARK_SECTIONS
        if section in content
    ]
    return {
        "section_hits": len(hits),
        "section_total": len(REQUIRED_MODEL_BENCHMARK_SECTIONS),
        "missing_sections": [
            section
            for section in REQUIRED_MODEL_BENCHMARK_SECTIONS
            if section not in hits
        ],
        "has_verification_marker": "待核验" in content,
        "output_chars": len(content),
    }
