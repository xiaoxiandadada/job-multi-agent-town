import base64

import pytest

from job_agent_harness.attachments import (
    download_images,
    sniff_media_type,
    strip_image_markdown,
    to_attachment,
)
from job_agent_harness.models import MAX_IMAGE_BASE64_CHARS


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
GIF = b"GIF89a" + b"0" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 32


class FakeResource:
    def __init__(self, file_key, file_name="", type="image"):
        self.type = type
        self.file_key = file_key
        self.file_name = file_name


class FakeMessage:
    def __init__(self, resources, content_text="", message_id="om_1"):
        self.resources = resources
        self.content_text = content_text
        self.message_id = message_id


class FakeChannel:
    """A Feishu channel that hands back whatever the test scripted."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.asked: list[str] = []

    async def download_resource(self, file_key, resource_type, message_id=None):
        self.asked.append(file_key)
        value = self.payloads[file_key]
        if isinstance(value, Exception):
            raise value
        return value


def test_media_type_comes_from_the_bytes_not_the_filename():
    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(GIF) == "image/gif"
    assert sniff_media_type(WEBP) == "image/webp"
    # HEIC from an iPhone and a renamed PDF both have to be refused here rather
    # than by an opaque provider 400.
    assert sniff_media_type(b"\x00\x00\x00\x18ftypheic") is None
    assert sniff_media_type(b"%PDF-1.7") is None
    assert sniff_media_type(b"") is None


def test_unsupported_and_oversized_images_are_rejected_with_a_reason():
    with pytest.raises(ValueError, match="PNG/JPEG/GIF/WebP"):
        to_attachment(b"%PDF-1.7 not a picture", "resume.pdf")

    # Just past the encoded ceiling: base64 grows 3 bytes into 4 characters.
    oversized = PNG + b"0" * (MAX_IMAGE_BASE64_CHARS // 4 * 3)
    with pytest.raises(ValueError, match="图片太大"):
        to_attachment(oversized, "huge.png")


def test_supported_image_round_trips_into_base64():
    attachment = to_attachment(JPEG, "jd.jpg")

    assert attachment.media_type == "image/jpeg"
    assert base64.b64decode(attachment.data) == JPEG
    assert attachment.source_name == "jd.jpg"


def test_flattened_image_markdown_never_reaches_the_model():
    # This is the original defect: lark_oapi turns a picture into a fake link
    # inside content_text, and the controller used to send that as the task.
    assert strip_image_markdown("![image](img_v3_abc) 帮我看这个 JD") == "帮我看这个 JD"
    assert strip_image_markdown("[image]") == ""
    assert strip_image_markdown("看这两张 ![image](a) ![image](b)") == "看这两张"
    assert strip_image_markdown("正常文字不动") == "正常文字不动"


async def test_download_images_reads_every_attached_picture():
    channel = FakeChannel({"k1": PNG, "k2": JPEG})
    message = FakeMessage(
        [FakeResource("k1", "jd.png"), FakeResource("k2", "resume.jpg")]
    )

    images, problems = await download_images(channel, message)

    assert problems == []
    assert [image.media_type for image in images] == ["image/png", "image/jpeg"]
    assert [image.source_name for image in images] == ["jd.png", "resume.jpg"]
    assert channel.asked == ["k1", "k2"]


async def test_one_broken_picture_does_not_cost_the_user_the_others():
    channel = FakeChannel(
        {
            "ok": PNG,
            "boom": RuntimeError("403"),
            "empty": b"",
            "heic": b"\x00\x00\x00\x18ftypheic",
        }
    )
    message = FakeMessage(
        [
            FakeResource("ok", "good.png"),
            FakeResource("boom", "gone.png"),
            FakeResource("empty", "blank.png"),
            FakeResource("heic", "phone.heic"),
            FakeResource("", "nokey.png"),
        ]
    )

    images, problems = await download_images(channel, message)

    assert [image.source_name for image in images] == ["good.png"]
    assert len(problems) == 4
    assert "gone.png：下载失败（403）" in problems
    assert "blank.png：下载结果为空" in problems
    assert any("phone.heic" in problem for problem in problems)
    assert "nokey.png：缺少 file_key" in problems


async def test_batch_over_the_cap_is_truncated_out_loud():
    keys = [f"k{index}" for index in range(11)]
    channel = FakeChannel({key: PNG for key in keys})
    message = FakeMessage([FakeResource(key) for key in keys])

    images, problems = await download_images(channel, message, max_items=9)

    assert len(images) == 9
    assert problems == ["只读前 9 张，其余 2 张已跳过"]


async def test_non_image_resources_are_ignored():
    channel = FakeChannel({"k1": PNG})
    message = FakeMessage(
        [FakeResource("audio1", type="audio"), FakeResource("k1", "jd.png")]
    )

    images, _ = await download_images(channel, message)

    assert [image.source_name for image in images] == ["jd.png"]
    assert channel.asked == ["k1"]
