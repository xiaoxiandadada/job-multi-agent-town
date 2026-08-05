"""Turn inbound Feishu media into the ``ImageAttachment`` the model can read.

Two things make this its own module rather than a few lines in the channel:

* **The fake link.** ``lark_oapi`` flattens an image into ``![image](image_key)``
  inside ``content_text``. Before this module the controller happily fed that
  markdown to the model as the task, so an @mention with a screenshot produced
  an answer about a link that does not exist. ``strip_image_markdown`` removes
  it; ``download_images`` supplies the actual pixels.
* **The media type.** Feishu does not tell us whether a ``file_key`` is a PNG or a
  JPEG, and Claude rejects anything outside four formats. Sniffing the magic
  bytes is the only honest answer — a filename extension is user-controlled and
  is wrong often enough to matter.
"""

from __future__ import annotations

import base64
import re

from .models import MAX_IMAGE_BASE64_CHARS, ImageAttachment, ImageMediaType


#: One Feishu media batch is 9 items, and Claude's own guidance is to stay well
#: under 20 images per request. 9 keeps the two limits aligned.
MAX_IMAGES_PER_MESSAGE = 9

#: ``![image](img_v3_xxx)`` and the ``[image]`` fallback the converter emits
#: when a key is missing. Also covers ``<image ...>``-shaped placeholders from
#: other media converters so a post with a picture does not leave debris.
IMAGE_MARKDOWN = re.compile(r"!\[image\]\([^)]*\)|\[image\]")


def strip_image_markdown(text: str) -> str:
    """Drop flattened media placeholders from a message's text.

    The pixels arrive separately as ``ImageAttachment``s, so leaving the
    placeholder in would hand the model a second, fictional source.
    """

    return " ".join(IMAGE_MARKDOWN.sub(" ", text).split()).strip()


def sniff_media_type(data: bytes) -> ImageMediaType | None:
    """The image format, read from the bytes themselves.

    Returns ``None`` for anything Claude cannot accept (HEIC from an iPhone,
    BMP, TIFF, a PDF someone renamed) so the caller can say which file was
    skipped instead of letting the provider return an opaque 400.
    """

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def to_attachment(data: bytes, source_name: str = "") -> ImageAttachment:
    """Encode raw bytes, or raise ``ValueError`` with a user-readable reason."""

    media_type = sniff_media_type(data)
    if media_type is None:
        raise ValueError("不是 PNG/JPEG/GIF/WebP，模型看不了")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        raise ValueError(
            f"图片太大（{len(data) / 1_048_576:.1f} MB），请压到 5 MB 以内"
        )
    return ImageAttachment(
        media_type=media_type,
        data=encoded,
        source_name=source_name[:200],
    )


def image_resources(message) -> list[object]:
    """Every picture attached to an inbound message.

    Mirrors ``voice.audio_resource``: prefer the ``resources`` list, and fall
    back to a bare ``ImageContent`` for a single-image message.
    """

    found = [
        resource
        for resource in getattr(message, "resources", ()) or ()
        if getattr(resource, "type", "") == "image"
    ]
    if found:
        return found
    content = getattr(message, "content", None)
    if getattr(content, "kind", "") == "image" and getattr(
        content,
        "image_key",
        "",
    ):
        return [content]
    return []


def resource_key(resource) -> str:
    return getattr(resource, "file_key", "") or getattr(
        resource,
        "image_key",
        "",
    )


async def download_images(
    channel,
    message,
    *,
    max_items: int = MAX_IMAGES_PER_MESSAGE,
) -> tuple[list[ImageAttachment], list[str]]:
    """Fetch every attached picture. Returns ``(attachments, problems)``.

    Never raises: one unreadable screenshot in a batch of four must not cost the
    user the other three, and the caller needs the ``problems`` list to say out
    loud what was skipped rather than pretending it read everything.
    """

    resources = image_resources(message)
    attachments: list[ImageAttachment] = []
    problems: list[str] = []
    if len(resources) > max_items:
        problems.append(
            f"只读前 {max_items} 张，其余 {len(resources) - max_items} 张已跳过"
        )
        resources = resources[:max_items]

    for index, resource in enumerate(resources, start=1):
        file_key = resource_key(resource)
        label = getattr(resource, "file_name", "") or f"图片{index}"
        if not file_key:
            problems.append(f"{label}：缺少 file_key")
            continue
        try:
            data = await channel.download_resource(
                file_key,
                "image",
                message_id=getattr(message, "message_id", None),
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            problems.append(f"{label}：下载失败（{exc}）")
            continue
        if not data:
            problems.append(f"{label}：下载结果为空")
            continue
        try:
            attachments.append(to_attachment(data, label))
        except ValueError as exc:
            problems.append(f"{label}：{exc}")
    return attachments, problems
