"""QQ WebSocket gateway lifecycle: Hello, Identify, Heartbeat and Resume."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import random
import re
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qsl, urlparse

from websockets.exceptions import ConnectionClosed

try:  # websockets >= 13 new asyncio implementation
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - compatibility with older websockets
    from websockets import connect as ws_connect  # type: ignore[no-redef]

from config import Settings
from handlers.message_handler import IncomingMessage, MessageHandler
from utils.api import QQApiClient, QQApiError
from utils.auth import TokenManager
from utils.text import clean_message_text, extract_mention_ids

LOGGER = logging.getLogger(__name__)

_GROUP_AT_EVENT = "GROUP_AT_MESSAGE_CREATE"
_GROUP_FULL_EVENT = "GROUP_MESSAGE_CREATE"
_CHANNEL_EVENT = "AT_MESSAGE_CREATE"
_C2C_EVENT = "C2C_MESSAGE_CREATE"
_GROUP_EVENTS = {_GROUP_AT_EVENT, _GROUP_FULL_EVENT}
_MESSAGE_EVENTS = {_GROUP_AT_EVENT, _GROUP_FULL_EVENT, _CHANNEL_EVENT, _C2C_EVENT}
_FATAL_CLOSE_CODES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}


def _mentions_bot(raw_content: str, bot_id: str, bot_name: str) -> bool:
    """Detect an explicit @bot in a full-mode group message."""

    if bot_id and (
        f"<@!{bot_id}>" in raw_content or f"<@{bot_id}>" in raw_content
    ):
        return True
    if bot_name and f"@{bot_name}" in raw_content:
        return True
    return False


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _extract_scene_ext(data: dict[str, Any]) -> dict[str, str]:
    """Parse ``message_scene.ext`` (list of ``key=value`` strings)."""

    scene = data.get("message_scene")
    if not isinstance(scene, dict):
        return {}
    ext = scene.get("ext")
    if isinstance(ext, dict):
        return {
            str(key).strip(): str(value).strip()
            for key, value in ext.items()
            if str(key).strip()
        }
    if not isinstance(ext, list):
        return {}
    result: dict[str, str] = {}
    for item in ext:
        text = str(item)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _find_quoted_element(
    elements: list[Any], *, inside_quote: bool = False
) -> tuple[str, str, tuple[str, ...]] | None:
    """Recursively find the quoted message inside ``msg_elements``.

    Top-level elements only count when marked ``message_type=103``; once we
    descend into a 103 wrapper, ordinary content elements are accepted too.
    """

    for element in elements:
        if not isinstance(element, dict):
            continue
        message_type = element.get("message_type")
        is_quote = str(message_type) == "103"
        author = element.get("author")
        author_name = ""
        if isinstance(author, dict):
            author_name = str(author.get("username") or "")
        content = str(element.get("content") or "").strip()
        images = _extract_image_urls(element)
        if is_quote and (content or images):
            return author_name, content, images

        nested = element.get("msg_elements")
        if isinstance(nested, list):
            found = _find_quoted_element(
                nested, inside_quote=inside_quote or is_quote
            )
            if found is not None:
                nested_author, nested_content, nested_images = found
                if is_quote and author_name and not nested_author:
                    nested_author = author_name
                return nested_author, nested_content, nested_images

        if inside_quote and (content or images):
            return author_name, content, images
    return None


def _collect_element_contents(
    elements: list[Any],
) -> tuple[str, str, tuple[str, ...]]:
    """Collect content/images from a quote/merged-message element list."""

    authors: list[str] = []
    contents: list[str] = []
    images: list[str] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if isinstance(author, dict):
                name = str(author.get("username") or "").strip()
                if name and name not in authors:
                    authors.append(name)
            content = str(item.get("content") or "").strip()
            if content:
                contents.append(content)
            images.extend(_extract_image_urls(item))
            nested = item.get("msg_elements")
            if isinstance(nested, list):
                walk(nested)

    walk(elements)
    unique_images = tuple(dict.fromkeys(images))
    return (
        "、".join(authors),
        "\n".join(contents),
        unique_images,
    )


def _extract_quoted_message(
    data: dict[str, Any],
) -> tuple[str, str, tuple[str, ...], str]:
    """Return ``(author, content, images, ref_index)`` for a quote reply.

    QQ group quote replies arrive as ``message_scene.ext`` with
    ``ref_msg_idx=...`` plus ``msg_elements`` whose ``message_type`` is 103.
    """

    ext = _extract_scene_ext(data)
    ref_index = ext.get("ref_msg_idx") or ""
    elements = data.get("msg_elements")
    quoted = (
        _find_quoted_element(elements)
        if isinstance(elements, list)
        else None
    )
    # A quote reply can mark the whole event as message_type=103 while the
    # quoted text lives directly in msg_elements (no element-level type).
    if (
        quoted is None
        and str(data.get("message_type")) == "103"
        and isinstance(elements, list)
        and elements
    ):
        author, content, images = _collect_element_contents(elements)
        if content or images:
            quoted = (author, content, images)

    # Fallback for clients that still send a message_reference object.
    if quoted is None:
        reference = data.get("message_reference")
        if isinstance(reference, dict):
            ref_index = ref_index or str(
                reference.get("message_id") or reference.get("id") or ""
            )
            content = str(
                reference.get("content") or reference.get("text") or ""
            ).strip()
            images = _extract_image_urls(reference)
            if content or images:
                quoted = ("", content, images)

    if quoted is None:
        return "", "", (), ref_index
    author, content, images = quoted
    return author, content[:2000], images, ref_index


def _extract_image_urls(data: dict[str, Any]) -> tuple[str, ...]:
    """Collect image attachments from a message event.

    QQ delivers media as URLs inside ``attachments``; the WebSocket frame never
    contains the image bytes itself.
    """

    urls: list[str] = []
    attachments = data.get("attachments")
    if not isinstance(attachments, list):
        return ()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        url = str(attachment.get("url") or "").strip()
        if not url:
            continue
        content_type = str(attachment.get("content_type") or "").lower()
        filename = str(attachment.get("filename") or "")
        suffix = os.path.splitext(filename)[1].lower()
        if content_type.startswith("image/") or suffix in _IMAGE_SUFFIXES:
            LOGGER.info(
                "图片附件 | type=%s | name=%s | size=%s | %sx%s | url=%s | params=%s",
                content_type or "?",
                filename or "-",
                attachment.get("size") or "?",
                attachment.get("width") or "?",
                attachment.get("height") or "?",
                len(url),
                ",".join(
                    key for key, _ in parse_qsl(urlparse(url).query)
                )
                or "-",
            )
            urls.append(url)
    return tuple(urls)


class GatewayReconnect(RuntimeError):
    """Internal signal asking the reconnect loop to establish a fresh link."""


class QQGatewayClient:
    def __init__(
        self,
        settings: Settings,
        api: QQApiClient,
        tokens: TokenManager,
        handler: MessageHandler,
    ) -> None:
        self._settings = settings
        self._api = api
        self._tokens = tokens
        self._handler = handler

        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._bot_id: str = ""
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval = 45.0
        self._heartbeat_ack_pending = False
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self._backoff = 1.0
        self._seen_messages: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        # openid/id -> nickname, learned from authors so later @mentions can be
        # rendered as readable names instead of being dropped or shown as hashes.
        self._member_names: dict[str, str] = {}
        self._member_names_file = settings.data_dir / "member_names.json"
        self._load_member_names()
        # The bot's own group openid is not exposed by the API.  It shows up as
        # ``<@!openid>`` inside full-mode messages, and the bot never appears as
        # an author, so an unknown leading mention can be learned as the bot.
        self._bot_mentions: set[str] = set()
        self._bot_mentions_file = settings.data_dir / "bot_mentions.json"
        self._load_bot_mentions()
        self._group_at_events = 0
        self._group_full_events = 0
        self._warned_no_full_mode = False

    def _load_member_names(self) -> None:
        try:
            raw = self._member_names_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning("读取群成员昵称缓存失败", exc_info=True)
            return
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            LOGGER.warning("群成员昵称缓存格式错误，忽略：%s", self._member_names_file)
            return
        if isinstance(data, dict):
            self._member_names.update(
                {
                    str(key): str(value)
                    for key, value in data.items()
                    if key and value
                }
            )
            if self._member_names:
                LOGGER.info("已载入 %s 个群成员昵称", len(self._member_names))

    def _remember_member(self, identifier: str, name: str) -> None:
        if not identifier or not name or name == "QQ用户":
            return
        if identifier in self._bot_mentions:
            # A token we guessed was the bot spoke as a human: undo the guess.
            self._bot_mentions.discard(identifier)
            self._save_bot_mentions()
        if self._member_names.get(identifier) == name:
            return
        self._member_names[identifier] = name
        try:
            self._member_names_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self._member_names_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(
                    self._member_names,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temp_file, self._member_names_file)
        except OSError:
            LOGGER.warning("保存群成员昵称缓存失败", exc_info=True)

    def _load_bot_mentions(self) -> None:
        try:
            raw = self._bot_mentions_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning("读取机器人 @ 标识缓存失败", exc_info=True)
            return
        try:
            data = json.loads(raw or "[]")
        except ValueError:
            LOGGER.warning("机器人 @ 标识缓存格式错误，忽略：%s", self._bot_mentions_file)
            return
        if isinstance(data, list):
            self._bot_mentions.update(str(item) for item in data if item)
            if self._bot_mentions:
                LOGGER.info("已载入 %s 个机器人 @ 标识", len(self._bot_mentions))

    def _save_bot_mentions(self) -> None:
        try:
            self._bot_mentions_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self._bot_mentions_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(sorted(self._bot_mentions), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temp_file, self._bot_mentions_file)
        except OSError:
            LOGGER.warning("保存机器人 @ 标识缓存失败", exc_info=True)

    def _remember_bot_mention(self, identifier: str) -> None:
        if not identifier or identifier in self._bot_mentions:
            return
        self._bot_mentions.add(identifier)
        self._save_bot_mentions()
        LOGGER.info(
            "识别到机器人被 @ 的群内标识：%s（之后会按 @机器人 处理）", identifier
        )

    def _identify_bot_mentions(self, raw_content: str) -> set[str]:
        """Learn the bot's own group openid from a leading opaque mention.

        QQ does not expose the bot's per-group openid, but a full-mode message
        that starts with ``<@!openid>`` and whose openid is not a known member
        is almost certainly addressing the bot.  If that token later speaks as
        a human, :meth:`_remember_member` removes it again.
        """

        tokens = extract_mention_ids(raw_content)
        if not tokens:
            return self._bot_mentions
        unknown = [
            token
            for token in tokens
            if token not in self._member_names and token != self._bot_id
        ]
        leading = re.match(r"^\s*<@!?([^>\s]+)>", raw_content)
        if len(unknown) == 1 and leading and leading.group(1) == unknown[0]:
            self._remember_bot_mention(unknown[0])
        return self._bot_mentions

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="shutdown")

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() and not self._stopping:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except GatewayReconnect as exc:
                LOGGER.warning("需要重新连接网关：%s", exc)
            except ConnectionClosed as exc:
                LOGGER.warning(
                    "WebSocket 已断开：code=%s reason=%s",
                    getattr(exc, "code", "?"),
                    getattr(exc, "reason", ""),
                )
                try:
                    fatal = await self._handle_close_code(exc.code)
                except Exception:
                    fatal = False
                    LOGGER.exception("处理 WebSocket 关闭码失败")
                if fatal:
                    stop_event.set()
                    break
            except (QQApiError, OSError) as exc:
                LOGGER.warning("网关连接/鉴权失败：%s", exc)
            except Exception:
                LOGGER.exception("网关连接出现未预期异常")
            finally:
                await self._cleanup_connection()

            if stop_event.is_set() or self._stopping:
                break

            delay = min(60.0, self._backoff) + random.uniform(0, 0.8)
            LOGGER.info("将在 %.1f 秒后重连网关…", delay)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self._backoff = min(60.0, self._backoff * 2)
            except asyncio.CancelledError:
                raise

        await self._cancel_handler_tasks()
        LOGGER.info("网关协程已停止")

    # ------------------------------------------------------------ connection
    async def _connect_once(self) -> None:
        gateway_url = await self._api.get_gateway()
        LOGGER.info(
            "连接 WebSocket 网关：%s（session=%s, seq=%s）",
            gateway_url,
            "resume" if self._can_resume() else "new",
            self._last_seq,
        )
        async with ws_connect(
            gateway_url,
            ping_interval=None,
            close_timeout=10,
            open_timeout=15,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            hello = await asyncio.wait_for(
                ws.recv(), timeout=self._settings.ws_hello_timeout
            )
            hello_payload = _decode_payload(hello)
            if hello_payload.get("op") != 10:
                raise GatewayReconnect(f"首包不是 Hello：{hello_payload.get('op')}")

            data = hello_payload.get("d") or {}
            interval_ms = int(data.get("heartbeat_interval") or 45_000)
            self._heartbeat_interval = max(5.0, min(120.0, interval_ms / 1000.0))
            self._heartbeat_ack_pending = False
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(ws), name="qq-heartbeat"
            )
            LOGGER.info("收到 Hello，心跳周期 %.1f 秒", self._heartbeat_interval)

            if self._can_resume():
                await self._send(
                    {
                        "op": 6,
                        "d": {
                            "token": f"QQBot {await self._tokens.get_token()}",
                            "session_id": self._session_id,
                            "seq": self._last_seq,
                        },
                    }
                )
                LOGGER.info("已发送 Resume（seq=%s）", self._last_seq)
            else:
                await self._send(
                    {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {await self._tokens.get_token()}",
                            "intents": self._settings.intents,
                            "shard": [0, 1],
                            "properties": {
                                "$os": platform.system().lower() or "linux",
                                "$browser": "dafeiyu",
                                "$device": "dafeiyu",
                            },
                        },
                    }
                )
                LOGGER.info("已发送 Identify（intents=%s）", self._settings.intents)

            async for raw in ws:
                await self._handle_payload(raw)

        LOGGER.info("WebSocket 连接正常关闭")

    async def _cleanup_connection(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ws = None
        self._heartbeat_ack_pending = False

    async def _handle_close_code(self, code: int | None) -> bool:
        """Update session state from the close code, returning True if fatal."""

        if code is None:
            return False
        if code in _FATAL_CLOSE_CODES:
            hint = ""
            if code in {4013, 4014}:
                hint = "；请检查 INTENTS 是否已在开放平台申请对应权限"
            LOGGER.critical("WebSocket 关闭码 %s 不可恢复%s", code, hint)
            return True
        if code == 4004:
            LOGGER.warning("Access Token 无效，刷新后重新 Identify")
            await self._tokens.invalidate()
            self._reset_session()
            self._backoff = 1.0
        elif code in {4006, 4007} or 4900 <= code <= 4913:
            LOGGER.warning("Session 无法恢复（%s），下次使用 Identify", code)
            self._reset_session()
        elif code in {4008, 4009}:
            LOGGER.warning("连接被要求重建（%s），保留 Session 并 Resume", code)
        elif code in {1000, 1001}:
            if not self._stopping:
                LOGGER.info("连接关闭码 %s，将尝试 Resume", code)
        return False

    def _can_resume(self) -> bool:
        return bool(self._session_id) and self._last_seq is not None

    def _reset_session(self) -> None:
        self._session_id = None
        self._last_seq = None

    # ---------------------------------------------------------------- payload
    async def _handle_payload(self, raw: str | bytes) -> None:
        payload = _decode_payload(raw)
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            self._last_seq = seq

        if op == 11:  # Heartbeat ACK
            self._heartbeat_ack_pending = False
            LOGGER.debug("收到心跳 ACK")
            return
        if op == 10:  # A second Hello
            data = payload.get("d") or {}
            interval_ms = int(data.get("heartbeat_interval") or 45_000)
            self._heartbeat_interval = max(5.0, min(120.0, interval_ms / 1000.0))
            return
        if op == 7:  # Reconnect
            LOGGER.info("网关通知 Reconnect")
            raise GatewayReconnect("opcode 7")
        if op == 9:  # Invalid Session
            LOGGER.warning("网关返回 Invalid Session，清除 Session 后 Identify")
            self._reset_session()
            raise GatewayReconnect("opcode 9 invalid session")
        if op == 1:  # Server-initiated heartbeat
            await self._send({"op": 1, "d": self._last_seq})
            return
        if op != 0:
            LOGGER.debug("忽略未知 opcode=%s", op)
            return

        event_type = str(payload.get("t") or "")
        data = payload.get("d")
        if event_type == "READY":
            self._on_ready(data if isinstance(data, dict) else {})
        elif event_type == "RESUMED":
            LOGGER.info("Session 恢复成功，开始补发遗漏事件")
            self._backoff = 1.0
        elif event_type in _MESSAGE_EVENTS:
            self._schedule_event(event_type, data if isinstance(data, dict) else {})
        else:
            LOGGER.debug("忽略事件 %s", event_type)

    def _on_ready(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("session_id") or "")
        user = data.get("user") or {}
        if session_id:
            self._session_id = session_id
        if isinstance(user, dict) and user.get("id"):
            self._bot_id = str(user["id"])
        self._backoff = 1.0
        LOGGER.info(
            "鉴权成功：bot=%s id=%s session=%s",
            user.get("username", self._settings.bot_name),
            self._bot_id or "?",
            session_id or "?",
        )

    def _schedule_event(self, event_type: str, data: dict[str, Any]) -> None:
        message_id = str(data.get("id") or "")
        author = data.get("author") or {}
        if not message_id or not isinstance(author, dict):
            return
        if author.get("bot") or (self._bot_id and str(author.get("id")) == self._bot_id):
            return
        if not self._remember_message(message_id):
            LOGGER.debug("忽略重复推送消息 %s", message_id)
            return

        task = asyncio.create_task(
            self._handle_event(event_type, data), name=f"qq-event-{message_id[-12:]}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_handler_done)

    async def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        author = data.get("author") or {}
        sender_id = str(
            author.get("member_openid")
            or author.get("user_openid")
            or author.get("id")
            or ""
        )
        sender_name = str(author.get("username") or "QQ用户")
        if sender_id and sender_name and sender_name != "QQ用户":
            self._remember_member(sender_id, sender_name)
            member_openid = str(author.get("member_openid") or "")
            if member_openid:
                self._remember_member(member_openid, sender_name)
            author_id = str(author.get("id") or "")
            if author_id:
                self._remember_member(author_id, sender_name)
        raw_content = str(data.get("content") or "")
        bot_mention_ids = (
            self._identify_bot_mentions(raw_content)
            if event_type in _GROUP_EVENTS
            else self._bot_mentions
        )
        content = clean_message_text(
            raw_content,
            self._settings.bot_name,
            self._bot_id,
            mention_names=self._member_names,
            bot_mention_ids=bot_mention_ids,
        )
        image_urls = _extract_image_urls(data)
        if not content and image_urls:
            content = "[图片]"
        scene_ext = _extract_scene_ext(data)
        (
            quoted_author,
            quoted_content,
            quoted_image_urls,
            quoted_index,
        ) = _extract_quoted_message(data)
        elements = data.get("msg_elements")
        element_types = (
            [
                str(item.get("message_type"))
                for item in elements
                if isinstance(item, dict)
            ]
            if isinstance(elements, list)
            else []
        )
        if (
            scene_ext.get("ref_msg_idx")
            or element_types
            or data.get("message_type")
        ):
            LOGGER.info(
                "引用结构 | ext=%s | message_type=%s | elements=%s | parsed=%s/%s",
                scene_ext,
                data.get("message_type"),
                element_types,
                quoted_author or "?",
                (quoted_content or "")[:60].replace("\n", " "),
            )
            LOGGER.debug(
                "引用原始事件 | %s",
                json.dumps(data, ensure_ascii=False)[:3000],
            )
        timestamp = str(data.get("timestamp") or "")

        if event_type in _GROUP_EVENTS:
            group_openid = str(data.get("group_openid") or "")
            if not group_openid or not (
                content or quoted_content or quoted_image_urls
            ):
                return
            self._note_group_event(event_type)
            mentioned = (
                event_type == _GROUP_AT_EVENT
                or _mentions_bot(
                    raw_content, self._bot_id, self._settings.bot_name
                )
                or any(
                    token in self._bot_mentions
                    for token in extract_mention_ids(raw_content)
                )
            )
            message = IncomingMessage(
                scene="group",
                message_id=str(data["id"]),
                target_id=group_openid,
                conversation_id=f"group:{group_openid}",
                content=content,
                sender_id=sender_id,
                sender_name=sender_name,
                timestamp=timestamp,
                mentioned=mentioned,
                image_urls=image_urls,
                message_index=scene_ext.get("msg_idx", ""),
                quoted_index=quoted_index,
                quoted_author=quoted_author,
                quoted_content=quoted_content,
                quoted_image_urls=quoted_image_urls,
            )
        elif event_type == _C2C_EVENT:
            user_openid = str(
                author.get("user_openid") or author.get("id") or ""
            )
            if not user_openid or not (
                content or quoted_content or quoted_image_urls
            ):
                return
            message = IncomingMessage(
                scene="c2c",
                message_id=str(data["id"]),
                target_id=user_openid,
                conversation_id=f"c2c:{user_openid}",
                content=content,
                sender_id=sender_id,
                sender_name=sender_name,
                timestamp=timestamp,
                mentioned=True,
                image_urls=image_urls,
                message_index=scene_ext.get("msg_idx", ""),
                quoted_index=quoted_index,
                quoted_author=quoted_author,
                quoted_content=quoted_content,
                quoted_image_urls=quoted_image_urls,
            )
        else:
            channel_id = str(data.get("channel_id") or "")
            if not channel_id or not (
                content or quoted_content or quoted_image_urls
            ):
                return
            message = IncomingMessage(
                scene="channel",
                message_id=str(data["id"]),
                target_id=channel_id,
                conversation_id=f"channel:{channel_id}",
                content=content,
                sender_id=sender_id,
                sender_name=sender_name,
                timestamp=timestamp,
                mentioned=True,
                image_urls=image_urls,
                message_index=scene_ext.get("msg_idx", ""),
                quoted_index=quoted_index,
                quoted_author=quoted_author,
                quoted_content=quoted_content,
                quoted_image_urls=quoted_image_urls,
            )

        scene_label = {"group": "群", "channel": "频道", "c2c": "私聊"}[message.scene]
        image_note = f" [图片x{len(message.image_urls)}]" if message.image_urls else ""
        quote_note = ""
        if message.quoted_content or message.quoted_image_urls:
            quoted = message.quoted_content or "[图片]"
            quote_note = (
                f" [引用 {message.quoted_author or '?'}:"
                f"{quoted[:30].replace(chr(10), ' ')}]"
            )
        LOGGER.info(
            "收到%s消息%s [%s] | %s(%s) | %s | %s%s%s",
            scene_label,
            "（@机器人）" if message.scene == "group" and message.mentioned else "",
            event_type,
            message.sender_name,
            message.sender_id or "?",
            message.conversation_id,
            message.content[:80].replace("\n", " "),
            image_note,
            quote_note,
        )
        await self._handler.handle(message)

    def _note_group_event(self, event_type: str) -> None:
        if event_type == _GROUP_FULL_EVENT:
            self._group_full_events += 1
            return
        self._group_at_events += 1
        if (
            not self._warned_no_full_mode
            and self._group_full_events == 0
            and self._group_at_events >= 20
        ):
            self._warned_no_full_mode = True
            LOGGER.warning(
                "已收到 %s 条群 @ 消息，但一条群全量消息也没有；"
                "如果希望机器人参与未 @ 它的聊天，请在 QQ 开放平台开启“接收所有消息”",
                self._group_at_events,
            )

    def _remember_message(self, message_id: str) -> bool:
        if message_id in self._seen_messages:
            return False
        self._seen_messages[message_id] = None
        self._seen_messages.move_to_end(message_id)
        while len(self._seen_messages) > 4096:
            self._seen_messages.popitem(last=False)
        return True

    def _on_handler_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            LOGGER.error(
                "消息处理任务异常", exc_info=(type(exc), exc, exc.__traceback__)
            )

    async def _cancel_handler_tasks(self) -> None:
        if not self._tasks:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # --------------------------------------------------------------- outbound
    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise GatewayReconnect("WebSocket 尚未连接")
        async with self._send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._stopping:
                    return
                if self._heartbeat_ack_pending:
                    LOGGER.warning("心跳 ACK 超时，主动断开并重连")
                    await ws.close(code=4000, reason="heartbeat ACK timeout")
                    return
                self._heartbeat_ack_pending = True
                try:
                    async with self._send_lock:
                        await ws.send(
                            json.dumps(
                                {"op": 1, "d": self._last_seq},
                                separators=(",", ":"),
                            )
                        )
                    LOGGER.debug("发送心跳 seq=%s", self._last_seq)
                except Exception:
                    self._heartbeat_ack_pending = False
                    raise
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("心跳协程异常退出")
            with contextlib.suppress(Exception):
                await ws.close(code=4000, reason="heartbeat failed")


def _decode_payload(raw: str | bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GatewayReconnect("收到无法解析的 WebSocket 数据") from exc
    if not isinstance(payload, dict):
        raise GatewayReconnect("WebSocket 数据不是 JSON 对象")
    return payload
