"""Per-conversation character-window context for every incoming message.

The bot currently has no external LLM, but every event is still trimmed into a
bounded context block and handed to the message handler.  The same component can
therefore be reused unchanged when an AI backend is added.

Supported window sizes are deliberately limited to 5,000 / 10,000 / 50,000
characters (``5000`` / ``1w`` / ``5w``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiofiles

from config import CONTEXT_WINDOW_OPTIONS, DEFAULT_CONTEXT_WINDOW

LOGGER = logging.getLogger(__name__)

_CONTEXT_ALIASES: dict[str, int] = {
    "5000": 5_000,
    "5000字": 5_000,
    "5k": 5_000,
    "1w": 10_000,
    "1万": 10_000,
    "10000": 10_000,
    "10k": 10_000,
    "5w": 50_000,
    "5万": 50_000,
    "50000": 50_000,
    "50k": 50_000,
}


def normalize_context_size(value: str | int) -> int | None:
    """Parse a user/ENV supplied window size, returning None if unsupported."""

    if isinstance(value, int):
        return value if value in CONTEXT_WINDOW_OPTIONS else None
    raw = str(value).strip().lower().replace(",", "").replace(" ", "")
    return _CONTEXT_ALIASES.get(raw)


def context_size_label(size: int) -> str:
    if size == 10_000:
        return "1w（10,000 字）"
    if size == 50_000:
        return "5w（50,000 字）"
    return f"{size:,} 字"


def context_options_label() -> str:
    return " / ".join(context_size_label(size) for size in CONTEXT_WINDOW_OPTIONS)


@dataclass(slots=True)
class ContextEntry:
    role: str
    author: str
    content: str
    created_at: datetime

    def render(self) -> str:
        stamp = self.created_at.strftime("%H:%M")
        return f"[{stamp}] {self.author}: {self.content}"


class ConversationBuffer:
    __slots__ = ("limit", "_entries", "_chars")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._entries: deque[ContextEntry] = deque()
        self._chars = 0

    def append(self, entry: ContextEntry) -> None:
        line = entry.render()
        if len(line) > self.limit:
            # Keep the newest part of an oversized line instead of losing all of
            # it.  This is important for pasted logs or long code snippets.
            overflow = len(line) - self.limit
            entry.content = entry.content[overflow:]
            line = entry.render()
        if self._entries:
            self._chars += 1  # newline separator
        self._entries.append(entry)
        self._chars += len(line)
        self._trim()

    def _trim(self) -> None:
        while self._entries and self._chars > self.limit:
            old = self._entries.popleft()
            # Removing the first line also removes the newline that separated it
            # from the new first line.
            self._chars -= len(old.render()) + (1 if self._entries else 0)

    def render(self) -> str:
        return "\n".join(entry.render() for entry in self._entries)

    def set_limit(self, limit: int) -> None:
        self.limit = limit
        self._trim()

    @property
    def chars(self) -> int:
        return self._chars

    @property
    def messages(self) -> int:
        return len(self._entries)


class ContextWindowManager:
    """Maintains in-memory history and persists only each chat's window size."""

    def __init__(
        self,
        state_file: Path,
        default_size: int = DEFAULT_CONTEXT_WINDOW,
        options: tuple[int, ...] = CONTEXT_WINDOW_OPTIONS,
    ) -> None:
        if default_size not in options:
            default_size = DEFAULT_CONTEXT_WINDOW
        self._state_file = state_file
        self._default_size = default_size
        self._options = options
        self._buffers: dict[str, ConversationBuffer] = {}
        self._limits: dict[str, int] = {}
        self._save_lock = asyncio.Lock()

    async def load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            async with aiofiles.open(self._state_file, "r", encoding="utf-8") as fh:
                raw = await fh.read()
            data = json.loads(raw or "{}")
            for conversation_id, value in data.get("limits", {}).items():
                size = normalize_context_size(value)
                if size in self._options:
                    self._limits[str(conversation_id)] = size
            LOGGER.info("已载入 %s 个会话的上下文窗口设置", len(self._limits))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("读取上下文窗口配置失败，使用默认值")

    async def save(self) -> None:
        async with self._save_lock:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self._state_file.with_suffix(".tmp")
            payload = json.dumps(
                {"version": 1, "limits": self._limits},
                ensure_ascii=False,
                indent=2,
            )
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as fh:
                await fh.write(payload)
                await fh.write("\n")
            os.replace(temp_file, self._state_file)

    def limit_for(self, conversation_id: str) -> int:
        return self._limits.get(conversation_id, self._default_size)

    def _buffer_for(self, conversation_id: str) -> ConversationBuffer:
        buffer = self._buffers.get(conversation_id)
        if buffer is None:
            buffer = ConversationBuffer(self.limit_for(conversation_id))
            self._buffers[conversation_id] = buffer
        return buffer

    def append(
        self,
        conversation_id: str,
        *,
        author: str,
        content: str,
        role: str = "user",
        created_at: datetime | None = None,
    ) -> None:
        content = (content or "").strip()
        if not content:
            return
        entry = ContextEntry(
            role=role,
            author=author or "未知用户",
            content=content,
            created_at=created_at or datetime.now(),
        )
        self._buffer_for(conversation_id).append(entry)

    def build(self, conversation_id: str) -> str:
        """Return the exact block passed to the handler for this event."""

        return self._buffer_for(conversation_id).render()

    def stats(self, conversation_id: str) -> tuple[int, int, int]:
        buffer = self._buffer_for(conversation_id)
        return buffer.limit, buffer.chars, buffer.messages

    async def set_limit(self, conversation_id: str, size: int) -> tuple[int, int]:
        if size not in self._options:
            raise ValueError(f"unsupported context size: {size}")
        old = self.limit_for(conversation_id)
        self._limits[conversation_id] = size
        self._buffer_for(conversation_id).set_limit(size)
        await self.save()
        return old, size

    def forget(self, conversation_id: str) -> None:
        self._buffers.pop(conversation_id, None)
