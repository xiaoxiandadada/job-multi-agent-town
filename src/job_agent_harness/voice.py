from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

#: macOS ships a Mandarin voice; `say -v '?'` lists it as `Tingting  zh_CN`.
DEFAULT_TTS_VOICE = "Tingting"
#: Feishu voice messages accept opus only, and only these encoders are realistic
#: on a laptop. Order is preference order.
OPUS_ENCODERS = ("ffmpeg", "opusenc")
#: Nobody listens to a five-minute robot monologue; the written reply carries
#: the detail, the voice carries the question.
MAX_SPEECH_CHARS = 600
#: Feishu file-recognize takes one chunk of audio, and the engine expects
#: 16 kHz mono for Mandarin.
STT_ENGINE_TYPE = "16k_auto"
#: `say` writes AIFF-C "twos" by default, which `opusenc` rejects outright, so
#: the intermediate file is plain little-endian 16-bit PCM WAV instead.
SAY_SAMPLE_RATE = 22050


@dataclass(frozen=True)
class SpeechClip:
    """A synthesized voice clip ready to be sent to Feishu."""

    path: Path
    duration_ms: int | None = None


def speakable_text(markdown: str, *, limit: int = MAX_SPEECH_CHARS) -> str:
    """Turn a Markdown reply into something worth hearing.

    Read aloud, backticks and hash marks become noise ("井号井号 第一题"), and a
    code block becomes unlistenable. Links are read as their label, because the
    URL is only useful on screen.
    """

    text = markdown.strip()
    text = re.sub(r"```.*?```", "，（详见文字版代码块），", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"https?://\S+", "链接见文字版", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) <= limit:
        return text
    # Cut on a sentence boundary so the clip does not stop mid-word.
    head = text[:limit]
    for mark in ("。", "？", "!", "！", "\n", "，"):
        cut = head.rfind(mark)
        if cut > limit // 2:
            return head[: cut + 1]
    return head


def find_opus_encoder(which=shutil.which) -> str | None:
    """The first available encoder that can produce Feishu-compatible opus."""

    for name in OPUS_ENCODERS:
        if which(name):
            return name
    return None


def encoder_command(encoder: str, source: Path, target: Path) -> list[str]:
    if encoder == "ffmpeg":
        return [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            str(target),
        ]
    if encoder == "opusenc":
        return [
            "opusenc",
            "--quiet",
            "--downmix-mono",
            "--bitrate",
            "24",
            str(source),
            str(target),
        ]
    raise ValueError(f"unknown opus encoder: {encoder}")


VOICE_SETUP_HINT = (
    "语音回复需要一个 opus 编码器：`brew install ffmpeg`（或 `brew install "
    "opus-tools`）之后重启机器人即可听到教练的声音；现在先用文字版。"
)
#: Uploading the clip is a separate Feishu permission from sending text, so a
#: bot can answer perfectly in text and still fail on every voice message.
VOICE_UPLOAD_HINT = (
    "语音合成成功但上传失败：这个机器人还需要飞书 `im:resource:upload` 权限"
    "（或 `im:resource`），开通并重新发布版本后就能收到语音；现在先用文字版。"
)


def synthesize_speech(
    text: str,
    directory: Path,
    *,
    voice: str | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> SpeechClip | None:
    """Speak ``text`` into an opus clip, or return ``None`` if we cannot.

    Returning ``None`` instead of raising is deliberate: a missing encoder must
    degrade the reply to text, never drop the interview turn.
    """

    body = speakable_text(text)
    if not body:
        return None
    if not which("say"):
        logger.info("voice reply skipped: no `say` binary on this platform")
        return None
    encoder = find_opus_encoder(which)
    if encoder is None:
        logger.info("voice reply skipped: no opus encoder (%s)", VOICE_SETUP_HINT)
        return None

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex[:12]
    raw = directory / f"coach-{stem}.wav"
    target = directory / f"coach-{stem}.opus"
    try:
        run(
            [
                "say",
                "-v",
                voice or os.getenv("JOB_AGENT_TTS_VOICE", DEFAULT_TTS_VOICE),
                # `say` defaults to AIFF-C "twos", which opusenc refuses with
                # "Can't handle compressed AIFF-C". Ask for plain PCM WAV.
                "--file-format=WAVE",
                f"--data-format=LEI16@{SAY_SAMPLE_RATE}",
                "-o",
                str(raw),
                body,
            ],
            check=True,
            capture_output=True,
        )
        run(
            encoder_command(encoder, raw, target),
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        logger.warning("voice synthesis failed: %s", type(exc).__name__)
        return None
    finally:
        if raw.exists():
            raw.unlink()
    if not target.exists():
        return None
    return SpeechClip(path=target)


async def synthesize_speech_async(
    text: str,
    directory: Path,
    *,
    voice: str | None = None,
) -> SpeechClip | None:
    """``synthesize_speech`` off the event loop.

    ``say`` takes about as long as the clip itself, which would otherwise block
    every other Feishu callback in this process.
    """

    return await asyncio.to_thread(
        synthesize_speech,
        text,
        directory,
        voice=voice,
    )


class TranscriptionUnavailable(RuntimeError):
    """Feishu could not turn this clip into text."""


def _speech_payload(audio: bytes) -> dict:
    return {
        "speech": {"speech": base64.b64encode(audio).decode("ascii")},
        "config": {
            "file_id": uuid.uuid4().hex[:16],
            "format": "opus",
            "engine_type": STT_ENGINE_TYPE,
        },
    }


def parse_recognition_response(payload: dict) -> str:
    """Read the recognized text out of a file-recognize response."""

    if payload.get("code") not in (0, None):
        raise TranscriptionUnavailable(
            f"飞书语音识别返回 code={payload.get('code')} "
            f"msg={payload.get('msg', '')}"
        )
    data = payload.get("data") or {}
    text = (data.get("recognition_text") or "").strip()
    if not text:
        raise TranscriptionUnavailable("飞书语音识别没有返回文本")
    return text


async def transcribe_opus(
    audio: bytes,
    *,
    app_id: str,
    app_secret: str,
    post=None,
) -> str:
    """Transcribe a Feishu voice message with Feishu's own STT.

    The audio already lives inside Feishu and the bot already holds Feishu
    credentials, so this needs no extra vendor: it only needs the
    ``speech_to_text:speech`` scope on the app.
    """

    if not audio:
        raise TranscriptionUnavailable("语音内容为空")
    sender = post or _post_file_recognize
    payload = await sender(
        _speech_payload(audio),
        app_id=app_id,
        app_secret=app_secret,
    )
    return parse_recognition_response(payload)


async def _post_file_recognize(
    body: dict,
    *,
    app_id: str,
    app_secret: str,
) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        token = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        token.raise_for_status()
        tenant_token = token.json().get("tenant_access_token", "")
        if not tenant_token:
            raise TranscriptionUnavailable("获取 tenant_access_token 失败")
        response = await client.post(
            "https://open.feishu.cn/open-apis/speech_to_text/v1/speech/file_recognize",
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        response.raise_for_status()
        return response.json()


def audio_resource(message) -> object | None:
    """The voice clip attached to an inbound message, if there is one."""

    for resource in getattr(message, "resources", ()) or ():
        if getattr(resource, "type", "") == "audio":
            return resource
    content = getattr(message, "content", None)
    if getattr(content, "kind", "") == "audio":
        return content
    return None
