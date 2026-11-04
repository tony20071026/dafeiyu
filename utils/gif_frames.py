"""Sample a few animation frames so vision models can read GIF memes."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_FRAME_COUNT = 4
MAX_FRAME_SIDE = 640
_JPEG_QUALITY = 82


def even_frame_indices(total: int, count: int) -> list[int]:
    """Pick ``count`` indices spread evenly from ``0`` to ``total - 1``."""

    if total <= 0 or count <= 0:
        return []
    if total <= count:
        return list(range(total))
    if count == 1:
        return [total // 2]
    return sorted(
        {round(index * (total - 1) / (count - 1)) for index in range(count)}
    )


def _encode_frame(frame: Any, max_side: int) -> bytes:
    from PIL import Image

    rgba = frame.convert("RGBA")
    # GIF transparency becomes black when flattened directly, so put every
    # frame on a white canvas before JPEG encoding.
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    background.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    background.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _sample_image(
    image: Any,
    count: int,
    max_side: int,
    label: str,
) -> list[tuple[bytes, str]]:
    if getattr(image, "format", "").upper() != "GIF":
        return []
    total = int(getattr(image, "n_frames", 1) or 1)
    wanted = set(even_frame_indices(total, count))
    from PIL import ImageSequence

    frames: list[tuple[bytes, str]] = []
    seen: set[bytes] = set()
    for index, frame in enumerate(ImageSequence.Iterator(image)):
        if index not in wanted:
            continue
        data = _encode_frame(frame, max_side)
        if data and data not in seen:
            seen.add(data)
            frames.append((data, "image/jpeg"))

    LOGGER.info("GIF 已抽取 %s/%s 帧用于识别 | %s", len(frames), total, label)
    return frames


def sample_gif_frames(
    path: Path,
    count: int = DEFAULT_FRAME_COUNT,
    max_side: int = MAX_FRAME_SIDE,
) -> list[tuple[bytes, str]]:
    """Return up to ``count`` evenly spaced frames as ``(jpeg_bytes, mime)``.

    Frames are flattened onto white, downscaled, and saved as JPEG so a
    four-frame request stays small enough for a chat-completions payload.
    """

    try:
        from PIL import Image
    except ImportError:
        LOGGER.warning("未安装 Pillow，无法抽取 GIF 帧")
        return []

    try:
        with Image.open(path) as image:
            return _sample_image(image, count, max_side, path.name)
    except Exception as exc:
        LOGGER.warning("GIF 取帧失败 %s: %s", path.name, exc)
        return []


def sample_gif_frames_bytes(
    data: bytes,
    count: int = DEFAULT_FRAME_COUNT,
    max_side: int = MAX_FRAME_SIDE,
    label: str = "remote.gif",
) -> list[tuple[bytes, str]]:
    """Same as :func:`sample_gif_frames` but for downloaded image bytes."""

    try:
        from PIL import Image
    except ImportError:
        LOGGER.warning("未安装 Pillow，无法抽取 GIF 帧")
        return []

    try:
        with Image.open(io.BytesIO(data)) as image:
            return _sample_image(image, count, max_side, label)
    except Exception as exc:
        LOGGER.warning("GIF 取帧失败 %s: %s", label, exc)
        return []