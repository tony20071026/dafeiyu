"""Helpers for reading remote media without relying on chunk boundaries."""

from __future__ import annotations

from typing import Any

# ``StreamReader.read(n)`` may return only the first buffered chunk.  Iterating
# explicitly is required to get the whole body (QQ often delivers images in
# 16 KiB / 32 KiB chunks, which previously looked like truncated files).
_CHUNK_SIZE = 64 * 1024


async def read_limited(
    response: Any, max_bytes: int, chunk_size: int = _CHUNK_SIZE
) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` bytes.  Returns ``(data, exceeded)``."""

    data = bytearray()
    async for chunk in response.content.iter_chunked(chunk_size):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            return bytes(data[:max_bytes]), True
    return bytes(data), False