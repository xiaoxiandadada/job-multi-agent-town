"""A real, auditable tool executor for job evidence.

Anthropic's server-side ``web_search`` / ``web_fetch`` tools are not available
on every provider surface (Amazon Bedrock, for instance, supports neither), so
a harness that only relied on them would silently fall back to paraphrasing
the local evidence pack — exactly the "abstract action list" failure this
project is trying to remove.

These tools run in our own process instead, using plain ``tool_use`` which
every provider supports:

``list_tracked_jobs``
    Read the maintained job table so the model starts from real companies,
    real role titles and real official URLs instead of guessing them.
``fetch_url``
    Fetch one of those official pages and hand back its readable text, so the
    model can quote the JD instead of inventing requirements.

``fetch_url`` is deliberately allowlisted: only hosts that already appear in
the local job table (plus anything the operator adds explicitly) can be
fetched. That keeps a prompt-injected "fetch this internal URL" from turning
the agent into an SSRF probe.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .role_context import default_prepare_dir


JOB_TABLE_RELATIVE = "jobs/autumn_job_tracker.md"

_URL_PATTERN = re.compile(r"https?://[^\s<>\"')|]+")
_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_END = re.compile(
    r"</(p|div|li|tr|h[1-6]|section|article|header|footer|table)\s*>",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")

LOCAL_TOOL_INSTRUCTIONS = """# 本地联网工具（本次调用可用）
你有两个在本机执行的工具，必须真的调用它们取证，不要凭印象作答：
- list_tracked_jobs：读取本地维护的岗位表，拿到真实公司、岗位全名、官方链接和备注。先调用它。
- fetch_url：抓取岗位表里的官方页面正文。只能抓 list_tracked_jobs 返回的、或用户消息里出现的链接；
  域名不在白名单时会被拒绝，这时写“抓取被拒绝：域名未授权”，不要换个链接硬编。
抓取失败就照实写“抓取失败：<原因>”，不要用推测补齐。

# 输出具体度要求（这是硬要求）
禁止只输出抽象清单或方法论。必须逐条给出可核对的实体：
1. 公司 + 岗位全名 + 岗位 ID（页面或岗位表里有就给，没有写“页面未标注”）
2. 完整官方 JD 链接
3. 届别、岗位类型、发布/截止时间，写明来源里的原文表述
4. JD 原文摘录：至少 2 句直接引用，用 > 引用块，不要改写
5. 与用户方向的匹配点与缺口，各自指向原文里的哪一句"""


def html_to_text(payload: str, max_chars: int = 12_000) -> str:
    """Turn a fetched page into quotable plain text."""

    text = _SCRIPT_STYLE.sub(" ", payload)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [
        re.sub(r"[ \t ]+", " ", line).strip()
        for line in text.splitlines()
    ]
    text = "\n".join(line for line in lines if line)
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[…正文已截断…]"
    return text


def _is_public_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


class LocalJobTools:
    """Tool definitions plus their implementations, sharing one allowlist."""

    def __init__(
        self,
        *,
        prepare_dir: str | Path | None = None,
        allowed_domains: list[str] | None = None,
        max_page_chars: int | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
        transport: Any | None = None,
    ):
        self.prepare_dir = Path(prepare_dir) if prepare_dir else default_prepare_dir()
        configured = allowed_domains
        if configured is None:
            raw = os.getenv("JOB_AGENT_FETCH_ALLOWED_DOMAINS", "")
            configured = [item.strip() for item in raw.split(",") if item.strip()]
        self.extra_allowed_domains = configured
        self.max_page_chars = max_page_chars or int(
            os.getenv("JOB_AGENT_FETCH_MAX_CHARS", "12000")
        )
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("JOB_AGENT_FETCH_TIMEOUT_SECONDS", "20")
        )
        self.max_response_bytes = max_response_bytes or int(
            os.getenv("JOB_AGENT_FETCH_MAX_BYTES", "2000000")
        )
        self.transport = transport
        self.fetched_urls: list[str] = []

    # ------------------------------------------------------------ job table

    @property
    def job_table_path(self) -> Path:
        return self.prepare_dir / JOB_TABLE_RELATIVE

    def tracked_jobs(self) -> list[dict[str, str]]:
        """Parse the maintained markdown job table into rows."""

        path = self.job_table_path
        if not path.is_file():
            return []
        jobs: list[dict[str, str]] = []
        headers: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not headers:
                headers = cells
                continue
            if all(set(cell) <= {"-", ":"} for cell in cells if cell):
                continue
            if len(cells) < 4:
                continue
            row = dict(zip(headers, cells))
            url_match = _URL_PATTERN.search(stripped)
            job = {
                "公司": row.get("公司", ""),
                "岗位": row.get("岗位", ""),
                "岗位方向": row.get("岗位方向", ""),
                "官方链接": url_match.group(0) if url_match else "",
                "缺口技能": row.get("缺口技能", ""),
                "是否投递": row.get("是否投递", ""),
                "备注": row.get("备注", ""),
            }
            if job["公司"] or job["岗位"]:
                jobs.append(job)
        return jobs

    def allowed_domains(self) -> set[str]:
        domains = {
            host
            for job in self.tracked_jobs()
            if (host := urlparse(job["官方链接"]).hostname)
        }
        domains.update(
            domain.lower().lstrip(".")
            for domain in self.extra_allowed_domains
        )
        return domains

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if not _is_public_host(host):
            return False
        return any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.allowed_domains()
        )

    # ---------------------------------------------------------------- tools

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_tracked_jobs",
                "description": (
                    "读取本地维护的 2027 届校招岗位表，返回真实公司、岗位全名、"
                    "官方 JD 链接、缺口技能和投递状态。先调用它再决定抓哪个链接。"
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "fetch_url",
                "description": (
                    "抓取一个官方招聘页面并返回正文纯文本，用于直接引用 JD 原文。"
                    "只允许抓取岗位表里出现过的域名。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "完整的 http(s) 链接",
                        }
                    },
                    "required": ["url"],
                },
            },
        ]

    @property
    def tool_names(self) -> set[str]:
        return {item["name"] for item in self.definitions()}

    async def run(self, name: str, payload: dict[str, Any]) -> str:
        if name == "list_tracked_jobs":
            jobs = self.tracked_jobs()
            if not jobs:
                return f"岗位表不存在或为空：{self.job_table_path}"
            return json.dumps(jobs, ensure_ascii=False, indent=2)
        if name == "fetch_url":
            return await self.fetch(str(payload.get("url", "")).strip())
        return f"未知工具：{name}"

    async def fetch(self, url: str) -> str:
        if not url:
            return "抓取被拒绝：缺少 url"
        if not self.is_allowed(url):
            allowed = ", ".join(sorted(self.allowed_domains())) or "（白名单为空）"
            return (
                f"抓取被拒绝：域名未授权 {url}\n"
                f"当前允许的域名：{allowed}\n"
                "如需新增，请在 JOB_AGENT_FETCH_ALLOWED_DOMAINS 里配置。"
            )
        options: dict[str, Any] = {
            "timeout": self.timeout_seconds,
            "follow_redirects": True,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        }
        if self.transport is not None:
            options["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**options) as client:
                response = await client.get(url)
                response.raise_for_status()
                body = response.content[: self.max_response_bytes]
        except httpx.HTTPStatusError as exc:
            return f"抓取失败：HTTP {exc.response.status_code} {url}"
        except httpx.HTTPError as exc:
            return f"抓取失败：{type(exc).__name__} {url}"
        self.fetched_urls.append(url)
        encoding = response.encoding or "utf-8"
        try:
            payload = body.decode(encoding, errors="replace")
        except LookupError:
            payload = body.decode("utf-8", errors="replace")
        text = html_to_text(payload, max_chars=self.max_page_chars)
        if not text:
            return (
                f"抓取成功但正文为空（可能是前端渲染页面）：{url}\n"
                "请改用岗位表备注里的信息，并标注“正文需人工核验”。"
            )
        return f"来源：{url}\n\n{text}"
