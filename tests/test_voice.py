import pytest

from job_agent_harness.voice import (
    TranscriptionUnavailable,
    audio_resource,
    encoder_command,
    find_opus_encoder,
    parse_recognition_response,
    speakable_text,
    synthesize_speech,
    transcribe_opus,
)


def test_speakable_text_drops_markup_that_reads_as_noise():
    spoken = speakable_text(
        "## 第 2 题\n"
        "- 你的 `golden set` 有多少条？\n"
        "- 参考 [百度 JD](https://talent.baidu.com/jobs/detail/x)\n"
    )

    assert "#" not in spoken
    assert "`" not in spoken
    assert "https://" not in spoken
    assert "第 2 题" in spoken
    assert "百度 JD" in spoken


def test_a_code_block_is_replaced_rather_than_read_out_loud():
    spoken = speakable_text("先看结论。\n```python\nfor i in range(10):\n    pass\n```")

    assert "for i in range" not in spoken
    assert "详见文字版代码块" in spoken


def test_a_long_reply_is_cut_on_a_sentence_boundary():
    body = "".join(f"这是第{index}句话。" for index in range(100))

    spoken = speakable_text(body, limit=60)

    assert len(spoken) <= 60
    assert spoken.endswith("。")


def test_the_preferred_encoder_wins_when_both_exist():
    assert find_opus_encoder(which=lambda name: f"/usr/bin/{name}") == "ffmpeg"
    assert (
        find_opus_encoder(which=lambda name: f"/x/{name}" if name == "opusenc" else None)
        == "opusenc"
    )
    assert find_opus_encoder(which=lambda _name: None) is None


def test_encoder_commands_produce_mono_16k_opus(tmp_path):
    source = tmp_path / "in.aiff"
    target = tmp_path / "out.opus"

    ffmpeg = encoder_command("ffmpeg", source, target)
    opusenc = encoder_command("opusenc", source, target)

    assert ffmpeg[:2] == ["ffmpeg", "-y"]
    assert "libopus" in ffmpeg
    assert ["-ar", "16000"] == ffmpeg[ffmpeg.index("-ar") : ffmpeg.index("-ar") + 2]
    assert str(target) == ffmpeg[-1] == opusenc[-1]
    assert "--downmix-mono" in opusenc


def test_synthesis_degrades_to_text_when_no_encoder_is_installed(tmp_path):
    """A missing encoder must lose the audio, never the interview turn."""

    calls = []

    clip = synthesize_speech(
        "第 1 题：讲一个项目。",
        tmp_path,
        which=lambda name: "/usr/bin/say" if name == "say" else None,
        run=lambda *args, **kwargs: calls.append(args),
    )

    assert clip is None
    assert calls == []


def test_synthesis_runs_say_then_the_encoder(tmp_path):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            # ffmpeg's own output file is what the caller sends to Feishu.
            open(command[-1], "wb").write(b"opus")
        return None

    clip = synthesize_speech(
        "## 第 1 题：讲一个项目。",
        tmp_path,
        voice="Tingting",
        which=lambda name: f"/usr/bin/{name}",
        run=fake_run,
    )

    assert clip is not None
    assert clip.path.suffix == ".opus"
    assert commands[0][:3] == ["say", "-v", "Tingting"]
    # The spoken text is the cleaned version, not raw Markdown.
    assert commands[0][-1] == "第 1 题：讲一个项目。"
    assert commands[1][0] == "ffmpeg"
    # The intermediate file is not left behind in the runtime dir.
    assert list(tmp_path.glob("*.wav")) == []
    assert list(tmp_path.glob("*.aiff")) == []


def test_say_writes_plain_wav_because_opusenc_rejects_aiff_c(tmp_path):
    """`say -o x.aiff` is AIFF-C "twos": opusenc fails with "compressed AIFF-C"."""

    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[0] == "opusenc":
            open(command[-1], "wb").write(b"opus")
        return None

    clip = synthesize_speech(
        "第 1 题",
        tmp_path,
        which=lambda name: f"/x/{name}" if name in {"say", "opusenc"} else None,
        run=fake_run,
    )

    assert clip is not None
    say = commands[0]
    assert "--file-format=WAVE" in say
    assert any(arg.startswith("--data-format=LEI16@") for arg in say)
    # And the encoder is handed that same wav file.
    assert say[say.index("-o") + 1].endswith(".wav")
    assert commands[1][-2] == say[say.index("-o") + 1]


def test_a_failed_encode_returns_none_instead_of_raising(tmp_path):
    def fake_run(command, **_kwargs):
        if command[0] == "ffmpeg":
            raise RuntimeError("libopus missing")
        return None

    assert (
        synthesize_speech(
            "第 1 题",
            tmp_path,
            which=lambda name: f"/usr/bin/{name}",
            run=fake_run,
        )
        is None
    )


def test_recognition_response_is_read_or_rejected():
    assert (
        parse_recognition_response(
            {"code": 0, "data": {"recognition_text": "我做了 JD Radar"}}
        )
        == "我做了 JD Radar"
    )
    with pytest.raises(TranscriptionUnavailable, match="99991672"):
        parse_recognition_response({"code": 99991672, "msg": "no permission"})
    with pytest.raises(TranscriptionUnavailable, match="没有返回文本"):
        parse_recognition_response({"code": 0, "data": {"recognition_text": " "}})


@pytest.mark.asyncio
async def test_transcription_sends_base64_opus_with_the_mandarin_engine():
    captured = {}

    async def fake_post(body, *, app_id, app_secret):
        captured.update(body=body, app_id=app_id)
        return {"code": 0, "data": {"recognition_text": "我用了 golden set"}}

    text = await transcribe_opus(
        b"fake-opus-bytes",
        app_id="cli_coach",
        app_secret="secret",
        post=fake_post,
    )

    assert text == "我用了 golden set"
    assert captured["app_id"] == "cli_coach"
    assert captured["body"]["config"]["format"] == "opus"
    assert captured["body"]["config"]["engine_type"] == "16k_auto"
    assert captured["body"]["speech"]["speech"] == "ZmFrZS1vcHVzLWJ5dGVz"


@pytest.mark.asyncio
async def test_an_empty_clip_is_rejected_before_calling_feishu():
    async def fail(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("should not call Feishu for an empty clip")

    with pytest.raises(TranscriptionUnavailable):
        await transcribe_opus(b"", app_id="a", app_secret="b", post=fail)


def test_audio_resource_is_found_through_resources_or_content():
    from types import SimpleNamespace

    by_resource = SimpleNamespace(
        resources=[
            SimpleNamespace(type="image", file_key="img"),
            SimpleNamespace(type="audio", file_key="aud"),
        ],
        content=SimpleNamespace(kind="audio"),
    )
    by_content = SimpleNamespace(
        resources=[],
        content=SimpleNamespace(kind="audio", file_key="aud2"),
    )
    text_only = SimpleNamespace(resources=[], content=SimpleNamespace(kind="text"))

    assert audio_resource(by_resource).file_key == "aud"
    assert audio_resource(by_content).file_key == "aud2"
    assert audio_resource(text_only) is None
