"""Claude (Anthropic) model client for the multi-agent harness.

This client is what makes an ``@`` mention return real, checkable content:
roles whose ``tools`` include ``web_search`` get Anthropic's server-side
``web_search`` / ``web_fetch`` tools, so the model reads live job pages
instead of paraphrasing the local evidence pack. Every fetched URL is
collected and appended as a source list, which is the difference between
"here is an action list" and "here is the JD, at this link, closing on
this date".
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .model_client import RolePromptComposer
from .models import ImageAttachment, ModelReply, RoleSpec
from .tools_executor import LOCAL_TOOL_INSTRUCTIONS, LocalJobTools


LOGGER = logging.getLogger("job_agent_harness.anthropic")


WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_FETCH_TOOL_TYPE = "web_fetch_20260209"

#: Substrings that mean "this provider surface has no server-side web tools".
#: Amazon Bedrock and Vertex AI reject them outright, so ``auto`` mode retries
#: with the local tool executor instead of failing the whole run.
UNSUPPORTED_SERVER_TOOL_MARKERS = (
    WEB_SEARCH_TOOL_TYPE,
    WEB_FETCH_TOOL_TYPE,
    "web_search",
    "web_fetch",
)

SERVER_WEB_INSTRUCTIONS = """# 联网核验规则（本次调用已开启联网工具）
你现在有 web_search 和 web_fetch 两个服务端工具，必须真的用它们核验，不要只复述资料包。
- web_fetch 只能抓取本对话里已经出现过的 URL。所以顺序固定为：先 web_search 找到官方招聘页，再 web_fetch 抓正文；资料包里已给出的官方链接可以直接 web_fetch。
- 优先官方来源：企业校招官网、官方招聘平台页面。不要引用聚合站、猎头号、二手转载作为唯一依据。
- 每条结论后面写清来源 URL。抓不到正文就写“抓取失败：<原因>”，不要用推测填充。

# 输出具体度要求（这是硬要求）
禁止只输出抽象清单或方法论。必须逐条给出可核对的实体：
1. 公司 + 岗位全名 + 岗位 ID（若页面有）
2. 官方 JD 链接（完整 URL）
3. 届别、岗位类型、发布/截止时间（写明页面上的原文表述）
4. JD 原文摘录：至少 2 句直接引用，用 > 引用块，不要改写
5. 与用户方向的匹配点与缺口，各自指向 JD 原文里的哪一句
如果某条信息页面上没有，写“页面未标注”，不要留空也不要编。"""

#: Sent back when a reply stopped at ``max_tokens``. Truncated answers used to
#: reach Feishu with an unclosed code fence, which swallowed the whole message.
CONTINUE_PROMPT = (
    "上一段在输出上限处被截断了。请从截断的位置继续写完，"
    "不要重复已经写过的内容，也不要重新开头或重写标题。"
)


def _is_truthy(value: str | None, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _normalize_web_mode(value: bool | str | None) -> str:
    """Resolve the web-tool mode.

    ``auto`` (the default) prefers Anthropic's server-side tools and falls
    back to the local executor when the provider rejects them. ``server`` and
    ``local`` pin one of the two; ``off`` grounds roles in the local evidence
    pack only.
    """

    if value is True:
        return "auto"
    if value is False:
        return "off"
    text = (value or "").strip().lower()
    if not text:
        return "auto"
    if text in {"1", "true", "yes", "on", "auto"}:
        return "auto"
    if text in {"0", "false", "no", "off", "none"}:
        return "off"
    if text in {"server", "anthropic", "remote"}:
        return "server"
    if text in {"local", "self", "executor"}:
        return "local"
    raise ValueError(
        "JOB_AGENT_CLAUDE_WEB_TOOLS 必须是 auto、server、local 或 off"
    )


class AnthropicClient(RolePromptComposer):
    """Call Claude through the official Anthropic SDK.

    Model tiers follow the role's ``model_profile``: the knowledge curator
    runs on Claude Opus 5, every other role runs on Claude Sonnet 5. Both
    are overridable per role via ``JOB_AGENT_ROLE_MODEL_<ROLE_ID>`` or a
    registry ``model`` override, so no code change is needed to move a
    single role to another tier.
    """

    default_model_env = "JOB_AGENT_MODEL"
    default_model_fallback = "claude-sonnet-5"
    knowledge_model_fallback = "claude-opus-5"
    max_output_tokens_fallback = 12000

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        default_model: str | None = None,
        judge_model: str | None = None,
        knowledge_model: str | None = None,
        reliable_model: str | None = None,
        max_output_tokens: int | None = None,
        knowledge_max_output_tokens: int | None = None,
        project_context_path: str | Path | None = None,
        prepare_dir: str | Path | None = None,
        role_context_max_chars: int | None = None,
        seed_roles_path: str | Path | None = None,
        effort: str | None = None,
        thinking: bool | None = None,
        web_tools: bool | str | None = None,
        search_max_uses: int | None = None,
        fetch_max_uses: int | None = None,
        max_continuations: int | None = None,
        max_tool_iterations: int | None = None,
        max_retries: int | None = None,
        cache_system_prompt: bool | None = None,
        stream: bool | None = None,
        local_tools: LocalJobTools | None = None,
        client: Any | None = None,
    ):
        super().__init__(
            default_model=default_model,
            judge_model=judge_model,
            knowledge_model=knowledge_model,
            reliable_model=reliable_model,
            max_output_tokens=max_output_tokens,
            project_context_path=project_context_path,
            prepare_dir=prepare_dir,
            role_context_max_chars=role_context_max_chars,
            seed_roles_path=seed_roles_path,
        )
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = base_url or os.getenv("ANTHROPIC_BASE_URL", "")
        self.knowledge_max_output_tokens = knowledge_max_output_tokens or int(
            os.getenv(
                "JOB_AGENT_KNOWLEDGE_MAX_OUTPUT_TOKENS",
                str(self.max_output_tokens * 2),
            )
        )
        self.effort = (effort or os.getenv("JOB_AGENT_CLAUDE_EFFORT", "")).strip()
        self.thinking = (
            thinking
            if thinking is not None
            else _is_truthy(os.getenv("JOB_AGENT_CLAUDE_THINKING"), True)
        )
        self.web_mode = _normalize_web_mode(
            web_tools
            if web_tools is not None
            else os.getenv("JOB_AGENT_CLAUDE_WEB_TOOLS")
        )
        self.local_tools = local_tools or LocalJobTools(prepare_dir=self.prepare_dir)
        self._server_tools_supported: bool | None = None
        # None means "stream, but downgrade if the gateway's SSE is broken".
        configured_stream = (
            stream
            if stream is not None
            else os.getenv("JOB_AGENT_CLAUDE_STREAM")
        )
        self._streaming_supported: bool | None = (
            None
            if configured_stream is None
            or str(configured_stream).strip().lower() in {"", "auto"}
            else _is_truthy(str(configured_stream))
        )
        self.search_max_uses = search_max_uses or int(
            os.getenv("JOB_AGENT_CLAUDE_SEARCH_MAX_USES", "6")
        )
        self.fetch_max_uses = fetch_max_uses or int(
            os.getenv("JOB_AGENT_CLAUDE_FETCH_MAX_USES", "6")
        )
        self.max_continuations = (
            max_continuations
            if max_continuations is not None
            else int(os.getenv("JOB_AGENT_CLAUDE_MAX_CONTINUATIONS", "4"))
        )
        self.max_tool_iterations = (
            max_tool_iterations
            if max_tool_iterations is not None
            else int(os.getenv("JOB_AGENT_CLAUDE_MAX_TOOL_ITERATIONS", "8"))
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.getenv("JOB_AGENT_CLAUDE_MAX_RETRIES", "2"))
        )
        self.cache_system_prompt = (
            cache_system_prompt
            if cache_system_prompt is not None
            else _is_truthy(os.getenv("JOB_AGENT_CLAUDE_PROMPT_CACHE"), True)
        )
        self.search_country = os.getenv("JOB_AGENT_SEARCH_COUNTRY", "CN").strip()
        self.search_timezone = os.getenv(
            "JOB_AGENT_SEARCH_TIMEZONE", "Asia/Shanghai"
        ).strip()
        self._client = client

    # ------------------------------------------------------------------ setup

    def client(self):
        """Build the async SDK client lazily so import never needs a key."""

        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "anthropic SDK 未安装；请运行 uv sync"
                ) from exc
            options: dict[str, Any] = {}
            if self.api_key:
                options["api_key"] = self.api_key
            if self.base_url:
                options["base_url"] = self.base_url
            self._client = AsyncAnthropic(**options)
        return self._client

    def web_mode_for(self, role: RoleSpec) -> str:
        """Which evidence tools this role gets on this provider."""

        if self.web_mode == "off" or "web_search" not in role.tools:
            return "off"
        if self.web_mode == "local":
            return "local"
        if self.web_mode == "server":
            return "server"
        # auto: prefer server tools until the provider tells us otherwise.
        return "server" if self._server_tools_supported is not False else "local"

    def uses_web_tools(self, role: RoleSpec) -> bool:
        return self.web_mode_for(role) != "off"

    def server_tools_for(self, role: RoleSpec) -> list[dict[str, Any]]:
        """Server-side tool definitions for one role.

        Dynamic filtering is built into the ``_20260209`` tool versions, so
        ``code_execution`` is deliberately not declared alongside them.
        """

        if self.web_mode_for(role) != "server":
            return []
        search: dict[str, Any] = {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": self.search_max_uses,
        }
        if self.search_country:
            location: dict[str, Any] = {
                "type": "approximate",
                "country": self.search_country,
            }
            if self.search_timezone:
                location["timezone"] = self.search_timezone
            search["user_location"] = location
        fetch: dict[str, Any] = {
            "type": WEB_FETCH_TOOL_TYPE,
            "name": "web_fetch",
            "max_uses": self.fetch_max_uses,
            "citations": {"enabled": True},
        }
        return [search, fetch]

    def local_tools_for(self, role: RoleSpec) -> list[dict[str, Any]]:
        if self.web_mode_for(role) != "local":
            return []
        return self.local_tools.definitions()

    def max_tokens_for(self, role: RoleSpec) -> int:
        if role.model_profile == "knowledge":
            return max(self.max_output_tokens, self.knowledge_max_output_tokens)
        return self.max_output_tokens

    def system_blocks_for(self, role: RoleSpec) -> list[dict[str, Any]]:
        text = self.system_prompt_for(role)
        mode = self.web_mode_for(role)
        if mode == "server":
            text = f"{text}\n\n{SERVER_WEB_INSTRUCTIONS}"
        elif mode == "local":
            text = f"{text}\n\n{LOCAL_TOOL_INSTRUCTIONS}"
        block: dict[str, Any] = {"type": "text", "text": text}
        if self.cache_system_prompt:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def request_kwargs(self, role: RoleSpec) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model_for(role),
            "max_tokens": self.max_tokens_for(role),
            "system": self.system_blocks_for(role),
        }
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        tools = self.server_tools_for(role) + self.local_tools_for(role)
        if tools:
            kwargs["tools"] = tools
        return kwargs

    # -------------------------------------------------------------- responses

    @staticmethod
    def _block_type(block: Any) -> str:
        return str(getattr(block, "type", "") or "")

    @staticmethod
    def _error_code(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("error_code") or "")
        return str(getattr(payload, "error_code", "") or "")

    @staticmethod
    def _field(payload: Any, name: str) -> str:
        if isinstance(payload, dict):
            return str(payload.get(name) or "")
        return str(getattr(payload, name, "") or "")

    def collect_blocks(
        self,
        blocks: Any,
        *,
        texts: list[str],
        sources: dict[str, str],
        notes: list[str],
    ) -> None:
        """Pull answer text, citation URLs and tool failures out of a reply."""

        for block in blocks or []:
            kind = self._block_type(block)
            if kind == "text":
                value = getattr(block, "text", "")
                if value:
                    texts.append(value)
                for citation in getattr(block, "citations", None) or []:
                    url = self._field(citation, "url")
                    if not url:
                        continue
                    title = (
                        self._field(citation, "title")
                        or self._field(citation, "document_title")
                        or url
                    )
                    sources.setdefault(url, title)
            elif kind == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    for item in content:
                        url = self._field(item, "url")
                        if url:
                            sources.setdefault(
                                url, self._field(item, "title") or url
                            )
                    continue
                code = self._error_code(content)
                if code:
                    notes.append(f"web_search 未完成：{code}")
            elif kind == "web_fetch_tool_result":
                content = getattr(block, "content", None)
                code = self._error_code(content)
                if code:
                    notes.append(f"web_fetch 未完成：{code}")
                    continue
                url = self._field(content, "url")
                if url:
                    sources.setdefault(url, url)

    SERVER_TOOL_BLOCK_TYPES = (
        "server_tool_use",
        "web_search_tool_result",
        "web_fetch_tool_result",
    )

    @classmethod
    def server_tools_ran(cls, content: Any) -> bool:
        """Did this turn actually contain a server-side tool block?"""

        return any(
            cls._block_type(block) in cls.SERVER_TOOL_BLOCK_TYPES
            for block in content or []
        )

    @staticmethod
    def refusal_text(message: Any) -> str:
        details = getattr(message, "stop_details", None)
        category = str(getattr(details, "category", "") or "unspecified")
        explanation = str(getattr(details, "explanation", "") or "")
        suffix = f"：{explanation}" if explanation else ""
        return (
            "⚠️ 模型拒绝回答这次请求"
            f"（类别 {category}{suffix}）。"
            "请换一个更具体、只涉及公开招聘信息的问题重试。"
        )

    def render(
        self,
        *,
        texts: list[str],
        sources: dict[str, str],
        notes: list[str],
        truncated: bool,
        exhausted: bool,
    ) -> str:
        body = "\n".join(part for part in texts if part).strip()
        sections = [body] if body else []
        if sources:
            listed = "\n".join(
                f"- [{title}]({url})" if title and title != url else f"- {url}"
                for url, title in list(sources.items())[:20]
            )
            sections.append(f"## 联网来源（本次真实抓取）\n{listed}")
        warnings = list(notes)
        if truncated:
            warnings.append(
                "自动续写了 "
                f"{self.max_continuations} 轮仍未写完；请调高 "
                "JOB_AGENT_MAX_OUTPUT_TOKENS 或把问题拆小。"
            )
        if exhausted:
            warnings.append(
                "工具或续写轮次达到上限"
                "（JOB_AGENT_CLAUDE_MAX_CONTINUATIONS / "
                "JOB_AGENT_CLAUDE_MAX_TOOL_ITERATIONS），结论可能不完整。"
            )
        if warnings:
            sections.append(
                "## 运行提示\n" + "\n".join(f"- {item}" for item in warnings)
            )
        return "\n\n".join(sections).strip() or "（模型没有返回文本内容）"

    # ------------------------------------------------------------------- call

    async def send(
        self,
        client: Any,
        *,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        """One request. Streams by default, falls back when a gateway can't.

        Streaming is the right default — these calls carry a big max_tokens and
        would otherwise risk a request timeout. But a proxy can emit
        ``content_block_delta`` events with no matching ``content_block_start``,
        and the SDK's accumulator then indexes past the end of the block list.
        That surfaced as a bare ``IndexError`` mid-patrol. Non-streaming skips
        the accumulator, so use it for the rest of the process once we have seen
        a malformed stream.
        """

        if self._streaming_supported is not False:
            try:
                async with client.messages.stream(
                    messages=messages,
                    **kwargs,
                ) as stream:
                    message = await stream.get_final_message()
            except IndexError:
                self._streaming_supported = False
                LOGGER.warning("上游返回的 SSE 事件不完整，本进程改用非流式请求。")
            else:
                self._streaming_supported = True
                return message
        return await client.messages.create(messages=messages, **kwargs)

    @staticmethod
    def is_unsupported_server_tool_error(exc: BaseException) -> bool:
        """Did the provider reject the server-side web tools themselves?"""

        try:
            from anthropic import BadRequestError
        except ImportError:  # pragma: no cover - depends on env
            return False
        if not isinstance(exc, BadRequestError):
            return False
        message = str(exc).lower()
        if "not supported" not in message and "unsupported" not in message:
            return False
        return any(
            marker.lower() in message for marker in UNSUPPORTED_SERVER_TOOL_MARKERS
        )

    async def complete(
        self,
        role: RoleSpec,
        query: str,
        *,
        images: Sequence[ImageAttachment] = (),
    ) -> ModelReply:
        model = self.model_for(role)
        if not model:
            raise RuntimeError(
                "Claude 模型未配置；请设置 JOB_AGENT_MODEL 或角色级 model"
            )
        mode = self.web_mode_for(role)
        trace: dict[str, Any] = {}
        try:
            reply = await self._dispatch(
                role, query, model=model, mode=mode, trace=trace, images=images
            )
        except Exception as exc:
            if not (
                mode == "server"
                and self.web_mode == "auto"
                and self.is_unsupported_server_tool_error(exc)
            ):
                raise
            # The provider rejected the tool type outright.
            return await self._fallback_to_local(
                role,
                query,
                model=model,
                reason="当前 provider 不支持 Claude 服务端联网工具",
                images=images,
            )
        if mode != "server":
            return reply
        if trace.get("server_tools_ran"):
            self._server_tools_supported = True
            return reply
        if self.web_mode != "auto" or self._server_tools_supported is not None:
            return reply
        # A gateway can also accept the request and silently drop the tools.
        # The tell-tale sign is a finished answer with no server tool block in
        # it, which is how the model ends up printing {"query": ...} as prose
        # instead of actually searching. Probe the local path once — the result
        # is remembered, so a role that simply chose not to search costs at
        # most one extra call per process.
        return await self._fallback_to_local(
            role,
            query,
            model=model,
            reason="当前 provider 声明接受了联网工具但一次都没执行",
            images=images,
        )

    async def _fallback_to_local(
        self,
        role: RoleSpec,
        query: str,
        *,
        model: str,
        reason: str,
        images: Sequence[ImageAttachment] = (),
    ) -> ModelReply:
        """Re-run with the in-process tool executor and say why.

        Bedrock and Vertex have no server-side web tools. Remember that for the
        rest of this process so later calls skip the wasted attempt, and answer
        from the local job table plus a real fetch, so an @mention still comes
        back with checkable JD text instead of a paraphrase.
        """

        self._server_tools_supported = False
        reply = await self._dispatch(
            role, query, model=model, mode="local", images=images
        )
        return reply.model_copy(
            update={
                "content": (
                    f"{reply.content}\n\n"
                    "## 运行提示\n"
                    f"- {reason}，"
                    "已自动切换到本地 Tool Executor（岗位表 + 官方页抓取）。"
                )
            }
        )

    async def _dispatch(
        self,
        role: RoleSpec,
        query: str,
        *,
        model: str,
        mode: str,
        trace: dict[str, Any] | None = None,
        images: Sequence[ImageAttachment] = (),
    ) -> ModelReply:
        client = self.client()
        if hasattr(client, "with_options"):
            client = client.with_options(
                timeout=role.timeout_seconds,
                max_retries=self.max_retries,
            )
        kwargs = self.request_kwargs(role)
        if mode != self.web_mode_for(role):
            # Forced mode (the auto fallback): rebuild the tool-dependent parts.
            forced = self.model_copy_for_mode(role, mode)
            kwargs["system"] = forced["system"]
            if forced["tools"]:
                kwargs["tools"] = forced["tools"]
            else:
                kwargs.pop("tools", None)
        base_message: dict[str, Any] = {"role": "user", "content": query}
        if images:
            # Pictures before the caption. Claude reads the trailing text as
            # being about the blocks above it, so putting the task first makes
            # it start answering before it has looked at the screenshot.
            base_message = {
                "role": "user",
                "content": [
                    *(image.to_anthropic_block() for image in images),
                    {"type": "text", "text": query},
                ],
            }
        messages: list[dict[str, Any]] = [base_message]

        texts: list[str] = []
        sources: dict[str, str] = {}
        notes: list[str] = []
        input_tokens = 0
        output_tokens = 0
        truncated = False
        exhausted = False
        answered_model = model
        continuations = 0
        tool_rounds = 0
        hard_cap = self.max_continuations + self.max_tool_iterations + 2

        for _ in range(hard_cap):
            message = await self.send(client, messages=messages, kwargs=kwargs)
            usage = getattr(message, "usage", None)
            input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            answered_model = str(getattr(message, "model", "") or model)
            stop_reason = getattr(message, "stop_reason", None)
            if stop_reason == "refusal":
                return ModelReply(
                    content=self.refusal_text(message),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model=answered_model,
                )
            content = getattr(message, "content", None)
            if trace is not None and self.server_tools_ran(content):
                trace["server_tools_ran"] = True
            self.collect_blocks(
                content,
                texts=texts,
                sources=sources,
                notes=notes,
            )
            if stop_reason == "max_tokens":
                # Hitting the output cap used to end the answer mid-sentence,
                # which is how an unclosed code fence swallowed a whole reply
                # in Feishu. Carry on in a new round instead of giving up.
                if continuations >= self.max_continuations:
                    truncated = True
                    break
                continuations += 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": CONTINUE_PROMPT},
                ]
                continue
            if stop_reason == "tool_use":
                if tool_rounds >= self.max_tool_iterations:
                    exhausted = True
                    break
                tool_rounds += 1
                results = await self.run_local_tools(content, sources=sources)
                if not results:
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": results},
                ]
                continue
            if stop_reason != "pause_turn":
                break
            if continuations >= self.max_continuations:
                exhausted = True
                break
            continuations += 1
            # Server-side tool loop hit its iteration cap. Resume by handing
            # the paused assistant turn back; do not append a "Continue."
            # message, that changes what the model was doing.
            messages = [
                base_message,
                {"role": "assistant", "content": content},
            ]
        else:
            exhausted = True

        return ModelReply(
            content=self.render(
                texts=texts,
                sources=sources,
                notes=notes,
                truncated=truncated,
                exhausted=exhausted,
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=answered_model,
        )

    def model_copy_for_mode(self, role: RoleSpec, mode: str) -> dict[str, Any]:
        """System prompt and tools for an explicitly forced web mode."""

        text = self.system_prompt_for(role)
        tools: list[dict[str, Any]] = []
        if mode == "server":
            text = f"{text}\n\n{SERVER_WEB_INSTRUCTIONS}"
            search = {
                "type": WEB_SEARCH_TOOL_TYPE,
                "name": "web_search",
                "max_uses": self.search_max_uses,
            }
            tools = [
                search,
                {
                    "type": WEB_FETCH_TOOL_TYPE,
                    "name": "web_fetch",
                    "max_uses": self.fetch_max_uses,
                    "citations": {"enabled": True},
                },
            ]
        elif mode == "local":
            text = f"{text}\n\n{LOCAL_TOOL_INSTRUCTIONS}"
            tools = self.local_tools.definitions()
        block: dict[str, Any] = {"type": "text", "text": text}
        if self.cache_system_prompt:
            block["cache_control"] = {"type": "ephemeral"}
        return {"system": [block], "tools": tools}

    async def run_local_tools(
        self,
        content: Any,
        *,
        sources: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Execute the locally implemented tools Claude just asked for."""

        results: list[dict[str, Any]] = []
        for block in content or []:
            if self._block_type(block) != "tool_use":
                continue
            name = str(getattr(block, "name", "") or "")
            payload = getattr(block, "input", None) or {}
            if name not in self.local_tools.tool_names:
                output = f"未知工具：{name}"
            else:
                output = await self.local_tools.run(name, dict(payload))
            if name == "fetch_url":
                url = str(dict(payload).get("url", "") or "")
                if url and not output.startswith(("抓取失败", "抓取被拒绝")):
                    sources.setdefault(url, url)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": getattr(block, "id", ""),
                    "content": output,
                }
            )
        return results

    async def available_models(self) -> list[str]:
        client = self.client()
        names: list[str] = []
        async for item in client.models.list():
            model_id = getattr(item, "id", "")
            if model_id:
                names.append(str(model_id))
        return sorted(names)
