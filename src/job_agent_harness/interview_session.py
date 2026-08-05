from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import RoleSpec


#: The coach role that owns a mock interview.
COACH_ROLE_ID = "interview_coach"
#: Rounds a session plans by default: long enough to reach a follow-up chain,
#: short enough to finish in one sitting.
DEFAULT_PLANNED_TURNS = 5
MAX_PLANNED_TURNS = 12
#: A session older than this is a leftover, not a conversation in progress.
SESSION_IDLE_SECONDS = 6 * 3600

SCORE_PATTERN = re.compile(r"评分[：:]\s*([0-9]{1,2}(?:\.[0-9])?)")
NEXT_QUESTION_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(?:\*\*)?(?:下一题|下一道题|第\s*\d+\s*题)"
    r"(?:\*\*)?\s*[：:]?\s*",
)
VOICE_WORDS = {"语音", "voice", "口述", "说话"}
NO_VOICE_WORDS = {"文字", "text", "静音", "no-voice", "novoice"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InterviewTurn(BaseModel):
    """One question, the answer it got, and how that answer scored."""

    index: int
    question: str
    answer: str = ""
    score: float | None = None
    feedback: str = ""
    asked_at: str = Field(default_factory=now_iso)
    answered_at: str | None = None
    #: The answer arrived as a voice message rather than typed text.
    spoken: bool = False


class InterviewSession(BaseModel):
    """A mock interview that survives between Feishu messages.

    Feishu delivers one message per callback with no conversation state, so
    "interactive" has to mean persisted: the coach re-reads this record to know
    which question is open and what the candidate already said.
    """

    chat_id: str
    identity: str = COACH_ROLE_ID
    topic: str
    role_id: str = COACH_ROLE_ID
    status: Literal["asking", "closed"] = "asking"
    #: Reply with a spoken question as well as the written one.
    voice: bool = False
    planned_turns: int = DEFAULT_PLANNED_TURNS
    turns: list[InterviewTurn] = Field(default_factory=list)
    started_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    @property
    def open_turn(self) -> InterviewTurn | None:
        """The question the candidate still owes an answer to."""

        return next(
            (turn for turn in reversed(self.turns) if not turn.answer),
            None,
        )

    @property
    def answered_turns(self) -> list[InterviewTurn]:
        return [turn for turn in self.turns if turn.answer]

    @property
    def scores(self) -> list[float]:
        return [
            turn.score
            for turn in self.turns
            if turn.score is not None
        ]

    @property
    def average_score(self) -> float | None:
        scores = self.scores
        if not scores:
            return None
        return round(sum(scores) / len(scores), 1)

    def ask(self, question: str) -> InterviewTurn:
        turn = InterviewTurn(index=len(self.turns) + 1, question=question)
        self.turns.append(turn)
        self.updated_at = now_iso()
        return turn

    def answer(
        self,
        text: str,
        *,
        spoken: bool = False,
    ) -> InterviewTurn:
        turn = self.open_turn
        if turn is None:
            # An answer with no open question still belongs to the transcript;
            # dropping it would make the next prompt lie about what was said.
            turn = self.ask("（本轮由候选人主动补充）")
        turn.answer = text
        turn.spoken = spoken
        turn.answered_at = now_iso()
        self.updated_at = now_iso()
        return turn

    def is_finished(self) -> bool:
        return len(self.answered_turns) >= self.planned_turns


@dataclass(frozen=True)
class InterviewCommand:
    action: Literal["start", "answer", "skip", "status", "end", "help"]
    payload: str = ""
    voice: bool | None = None
    planned_turns: int | None = None


def _looks_like_other_command(text: str) -> bool:
    return text.startswith("/") and not text.startswith("/mock")


def parse_interview_command(
    text: str,
    *,
    session_open: bool,
) -> InterviewCommand | None:
    """Read one Feishu message as a mock-interview move.

    ``None`` means "not mine": with no session open a plain question must still
    reach the normal single-role run, otherwise starting the coach bot would
    break every other way of talking to it.
    """

    value = text.strip()
    if not value:
        return None
    if _looks_like_other_command(value):
        return None

    if value.startswith("/mock"):
        rest = value.removeprefix("/mock").strip()
        keyword, _, remainder = rest.partition(" ")
        keyword = keyword.strip().casefold()
        if not rest or keyword in {"help", "帮助"}:
            return InterviewCommand(action="help")
        if keyword in {"end", "结束", "stop"}:
            return InterviewCommand(action="end")
        if keyword in {"status", "状态"}:
            return InterviewCommand(action="status")
        if keyword in {"skip", "next", "跳过", "下一题"}:
            return InterviewCommand(action="skip")
        if keyword in {"start", "开始"}:
            rest = remainder.strip()
        topic, voice, planned_turns = _parse_start_options(rest)
        if not topic:
            return InterviewCommand(action="help")
        return InterviewCommand(
            action="start",
            payload=topic,
            voice=voice,
            planned_turns=planned_turns,
        )

    if session_open:
        return InterviewCommand(action="answer", payload=value)
    return None


def _parse_start_options(rest: str) -> tuple[str, bool | None, int | None]:
    voice: bool | None = None
    planned_turns: int | None = None
    words: list[str] = []
    for word in rest.split():
        lowered = word.casefold().strip("，,。")
        if lowered in VOICE_WORDS:
            voice = True
            continue
        if lowered in NO_VOICE_WORDS:
            voice = False
            continue
        rounds = re.fullmatch(r"(\d{1,2})\s*(?:轮|rounds?|turns?)", lowered)
        if rounds:
            planned_turns = max(1, min(MAX_PLANNED_TURNS, int(rounds.group(1))))
            continue
        words.append(word)
    return " ".join(words).strip(), voice, planned_turns


@dataclass(frozen=True)
class CoachReply:
    """What the model said, split into the parts the flow needs."""

    score: float | None
    feedback: str
    question: str


def parse_coach_reply(text: str) -> CoachReply:
    """Split a review reply into score, feedback and the next question.

    The model is asked for a fixed shape, but a mock interview must not break
    when it drifts: an unparsed reply degrades to "all feedback, no next
    question", and the caller asks a fresh question instead of losing the turn.
    """

    body = text.strip()
    score_match = SCORE_PATTERN.search(body)
    score = float(score_match.group(1)) if score_match else None
    if score is not None:
        score = max(0.0, min(10.0, score))

    split = NEXT_QUESTION_PATTERN.search(body)
    if split is None:
        return CoachReply(score=score, feedback=body, question="")
    feedback = body[: split.start()].strip()
    question = body[split.end() :].strip()
    return CoachReply(score=score, feedback=feedback, question=question)


def _transcript(session: InterviewSession, *, limit: int = 6) -> str:
    if not session.turns:
        return "（还没有问答记录）"
    lines: list[str] = []
    for turn in session.turns[-limit:]:
        lines.append(f"第 {turn.index} 题：{turn.question}")
        if turn.answer:
            score = "未评分" if turn.score is None else f"{turn.score}/10"
            lines.append(f"候选人回答（{score}）：{turn.answer}")
        else:
            lines.append("候选人回答：（本题尚未作答）")
    return "\n".join(lines)


SHARED_RULES = """规则：
- 这是实时语音/文字模拟面试，不是一次性题库输出。
- 一次只问一道题，问完立刻停下，等候选人回答，不要自问自答。
- 题目必须回指真实材料：秋招追踪表里的公司与岗位、当天学习内容、或候选人自己的项目证据。
- 公司真题不确定就标「通用模拟」，不要伪装成真题。
- 整段回答不要包在代码块里。"""


def build_opening_prompt(session: InterviewSession) -> str:
    """Ask for the first question only."""

    return "\n\n".join(
        [
            f"# 模拟面试开场（共 {session.planned_turns} 轮）",
            f"面试主题：{session.topic}",
            SHARED_RULES,
            "本轮只输出：\n"
            "1. 一句开场（面试官口吻，说明角色和节奏）\n"
            "2. `第 1 题：` + 一道题\n"
            "3. `考察点：` 一行\n"
            "不要给出参考答案、rubric 或后续题目。",
        ]
    )


def build_review_prompt(
    session: InterviewSession,
    answer: str,
    *,
    spoken: bool = False,
) -> str:
    """Score the answer that just arrived, then ask the next question."""

    turn = session.open_turn
    question = turn.question if turn is not None else session.topic
    answered = len(session.answered_turns) + 1
    remaining = max(0, session.planned_turns - answered)
    source = "语音转写" if spoken else "文字输入"
    tail = (
        "4. `下一题：` + 下一道题（难度按上一题表现调整，可以是对同一项目的深挖追问）"
        if remaining
        else "4. `收尾：` 一句总结，并说明本轮面试到此结束（不要再出新题）"
    )
    return "\n\n".join(
        [
            f"# 模拟面试第 {answered} 轮评分（还剩 {remaining} 轮）",
            f"面试主题：{session.topic}",
            f"本题：{question}",
            f"候选人回答（{source}）：\n{answer.strip()}",
            f"已有问答记录：\n{_transcript(session)}",
            SHARED_RULES,
            "严格按以下顺序输出，每项独立成行：\n"
            "1. `评分：X/10`（X 是 0-10 的数字）\n"
            "2. `点评：` 亮点一句 + 缺口一句，都要引用候选人的原话\n"
            "3. `更好的说法：` 一段 30 秒可口述的改写\n"
            f"{tail}",
        ]
    )


def build_closing_prompt(session: InterviewSession) -> str:
    return "\n\n".join(
        [
            "# 模拟面试收尾复盘",
            f"面试主题：{session.topic}",
            f"完整问答记录：\n{_transcript(session, limit=MAX_PLANNED_TURNS)}",
            SHARED_RULES,
            "输出：\n"
            "1. `总评：` 一段（当前水平能过哪一轮、卡在哪）\n"
            "2. `逐题得分：` 每题一行，写清扣分原因\n"
            "3. `明天补什么：` 3 条，每条指向具体材料或具体项目动作\n"
            "4. `再练一次的重点：` 1 条",
        ]
    )


def render_session_summary(session: InterviewSession) -> str:
    """A summary that does not need the model to succeed.

    ``/mock end`` has to answer even when the closing model call fails, so the
    numbers come from the stored transcript.
    """

    average = session.average_score
    header = [
        f"# 模拟面试记录 · {session.topic}",
        f"- 已完成：{len(session.answered_turns)}/{session.planned_turns} 轮",
        f"- 平均分：{average}/10" if average is not None else "- 平均分：暂无评分",
        f"- 开始时间：{session.started_at}",
    ]
    lines = [
        f"- 第 {turn.index} 题（{'未评分' if turn.score is None else f'{turn.score}/10'}）"
        f"：{turn.question.splitlines()[0] if turn.question else '—'}"
        for turn in session.turns
    ]
    return "\n".join([*header, "", "## 逐题", *(lines or ["- 本次没有留下问答记录"])])


def session_is_stale(
    session: InterviewSession,
    *,
    now: datetime | None = None,
    idle_seconds: float = SESSION_IDLE_SECONDS,
) -> bool:
    """Has this session been sitting untouched long enough to be abandoned?"""

    try:
        updated = datetime.fromisoformat(session.updated_at)
    except ValueError:
        return True
    if updated.tzinfo is None:
        return True
    moment = now or datetime.now(timezone.utc)
    return (moment - updated).total_seconds() > idle_seconds


def _session_file_name(chat_id: str, identity: str) -> str:
    """A file name that is safe and still unique per chat.

    Feishu ids are opaque, so the sanitized form can collide; the digest keeps
    two different chats from sharing one transcript.
    """

    digest = hashlib.sha256(f"{identity}:{chat_id}".encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)[:40] or "chat"
    return f"{safe}-{digest}.json"


class InterviewSessionStore:
    """One JSON file per chat, written atomically."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path_for(self, chat_id: str, identity: str = COACH_ROLE_ID) -> Path:
        return self.directory / _session_file_name(chat_id, identity)

    def load(
        self,
        chat_id: str,
        identity: str = COACH_ROLE_ID,
    ) -> InterviewSession | None:
        path = self.path_for(chat_id, identity)
        if not path.exists():
            return None
        try:
            session = InterviewSession.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:
            # A corrupted transcript must not brick the bot; a new session is
            # always recoverable, a crash loop is not.
            return None
        if session.status == "closed":
            return None
        if session_is_stale(session):
            # Yesterday's half-finished interview must not swallow today's
            # plain question as if it were an answer.
            return None
        return session

    def save(self, session: InterviewSession) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(session.chat_id, session.identity)
        payload = session.model_dump_json(indent=2)
        handle, temp_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=".interview.",
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload + "\n")
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def close(self, session: InterviewSession) -> None:
        session.status = "closed"
        session.updated_at = now_iso()
        self.save(session)

    def list_sessions(self) -> list[InterviewSession]:
        if not self.directory.is_dir():
            return []
        sessions: list[InterviewSession] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                sessions.append(
                    InterviewSession.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                )
            except Exception:
                continue
        return sessions


@dataclass
class CoachTurn:
    """What the channel should send back for one interview move."""

    markdown: str
    #: The part worth speaking aloud — a spoken score breakdown is noise.
    speech: str = ""
    session: InterviewSession | None = None
    finished: bool = False


def coach_role(registry, *, prompt_suffix: str = "") -> RoleSpec:
    """The coach role, told to behave like a live interviewer.

    The registry prompt is written for one-shot question packs ("固定输出：知识
    题、项目深挖题、rubric…"). Kept as-is it produces a whole bank instead of a
    single question, so the live mode appends its own instruction rather than
    editing the shared role.
    """

    role = registry.get(COACH_ROLE_ID)
    suffix = prompt_suffix or (
        "\n\n现在你在进行实时模拟面试：一次只问一道题，问完必须停下等候选人回答，"
        "不要一次给出题库或参考答案。评分时先给 `评分：X/10`，再点评，再出下一题。"
    )
    return role.model_copy(
        update={"system_prompt": f"{role.system_prompt}{suffix}"}
    )


class InterviewCoach:
    """Drive a multi-turn mock interview over a one-shot message channel."""

    def __init__(
        self,
        *,
        registry,
        complete,
        store: InterviewSessionStore,
        identity: str = COACH_ROLE_ID,
    ):
        self.registry = registry
        self._complete = complete
        self.store = store
        self.identity = identity

    async def _ask_model(self, prompt: str) -> str:
        reply = await self._complete(coach_role(self.registry), prompt)
        content = getattr(reply, "content", reply)
        return str(content).strip()

    async def handle(
        self,
        chat_id: str,
        text: str,
        *,
        spoken: bool = False,
    ) -> CoachTurn | None:
        """Return the reply for one message, or ``None`` if it is not ours."""

        session = self.store.load(chat_id, self.identity)
        command = parse_interview_command(
            text,
            session_open=session is not None,
        )
        if command is None:
            return None
        if command.action == "help":
            return CoachTurn(markdown=INTERVIEW_HELP, session=session)
        if command.action == "start":
            return await self._start(chat_id, command)
        if session is None:
            return CoachTurn(
                markdown=(
                    "现在没有进行中的模拟面试。发送 "
                    "`/mock start <主题> 语音` 开始一场。"
                )
            )
        if command.action == "status":
            return CoachTurn(
                markdown=render_session_summary(session),
                session=session,
            )
        if command.action == "end":
            return await self._end(session)
        if command.action == "skip":
            return await self._skip(session)
        return await self._answer(session, command.payload, spoken=spoken)

    async def _start(
        self,
        chat_id: str,
        command: InterviewCommand,
    ) -> CoachTurn:
        previous = self.store.load(chat_id, self.identity)
        if previous is not None:
            # Starting a new topic ends the old session instead of interleaving
            # two transcripts in one chat.
            self.store.close(previous)
        session = InterviewSession(
            chat_id=chat_id,
            identity=self.identity,
            topic=command.payload,
            voice=bool(command.voice),
            planned_turns=command.planned_turns or DEFAULT_PLANNED_TURNS,
        )
        opening = await self._ask_model(build_opening_prompt(session))
        # The opening reply is a greeting plus the first question; only the
        # question belongs in the transcript, or the summary and the next
        # prompt would quote "你好，我是今天的面试官" as 第 1 题.
        session.ask(parse_coach_reply(opening).question or opening)
        self.store.save(session)
        header = (
            f"# 模拟面试开始 · {session.topic}\n"
            f"- 计划 {session.planned_turns} 轮，"
            f"{'语音 + 文字' if session.voice else '文字'}互动\n"
            "- 直接回答即可；`/mock skip` 跳过本题，`/mock end` 结束复盘\n"
        )
        return CoachTurn(
            markdown=f"{header}\n{opening}",
            speech=opening,
            session=session,
        )

    async def _answer(
        self,
        session: InterviewSession,
        answer: str,
        *,
        spoken: bool,
    ) -> CoachTurn:
        prompt = build_review_prompt(session, answer, spoken=spoken)
        session.answer(answer, spoken=spoken)
        raw = await self._ask_model(prompt)
        reply = parse_coach_reply(raw)
        turn = session.turns[-1]
        turn.score = reply.score
        turn.feedback = reply.feedback or raw
        session.updated_at = now_iso()

        if session.is_finished():
            self.store.save(session)
            closing = await self._end(session)
            return CoachTurn(
                markdown=f"{raw}\n\n---\n\n{closing.markdown}",
                speech=reply.feedback[:200] or raw[:200],
                session=session,
                finished=True,
            )

        question = reply.question
        if not question:
            # The reply drifted from the format. Rather than stall the
            # interview, ask for the next question in its own call.
            question = await self._ask_model(
                build_review_prompt(session, answer, spoken=spoken)
                + "\n\n只输出下一道题，不要重复评分。"
            )
        session.ask(question)
        self.store.save(session)
        markdown = (
            raw
            if reply.question
            else f"{raw}\n\n**第 {session.turns[-1].index} 题**：{question}"
        )
        return CoachTurn(
            markdown=markdown,
            speech=question,
            session=session,
        )

    async def _skip(self, session: InterviewSession) -> CoachTurn:
        session.answer("（跳过本题）")
        session.turns[-1].feedback = "候选人跳过本题"
        question = await self._ask_model(
            build_review_prompt(session, "候选人选择跳过这道题", spoken=False)
            + "\n\n只输出 `下一题：` 和一行 `考察点：`，不要评分。"
        )
        reply = parse_coach_reply(question)
        next_question = reply.question or question
        session.ask(next_question)
        self.store.save(session)
        return CoachTurn(
            markdown=f"已跳过第 {session.turns[-2].index} 题。\n\n{next_question}",
            speech=next_question,
            session=session,
        )

    async def _end(self, session: InterviewSession) -> CoachTurn:
        local = render_session_summary(session)
        try:
            closing = await self._ask_model(build_closing_prompt(session))
        except Exception as exc:
            closing = f"（模型复盘失败：{type(exc).__name__}）"
        self.store.close(session)
        return CoachTurn(
            markdown=f"{local}\n\n## 教练复盘\n\n{closing}",
            speech="",
            session=session,
            finished=True,
        )


INTERVIEW_HELP = """# 模拟面试（真人来回，可带语音）

- `/mock start <主题>`：开始一场模拟面试，一轮一道题
- `/mock start <主题> 语音`：额外收到教练的语音提问
- `/mock start <主题> 语音 8轮`：指定轮数（1-12）
- 直接回答：文字或语音都可以，语音会先转写再评分
- `/mock skip`：跳过当前这道题
- `/mock status`：查看已答轮数与平均分
- `/mock end`：立即结束并拿到复盘

主题建议写具体岗位，例如 `/mock start 百度 Agent应用全栈 J99974 语音`。"""
