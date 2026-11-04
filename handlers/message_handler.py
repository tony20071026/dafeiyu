"""Message routing for group and channel @-mentions."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from config import Settings
from utils.ai import AIClient, AIError
from utils.api import (
    CHANNEL_IMAGE_SUFFIXES,
    GROUP_IMAGE_SUFFIXES,
    ImageAssetError,
    QQApiClient,
    QQApiError,
)
from utils.context_window import (
    ContextWindowManager,
    context_options_label,
    context_size_label,
    normalize_context_size,
)
from utils.memes import MemeLibrary
from utils.text import is_http_url, truncate_text
from utils.web_fetch import WebFetcher, looks_like_image_url, render_pages

LOGGER = logging.getLogger(__name__)

Scene = Literal["group", "channel", "c2c"]

_MEME_SUFFIXES = GROUP_IMAGE_SUFFIXES | CHANNEL_IMAGE_SUFFIXES
_MEME_KEYWORDS = ("表情包", "meme", "斗图", "来张图", "发张图", "来张表情")
_MEME_REQUEST_VERBS = (
    "发",
    "来",
    "给",
    "整",
    "求",
    "要",
    "有没有",
    "斗图",
    "send",
    "give",
    "show",
)
_MEME_QUESTION_MARKERS = (
    "什么意思",
    "啥意思",
    "是什么",
    "什么梗",
    "什么鬼",
    "看懂",
    "认识",
    "描述",
    "解释",
    "出处",
    "what",
    "meaning",
    "explain",
)
_MEME_SAVE_PREFIXES = ("保存", "添加", "存为", "命名", "收藏")

_MUTE_SECONDS = 600.0
_MUTE_KEYWORDS = ("闭嘴", "别说话", "安静点", "安静一下", "消停点")
_UNMUTE_KEYWORDS = (
    "允许说话",
    "可以说话",
    "解除闭嘴",
    "别闭嘴",
    "开口说话",
    "说话吧",
)

_WATER_MESSAGES = (
    "冒个泡，证明大肥鱼还在鱼缸里。",
    "水一下，别让这个群沉底了。",
    "咕噜咕噜……大肥鱼出来换口气。",
    "好久没见，大肥鱼来巡个场。",
    "群友们还在吗？大肥鱼在线等聊天。",
)

_IMAGE_FOLLOWUP_KEYWORDS = (
    "图片",
    "照片",
    "截图",
    "画像",
    "这张图",
    "那张图",
    "上图",
    "下图",
    "图中",
    "图里",
    "看图",
    "刚才的图",
    "刚发的图",
    "表情包",
    "这个表情",
    "这张表情",
    "那个表情",
    "刚才的表情",
    "刚发的表情",
    "贴图",
    "梗图",
    "meme",
)
_IMAGE_FOLLOWUP_TTL = 600.0
_IMAGE_HISTORY_LIMIT = 6
_QUOTE_CACHE_LIMIT = 80
_QUOTE_CONTENT_LIMIT = 1_500

_MEME_MARKER_RE = re.compile(
    r"[\[【]\s*MEME\s*[:：]\s*([^\]】\n]+?)\s*[\]】]", re.IGNORECASE
)

_KEYWORD_REPLIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("你好", "哈喽", "hello", "hi"),
        (
            "你好呀！我是大肥鱼，一条住在服务器里的鱼。发 /help 看看我会什么～",
            "嗨！大肥鱼浮出水面了。今天也要开心摸鱼哦。",
        ),
    ),
    (
        ("在吗", "在不在"),
        (
            "在的在的，大肥鱼一直潜伏在水里，有事直接 @ 我。",
            "在！只要服务器不停电，大肥鱼就不掉线。",
        ),
    ),
    (
        ("摸鱼", "摸会儿鱼"),
        (
            "摸鱼中，勿扰。大肥鱼正在认真地假装工作。",
            "检测到摸鱼信号！本鱼宣布：现在进入合法摸鱼时间。",
            "工位是岸，我是鱼，摸鱼才是正经事。",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    scene: Scene
    message_id: str
    target_id: str
    conversation_id: str
    content: str
    sender_id: str
    sender_name: str
    timestamp: str = ""
    mentioned: bool = False
    image_urls: tuple[str, ...] = ()
    message_index: str = ""
    quoted_index: str = ""
    quoted_author: str = ""
    quoted_content: str = ""
    quoted_image_urls: tuple[str, ...] = ()


class MessageHandler:
    def __init__(
        self,
        settings: Settings,
        api: QQApiClient,
        contexts: ContextWindowManager,
        ai_client: AIClient | None = None,
        meme_library: MemeLibrary | None = None,
        web_fetcher: WebFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._api = api
        self._contexts = contexts
        self._ai = ai_client
        self._meme_library = meme_library
        self._web_fetcher = web_fetcher
        self._last_group_reply: dict[str, float] = {}
        self._last_meme_sent: dict[str, float] = {}
        self._group_silence: dict[str, int] = {}
        self._mute_until: dict[str, float] = {}
        self._group_targets: dict[str, str] = {}
        self._group_last_spoke: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        # conversation -> (monotonic time, sender, image urls)
        self._recent_images: dict[
            str, deque[tuple[float, str, tuple[str, ...]]]
        ] = {}
        # conversation -> (message_index, message_id, author, content, images)
        self._quote_cache: dict[
            str, deque[tuple[str, str, str, str, tuple[str, ...]]]
        ] = {}

    async def handle(self, message: IncomingMessage) -> None:
        if not (
            message.content
            or message.image_urls
            or message.quoted_content
            or message.quoted_image_urls
            or message.quoted_index
        ):
            LOGGER.debug("忽略空文本消息 %s", message.message_id)
            return

        if message.scene == "group":
            self._remember_group_target(message)

        if await self._handle_mute_state(message):
            return

        self._remember_recent_images(message)
        self._remember_quote_source(message)
        quote_author, quote_text, quote_images = self._resolve_quote(message)
        if quote_images and not message.image_urls:
            message = replace(message, quoted_image_urls=quote_images)
        self._queue_meme_collection(message)
        created_at = _parse_event_time(message.timestamp)
        stored_content = message.content
        if message.image_urls:
            stored_content = (
                f"{stored_content} [图片]" if stored_content else "[图片]"
            )
        self._contexts.append(
            message.conversation_id,
            author=message.sender_name,
            content=stored_content,
            created_at=created_at,
        )
        context_text = self._contexts.build(message.conversation_id)
        if quote_text:
            quoted_author = quote_author or "某位群友"
            quoted = f"{quoted_author}: {quote_text}"
            # Keep the quote directly under the current message.  A separate
            # block after a blank line makes the model treat the previous chat
            # line as “这句话”, which is exactly the bug this fixes.
            context_text = (
                f"{context_text}\n"
                f"（{message.sender_name} 引用的是 {quoted_author} 的这条消息："
                f"「{quote_text}」）"
            )
            LOGGER.info(
                "本条消息引用了一条内容 | %s | %s",
                message.conversation_id,
                quoted[:100].replace("\n", " "),
            )
        elif message.quoted_index:
            # The client told us this is a quote reply but did not include the
            # original text.  Make that explicit so the model does not explain
            # the previous chat line by mistake.
            context_text = (
                f"{context_text}\n"
                f"（{message.sender_name} 引用了一条消息，但这次事件里没有带"
                "被引用的原文；对方问“这句话/他说的什么意思”时，"
                "请说明看不到引用内容，不要拿聊天记录里的上一条作答）"
            )
            LOGGER.info(
                "引用缺少原文，已提示模型不要猜 | %s | ref=%s",
                message.conversation_id,
                message.quoted_index,
            )
        LOGGER.debug(
            "会话 %s 传入上下文：%s 字（窗口 %s 字）",
            message.conversation_id,
            len(context_text),
            self._contexts.limit_for(message.conversation_id),
        )
        await self._dispatch(message, context_text)

    async def _handle_mute_state(self, message: IncomingMessage) -> bool:
        """Return True when the message was consumed by mute handling.

        A muted group ignores everything except an @mention containing an
        unmute phrase.  The mute is group-local and expires automatically.
        """

        if message.scene != "group":
            return False

        remaining = self._mute_remaining(message.conversation_id)

        if remaining > 0:
            if _is_unmute_command(message):
                self._mute_until.pop(message.conversation_id, None)
                LOGGER.info(
                    "解除闭嘴模式 | %s | %s",
                    message.conversation_id,
                    message.sender_name,
                )
                await self._reply_text(message, "好，我回来了。")
                return True
            LOGGER.info(
                "闭嘴模式中，忽略消息 | %s | 剩余 %.0f 秒 | %s",
                message.conversation_id,
                remaining,
                message.content[:40].replace("\n", " "),
            )
            return True

        if _is_mute_command(message):
            self._mute_until[message.conversation_id] = (
                time.monotonic() + _MUTE_SECONDS
            )
            LOGGER.info(
                "进入闭嘴模式 10 分钟 | %s | %s",
                message.conversation_id,
                message.sender_name,
            )
            await self._reply_text(
                message,
                "好，我闭嘴十分钟。需要我说话时 @ 我说“允许说话”。",
            )
            return True

        return False

    def _mute_remaining(self, conversation_id: str) -> float:
        until = self._mute_until.get(conversation_id)
        if until is None:
            return 0.0
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._mute_until.pop(conversation_id, None)
            LOGGER.info("闭嘴模式已到期 | %s", conversation_id)
            return 0.0
        return remaining

    def _remember_group_target(self, message: IncomingMessage) -> None:
        """Track groups so the water timer can speak without a new event."""

        self._group_targets[message.conversation_id] = message.target_id
        self._group_last_spoke.setdefault(
            message.conversation_id, time.monotonic()
        )

    def _mark_group_spoke(self, conversation_id: str) -> None:
        self._group_last_spoke[conversation_id] = time.monotonic()

    async def run_water_timer(self, stop_event: asyncio.Event) -> None:
        """Post one message in groups where the bot has been quiet too long."""

        hours = self._settings.group_water_hours
        if hours <= 0:
            LOGGER.info("水群计时器已关闭（GROUP_WATER_HOURS=0）")
            return
        interval = max(5.0, self._settings.group_water_check_seconds)
        LOGGER.info(
            "水群计时器已启动：连续 %g 小时未发言时主动水一句（每 %g 秒检查）",
            hours,
            interval,
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.water_idle_groups()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("水群计时器检查失败")

    async def water_idle_groups(self, *, now: float | None = None) -> int:
        """Send at most one water message per idle group; return the count."""

        hours = self._settings.group_water_hours
        if hours <= 0:
            return 0
        threshold = hours * 3600.0
        current = time.monotonic() if now is None else now
        sent = 0

        for conversation_id, target_id in list(self._group_targets.items()):
            last = self._group_last_spoke.get(conversation_id)
            if last is None or current - last < threshold:
                continue
            if self._mute_remaining(conversation_id) > 0:
                LOGGER.debug(
                    "水群计时器跳过闭嘴中的群 | %s", conversation_id
                )
                continue

            # Reset before sending: a failed proactive message should not be
            # retried on every tick and turn into a send storm.
            self._group_last_spoke[conversation_id] = current
            content = random.choice(_WATER_MESSAGES)
            try:
                await self._api.send_group_text(target_id, content, "")
            except Exception as exc:
                LOGGER.warning(
                    "水群发言失败 | %s | %s", conversation_id, exc
                )
                continue

            self._contexts.append(
                conversation_id,
                author=self._settings.bot_name,
                content=content,
                role="assistant",
            )
            self._group_silence[conversation_id] = 0
            sent += 1
            LOGGER.info(
                "群聊连续 %g 小时未发言，主动水群 | %s | %s",
                hours,
                conversation_id,
                content,
            )

        return sent

    async def _dispatch(self, message: IncomingMessage, context_text: str) -> None:
        # The bounded block is intentionally available here.  Today's command
        # router does not need an LLM, but an AI backend can consume context_text
        # without changing the window management or event pipeline.
        LOGGER.debug("dispatch context bytes=%s", len(context_text.encode("utf-8")))
        text = message.content.strip()
        normalized = text.replace("／", "/")
        parts = normalized.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command == "/ping":
            await self._reply_text(message, "pong! 大肥鱼已在线")
            return

        if command in {"/help", "/帮助", "/菜单"}:
            await self._reply_text(message, self._help_text())
            return

        if command in {"/meme", "/表情包"}:
            alias = _meme_save_alias(argument)
            if alias is not None:
                await self._save_meme(message, alias)
            else:
                await self._send_meme(message, argument)
            return

        if command in {"/image", "/img", "/图片"}:
            await self._send_network_image(message, argument)
            return

        if command in {"/context", "/上下文"}:
            await self._handle_context_command(message, argument)
            return

        if command in {"/reset", "/清空", "/清空上下文"}:
            self._contexts.forget(message.conversation_id)
            await self._reply_text(
                message, "🧹 当前会话的上下文已清空，我们重新开始～"
            )
            self._contexts.forget(message.conversation_id)
            return

        # Also accept a bare public image URL, e.g. "@大肥鱼 https://.../a.png".
        # Non-image links fall through to the AI so the page can be read.
        if is_http_url(normalized) and looks_like_image_url(normalized):
            await self._send_network_image(message, normalized)
            return

        # Group full-mode messages are noisy.  Unless the bot was explicitly
        # mentioned, ask the model whether this message is worth joining before
        # doing anything that produces a visible reply.
        if self._should_judge(message):
            # Only ordinary messages (no explicit @bot) count toward the
            # "silent for N messages" threshold.  @mentions and commands are
            # direct interactions and must not inflate or reset the counter.
            count = self._group_silence.get(message.conversation_id, 0) + 1
            self._group_silence[message.conversation_id] = count
            LOGGER.info(
                "群聊静默计数 %s/%s | %s | %s",
                count,
                self._settings.group_idle_speak_every,
                message.conversation_id,
                message.content[:40].replace("\n", " "),
            )

            # After a long silent stretch, say something even if the current
            # message itself looks unimportant.
            if self._should_interject(message):
                if self._reserve_group_reply_slot(message):
                    await self._reply_with_interjection(message, context_text)
                    return
            if not await self._group_message_worth_reply(message, context_text):
                return
            if not self._reserve_group_reply_slot(message):
                return

        # Natural-language meme requests should work without remembering the
        # slash command, e.g. "发个表情包看看".  Questions such as
        # "这个表情包是什么意思" must go to the AI instead of triggering a send.
        if _is_meme_request(normalized):
            await self._send_meme(message)
            return

        if self._ai is not None:
            await self._reply_with_ai(message, context_text)
            return

        if not self._settings.fixed_replies_enabled:
            LOGGER.debug("固定回复已关闭，且未启用 AI，忽略消息")
            return

        lowered = normalized.lower()
        for keywords, replies in _KEYWORD_REPLIES:
            if any(_keyword_matches(lowered, keyword) for keyword in keywords):
                await self._reply_text(message, random.choice(replies))
                return

    def _should_judge(self, message: IncomingMessage) -> bool:
        return (
            message.scene == "group"
            and not message.mentioned
            and self._ai is not None
            and self._settings.group_reply_judge
        )

    def _should_interject(self, message: IncomingMessage) -> bool:
        threshold = self._settings.group_idle_speak_every
        return (
            threshold > 0
            and self._ai is not None
            and message.scene == "group"
            and not message.mentioned
            and self._group_silence.get(message.conversation_id, 0) >= threshold
        )

    def _queue_meme_collection(self, message: IncomingMessage) -> None:
        """Save group images (including quoted ones) into the meme library."""

        urls = tuple(
            dict.fromkeys(
                (*message.image_urls, *message.quoted_image_urls)
            )
        )
        if (
            self._meme_library is None
            or not self._settings.meme_auto_collect
            or message.scene != "group"
            or not urls
            or message.sender_name == self._settings.bot_name
            or _manual_meme_save_alias(message.content) is not None
        ):
            return
        self._spawn(self._collect_memes(message, urls))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _collect_memes(
        self, message: IncomingMessage, urls: tuple[str, ...]
    ) -> None:
        if self._meme_library is None:
            return
        try:
            entries = await self._meme_library.collect_from_urls(urls)
        except Exception:
            LOGGER.exception(
                "群图片加入表情包库失败 | %s", message.conversation_id
            )
            return
        if entries:
            LOGGER.info(
                "群图片已加入表情包库 %s 张 | %s",
                len(entries),
                message.conversation_id,
            )

    async def _meme_catalog_for(
        self, message: IncomingMessage, context_text: str
    ) -> str:
        if (
            self._meme_library is None
            or not self._settings.meme_send_enabled
        ):
            return ""
        try:
            pending = await self._meme_library.refresh()
        except Exception:
            LOGGER.exception("刷新表情包库失败")
            pending = False
        if pending:
            self._spawn(self._meme_library.analyze_pending())
        topic = f"{context_text}\n{message.content}"
        return self._meme_library.catalog_text(topic)

    async def _send_ai_output(
        self, message: IncomingMessage, reply: str
    ) -> None:
        """Send the text reply and, if the model asked for one, a meme."""

        clean_text, meme_name = _extract_meme_marker(reply)
        if clean_text:
            await self._reply_text(message, clean_text)
        if not meme_name or self._meme_library is None:
            return
        if not self._settings.meme_send_enabled:
            LOGGER.debug("表情包主动发送已关闭，忽略 [MEME:%s]", meme_name)
            return

        path = self._meme_library.resolve(meme_name)
        if path is None:
            LOGGER.warning("AI 选择了不存在的表情包: %s", meme_name)
            return

        cooldown = self._settings.meme_send_cooldown
        now = time.monotonic()
        last = self._last_meme_sent.get(message.conversation_id, 0.0)
        if cooldown > 0 and now - last < cooldown:
            LOGGER.info(
                "表情包发送冷却中（还需 %.0fs），跳过 %s | %s",
                cooldown - (now - last),
                path.name,
                message.conversation_id,
            )
            return
        self._last_meme_sent[message.conversation_id] = now

        try:
            await self._reply_image(message, path)
        except (ImageAssetError, QQApiError) as exc:
            LOGGER.warning("主动表情包发送失败 %s: %s", path.name, exc)
            return
        LOGGER.info(
            "主动发送表情包 %s -> %s", path.name, message.conversation_id
        )

    def _remember_quote_source(self, message: IncomingMessage) -> None:
        if not message.message_index and not message.message_id:
            return
        content = message.content
        if message.image_urls:
            content = f"{content} [图片]" if content else "[图片]"
        history = self._quote_cache.setdefault(
            message.conversation_id, deque(maxlen=_QUOTE_CACHE_LIMIT)
        )
        if (
            history
            and history[-1][0] == message.message_index
            and history[-1][1] == message.message_id
        ):
            return
        history.append(
            (
                message.message_index,
                message.message_id,
                message.sender_name,
                content,
                message.image_urls,
            )
        )

    def _resolve_quote(
        self, message: IncomingMessage
    ) -> tuple[str, str, tuple[str, ...]]:
        """Resolve the quoted message text/images for the current turn.

        QQ usually sends the quoted content inside ``msg_elements``; the local
        cache is a fallback for events that only carry ``ref_msg_idx``.
        """

        author = message.quoted_author.strip()
        content = message.quoted_content.strip()
        images = message.quoted_image_urls
        if not content and not images and message.quoted_index:
            history = self._quote_cache.get(message.conversation_id)
            if history:
                for index, message_id, cached_author, cached_content, cached_images in reversed(history):
                    if message.quoted_index in {index, message_id}:
                        author = author or cached_author
                        content = content or cached_content
                        images = images or cached_images
                        break
        if not content and images:
            content = "[图片]"
        if content:
            content = content[:_QUOTE_CONTENT_LIMIT]
        return author, content, images

    def _remember_recent_images(self, message: IncomingMessage) -> None:
        if not message.image_urls:
            return
        history = self._recent_images.setdefault(
            message.conversation_id, deque(maxlen=_IMAGE_HISTORY_LIMIT)
        )
        history.append(
            (time.monotonic(), message.sender_name, message.image_urls)
        )
        LOGGER.info(
            "缓存图片附件 | %s | %s 张 | %s",
            message.conversation_id,
            len(message.image_urls),
            message.image_urls[0][:160],
        )

    def _latest_recent_images(
        self, message: IncomingMessage, limit: int = 4
    ) -> tuple[str, ...]:
        """Return the newest image batch in this conversation, if fresh."""

        history = self._recent_images.get(message.conversation_id)
        if not history:
            return ()
        now = time.monotonic()
        for timestamp, _sender, urls in reversed(history):
            if now - timestamp > _IMAGE_FOLLOWUP_TTL:
                break
            if urls:
                return urls[:limit]
        return ()

    def _images_for_reply(self, message: IncomingMessage) -> tuple[str, ...]:
        """Attach the most recent images when a text message refers to one.

        QQ only puts ``attachments`` on the message that carried the image, so
        a follow-up such as “这张图写了啥” would otherwise reach the model
        without any image at all.
        """

        if message.image_urls:
            return message.image_urls
        if message.quoted_image_urls:
            return message.quoted_image_urls[:4]
        lowered = message.content.lower()
        if not any(
            keyword in lowered for keyword in _IMAGE_FOLLOWUP_KEYWORDS
        ):
            return ()
        history = self._recent_images.get(message.conversation_id)
        if not history:
            return ()
        now = time.monotonic()
        urls: list[str] = []
        for timestamp, _sender, entry_urls in reversed(history):
            if now - timestamp > _IMAGE_FOLLOWUP_TTL:
                break
            for url in entry_urls:
                if url not in urls:
                    urls.append(url)
            if len(urls) >= 4:
                break
        if urls:
            LOGGER.info(
                "引用最近图片 | %s | %s 张", message.conversation_id, len(urls)
            )
        return tuple(urls[:4])

    async def _reply_with_interjection(
        self, message: IncomingMessage, context_text: str
    ) -> None:
        if self._ai is None:
            return
        silent_count = self._group_silence.get(message.conversation_id, 0)
        LOGGER.info(
            "群聊连续 %s 条未回复，触发主动插话 | %s",
            silent_count,
            message.conversation_id,
        )
        meme_catalog = await self._meme_catalog_for(message, context_text)
        try:
            if meme_catalog:
                reply = await self._ai.interject(
                    context_text, meme_catalog=meme_catalog
                )
            else:
                reply = await self._ai.interject(context_text)
        except AIError as exc:
            LOGGER.error("群聊主动插话失败: %s", exc)
            return
        LOGGER.info(
            "AI 插话内容 | %s | %s",
            message.conversation_id,
            reply[:200].replace("\n", " "),
        )
        await self._send_ai_output(message, reply)

    async def _group_message_worth_reply(
        self, message: IncomingMessage, context_text: str
    ) -> bool:
        if self._ai is None:
            return True
        try:
            decision = await self._ai.judge(
                context_text,
                message.content or "[图片]",
                sender_name=message.sender_name,
            )
        except AIError as exc:
            # The judge is a traffic filter.  If it fails, staying silent is
            # safer than turning an API hiccup into a burst of replies.
            LOGGER.warning("群聊重要性判断失败，本条不回复: %s", exc)
            return False

        LOGGER.info(
            "群聊重要性判断 reply=%s score=%s reason=%s | %s | %s",
            decision.should_reply,
            decision.score,
            decision.reason or "-",
            message.conversation_id,
            message.content[:60].replace("\n", " "),
        )
        return decision.should_reply

    def _reserve_group_reply_slot(self, message: IncomingMessage) -> bool:
        """Throttle AI-initiated group replies so a burst cannot spam the room."""

        cooldown = self._settings.group_reply_cooldown
        if cooldown <= 0:
            return True
        now = time.monotonic()
        last = self._last_group_reply.get(message.conversation_id, 0.0)
        remaining = cooldown - (now - last)
        if remaining > 0:
            LOGGER.info(
                "群聊回复冷却中（还剩 %.1fs），跳过 | %s",
                remaining,
                message.conversation_id,
            )
            return False
        self._last_group_reply[message.conversation_id] = now
        return True

    def _help_text(self) -> str:
        return (
            "🐟 大肥鱼指令菜单\n"
            "/ping — 检测大肥鱼是否在线\n"
            "/help — 查看本菜单\n"
            "/表情包 或 /meme — 随机发送一张本地表情包\n"
            "/表情包 <关键词> — 按描述/标签发送，例如 /表情包 哈哈哈\n"
            "/表情包 保存 <名字> — 引用图片后保存并发送一次\n"
            "@我 + 闭嘴 — 十分钟内不再回复\n"
            "@我 + 允许说话 — 解除闭嘴\n"
            "也可以直接说“发个表情包看看”\n"
            "/image <图片URL> 或 /img — 发送网络图片（也可直接发 .png/.jpg 等图片链接）\n"
            "直接发网页链接并 @ 我 — 大肥鱼能读网页正文\n"
            "直接发图片并 @ 我 — 大肥鱼能看图\n"
            "/context — 查看当前上下文窗口\n"
            f"/context 5000|1w|5w — 调整窗口（{context_options_label()}）\n"
            "/reset — 清空当前会话的聊天上下文\n"
            "关键词：你好、在吗、摸鱼"
        )

    async def _handle_context_command(
        self, message: IncomingMessage, argument: str
    ) -> None:
        limit, chars, count = self._contexts.stats(message.conversation_id)
        if not argument:
            await self._reply_text(
                message,
                "🐟 当前上下文窗口："
                f"{context_size_label(limit)}\n"
                f"已使用 {chars:,} 字 / {limit:,} 字，共 {count} 条消息\n"
                f"可调整：{context_options_label()}\n"
                "发送 /context 5000、/context 1w 或 /context 5w 进行切换。",
            )
            return

        size = normalize_context_size(argument)
        if size is None:
            await self._reply_text(
                message,
                f"这个窗口大小不支持哦。可选：{context_options_label()}。\n"
                "例如：/context 1w",
            )
            return

        old, new = await self._contexts.set_limit(message.conversation_id, size)
        if old == new:
            await self._reply_text(
                message, f"上下文窗口已经是 {context_size_label(new)} 啦。"
            )
        else:
            await self._reply_text(
                message,
                "✅ 上下文窗口已调整："
                f"{context_size_label(old)} → {context_size_label(new)}。\n"
                "之后的每条消息都会按新窗口裁剪后传入处理。",
            )

    async def _reply_with_ai(
        self, message: IncomingMessage, context_text: str
    ) -> None:
        if self._ai is None:
            return
        images = self._images_for_reply(message)
        meme_catalog = await self._meme_catalog_for(message, context_text)
        if self._web_fetcher is not None and self._should_read_links(message):
            link_context = await self._link_context(message)
            if link_context:
                # Temporary: this block is sent with this reply only and is
                # never appended to the stored conversation history.
                context_text = f"{context_text}\n\n{link_context}"
        LOGGER.info(
            "请求 AI | model=%s | scene=%s | context=%s 字 | images=%s | memes=%s",
            self._settings.ai_model,
            message.scene,
            len(context_text),
            len(images),
            "有" if meme_catalog else "无",
        )
        kwargs: dict[str, object] = {}
        if images:
            kwargs["images"] = images
        if meme_catalog:
            kwargs["meme_catalog"] = meme_catalog
        try:
            reply = await self._ai.chat(context_text, **kwargs)
        except AIError as exc:
            LOGGER.error("AI 回复失败: %s", exc)
            if message.scene == "group":
                # A failure notice in a busy group is just more noise.
                return
            await self._reply_text(
                message,
                "AI 暂时连接失败了，请稍后再试。",
            )
            return
        LOGGER.info(
            "AI 回复内容 | %s | %s",
            message.conversation_id,
            reply[:200].replace("\n", " "),
        )
        await self._send_ai_output(message, reply)

    def _should_read_links(self, message: IncomingMessage) -> bool:
        """Only read pages for direct interactions, never for ambient links."""

        if message.scene != "group":
            return True
        if message.mentioned:
            return True
        return bool(message.quoted_author) and (
            message.quoted_author.strip() == self._settings.bot_name
        )

    async def _link_context(self, message: IncomingMessage) -> str:
        if self._web_fetcher is None:
            return ""
        source = "\n".join(
            part
            for part in (message.content, message.quoted_content)
            if part
        )
        if not source:
            return ""
        try:
            pages = await self._web_fetcher.fetch_for_text(source)
        except Exception:
            LOGGER.exception(
                "读取网页失败 | %s", message.conversation_id
            )
            return ""
        if not pages:
            return ""
        block = render_pages(pages, self._settings.web_fetch_max_chars)
        if not block:
            return ""
        LOGGER.info(
            "已读取网页 | %s | %s | 附加 %s 字",
            message.conversation_id,
            pages[0].url[:120],
            len(block),
        )
        return (
            f"{block}\n"
            "（以上是网页正文摘录。回答时优先依据摘录；"
            "摘录没有覆盖的部分不要编造，可以直说没看到。）"
        )

    async def _save_meme(self, message: IncomingMessage, alias: str) -> None:
        """Save the current/quoted image under a user-provided name."""

        alias = (alias or "").strip()
        if not alias:
            await self._reply_text(
                message,
                "用法：引用一张表情包，发送 /表情包 保存 哈哈哈\n"
                "也可以在同一条消息里带图片并发送这个命令。",
            )
            return
        if self._meme_library is None:
            await self._reply_text(message, "表情包库还没准备好，稍后再试～")
            return

        urls = tuple(
            dict.fromkeys((*message.quoted_image_urls, *message.image_urls))
        )
        if not urls:
            urls = self._latest_recent_images(message)
        if not urls:
            await self._reply_text(
                message,
                "没有找到图片。请引用那张表情包，或把图片和命令一起发。",
            )
            return
        try:
            entries = await self._meme_library.collect_from_urls(
                urls, alias=alias
            )
        except Exception:
            LOGGER.exception(
                "手动保存表情包失败 | %s | alias=%s",
                message.conversation_id,
                alias,
            )
            await self._reply_text(message, "保存失败，请稍后再试。")
            return
        if not entries:
            await self._reply_text(
                message, "没下载到图片，可能链接已过期，请重新发一次。"
            )
            return

        entry = entries[0]
        LOGGER.info(
            "手动保存表情包 %s | alias=%s | %s",
            entry.filename,
            alias,
            message.conversation_id,
        )
        await self._reply_text(message, f"✅ 已保存为「{alias}」，发一次看看～")
        path = self._meme_library.resolve(entry.filename)
        if path is None:
            await self._reply_text(message, "文件保存后找不到了，请重新发一次。")
            return
        try:
            await self._reply_image(message, path)
        except ImageAssetError as exc:
            await self._reply_text(message, f"这张表情包暂时发不出去：{exc}")
        except QQApiError as exc:
            LOGGER.error("发送刚保存的表情包失败: %s", exc)
            await self._reply_text(
                message, self._friendly_api_error(exc, image=True)
            )

    async def _send_meme(
        self, message: IncomingMessage, query: str = ""
    ) -> None:
        query = (query or "").strip()
        selected: Path | None = None
        if query:
            if self._meme_library is None:
                await self._reply_text(message, "表情包库还没准备好，稍后再试～")
                return
            matches = self._meme_library.search(query)
            if not matches:
                await self._reply_text(
                    message,
                    f"🐟 没找到和「{query}」相关的表情包。"
                    "可以先 /表情包 看一张随机的。",
                )
                return
            selected = self._meme_library.resolve(matches[0].filename)
            if selected is None:
                await self._reply_text(
                    message, f"「{query}」对应的文件不见了，请重新上传。"
                )
                return
        elif self._meme_library is not None:
            selected = self._meme_library.random_entry()
        if selected is None:
            try:
                files = [
                    path
                    for path in self._settings.memes_dir.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in _MEME_SUFFIXES
                ]
            except OSError:
                LOGGER.exception(
                    "读取表情包目录失败: %s", self._settings.memes_dir
                )
                files = []
            selected = random.choice(files) if files else None

        if selected is None:
            await self._reply_text(
                message,
                "🐟 表情包仓库空空的。请把 png/jpg/gif/webp 图片放进 "
                "assets/memes/ 再试试。",
            )
            return

        try:
            await self._reply_image(message, selected)
            LOGGER.info(
                "已发送表情包 %s -> %s", selected.name, message.conversation_id
            )
        except ImageAssetError as exc:
            LOGGER.warning("表情包不可用: %s", exc)
            await self._reply_text(
                message,
                f"这张表情包暂时发不出去：{exc}。\n"
                "请换一张，或把文件控制在 "
                f"{self._settings.max_local_image_mb:g}MB 以内。",
            )
        except QQApiError as exc:
            LOGGER.error("发送表情包失败: %s", exc)
            await self._reply_text(message, self._friendly_api_error(exc, image=True))

    async def _send_network_image(
        self, message: IncomingMessage, argument: str
    ) -> None:
        url = argument.strip().strip("<>").strip()
        if not url:
            await self._reply_text(
                message,
                "用法：/image <公网图片URL>\n例如：/image https://example.com/a.png",
            )
            return
        if not is_http_url(url):
            await self._reply_text(
                message, "这不是合法的 http/https 图片地址，请检查后再发送。"
            )
            return

        try:
            await self._reply_image(message, url)
            LOGGER.info("已发送网络图片 %s -> %s", url, message.conversation_id)
        except (QQApiError, ImageAssetError) as exc:
            LOGGER.error("发送网络图片失败: %s", exc)
            friendly = (
                self._friendly_api_error(exc, image=True)
                if isinstance(exc, QQApiError)
                else str(exc)
            )
            await self._reply_text(message, f"图片没发出去：{friendly}")

    async def _reply_text(self, message: IncomingMessage, content: str) -> None:
        content = truncate_text(content.strip(), self._settings.max_message_chars)
        if not content:
            return
        try:
            if message.scene == "group":
                await self._api.send_group_text(
                    message.target_id, content, message.message_id
                )
            elif message.scene == "c2c":
                await self._api.send_c2c_text(
                    message.target_id, content, message.message_id
                )
            else:
                await self._api.send_channel_text(
                    message.target_id, content, message.message_id
                )
            self._contexts.append(
                message.conversation_id,
                author=self._settings.bot_name,
                content=content,
                role="assistant",
            )
            if message.scene == "group":
                self._reset_group_silence(message)
                self._mark_group_spoke(message.conversation_id)
            LOGGER.info(
                "文本回复已发送 scene=%s target=%s msg_id=%s chars=%s",
                message.scene,
                message.target_id,
                message.message_id,
                len(content),
            )
        except QQApiError as exc:
            LOGGER.error(
                "文本被动回复失败 scene=%s target=%s msg_id=%s: %s",
                message.scene,
                message.target_id,
                message.message_id,
                exc,
            )

    async def _reply_image(
        self, message: IncomingMessage, image: str | Path
    ) -> None:
        if message.scene == "group":
            await self._api.send_group_image(
                message.target_id, image, message.message_id
            )
        elif message.scene == "c2c":
            await self._api.send_c2c_image(
                message.target_id, image, message.message_id
            )
        else:
            await self._api.send_channel_image(
                message.target_id, image, message.message_id
            )
        if message.scene == "group":
            self._reset_group_silence(message)
            self._mark_group_spoke(message.conversation_id)

    def _reset_group_silence(self, message: IncomingMessage) -> None:
        if message.scene != "group":
            return
        previous = self._group_silence.get(message.conversation_id, 0)
        if previous:
            LOGGER.info(
                "群聊静默计数清零（大肥鱼已发言）| %s | 之前=%s",
                message.conversation_id,
                previous,
            )
        self._group_silence[message.conversation_id] = 0

    @staticmethod
    def _friendly_api_error(exc: QQApiError, *, image: bool = False) -> str:
        code = str(exc.code or "")
        mapping = {
            "304103": "这条消息已经超过被动回复时效，请重新 @ 我。",
            "40034005": "这条消息已经超过被动回复时效，请重新 @ 我。",
            "40034128": "这条消息已经超过被动回复时效或次数上限，请重新 @ 我。",
            "40054002": "大肥鱼被禁言了，等管理员解禁后再试吧。",
            "850018": "大肥鱼被禁言了，或者暂时不能在群里发言。",
            "40034101": "大肥鱼还不是这个群的成员。",
            "40054003": "大肥鱼还不是这个群的成员。",
            "40054004": "请先添加大肥鱼为好友，再发送私聊消息。",
            "40054006": "好友关系验证失败，请稍后重试。",
            "40054007": "回复内容太长了，稍后再试吧。",
            "40054013": "你已关闭接收消息，无法回复。",
            "40054018": "消息过长或异常，请缩短后重试。",
            "40034006": "内容没有通过审核，换一种说法试试。",
            "40054010": "平台不允许发送这个 URL。",
            "40034100": "发送太频繁了，休息一下再试。",
            "850019": "平台不支持这张图片的格式。",
            "850031": "图片超过了平台限制，请换一张小一点的。",
            "40093002": "今天的群文件发送额度用完了，明天再来吧。",
        }
        if code in mapping:
            return mapping[code]
        if image:
            return "图片上传或发送失败，请稍后重试，或换一张图片。"
        return "发送失败，请稍后重试。"


def _parse_event_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


def _keyword_matches(text: str, keyword: str) -> bool:
    """Substring match for CJK, word-boundary match for short Latin keywords."""

    if keyword.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _is_mute_command(message: IncomingMessage) -> bool:
    """Only an @mention such as “@大肥鱼 闭嘴” can mute the group."""

    if message.scene != "group" or not message.mentioned:
        return False
    text = (message.content or "").replace("／", "/")
    if any(keyword in text for keyword in _UNMUTE_KEYWORDS):
        return False
    return any(keyword in text for keyword in _MUTE_KEYWORDS)


def _is_unmute_command(message: IncomingMessage) -> bool:
    if message.scene != "group" or not message.mentioned:
        return False
    text = (message.content or "").replace("／", "/")
    return any(keyword in text for keyword in _UNMUTE_KEYWORDS)


def _is_meme_request(text: str) -> bool:
    """Return True only for requests like “发个表情包”, not questions."""

    lowered = (text or "").lower()
    if not any(
        _keyword_matches(lowered, keyword) for keyword in _MEME_KEYWORDS
    ):
        return False
    if any(marker in lowered for marker in _MEME_QUESTION_MARKERS):
        return False
    return any(
        _keyword_matches(lowered, verb) for verb in _MEME_REQUEST_VERBS
    )


def _meme_save_alias(argument: str) -> str | None:
    """Parse ``/表情包 保存 哈哈哈`` into the alias ``哈哈哈``.

    Returns ``None`` when the argument is a normal search term, and ``""``
    when the user asked to save but forgot the name.
    """

    argument = (argument or "").strip()
    if not argument:
        return None
    for prefix in _MEME_SAVE_PREFIXES:
        if argument == prefix:
            return ""
        if argument.startswith(prefix):
            return argument[len(prefix):].strip(" ：:，,")
    return None


def _manual_meme_save_alias(text: str) -> str | None:
    """Detect a manual save command so auto-collection does not race it."""

    normalized = (text or "").strip().replace("／", "/")
    parts = normalized.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() not in {"/meme", "/表情包"}:
        return None
    return _meme_save_alias(parts[1])


def _extract_meme_marker(reply: str) -> tuple[str, str]:
    """Split ``[MEME:file.png]`` out of a model reply."""

    name = ""
    match = _MEME_MARKER_RE.search(reply)
    if match is not None:
        name = match.group(1).strip()
    clean = _MEME_MARKER_RE.sub("", reply)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, name
