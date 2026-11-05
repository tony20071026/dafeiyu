"""OpenAI-compatible chat-completions client (DeepSeek / Qwen / etc.)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import aiohttp

from config import Settings
from utils.gif_frames import sample_gif_frames_bytes
from utils.media import read_limited

LOGGER = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = (
    "你是 QQ 群聊机器人“大肥鱼”的发言判断器。你的任务不是聊天，"
    "而是判断机器人是否应该回复最后一条群消息。\n\n"
    "值得回复（reply=true）：\n"
    "1. 明确 @ 或点名机器人（大肥鱼、肥鱼、鱼、机器人），向机器人提问；\n"
    "2. 直接用“你/您”对机器人说话，或紧接机器人上一句继续对话；\n"
    "3. 群友直接对机器人说话，质问或评价机器人的表现：为什么慢、为什么回/不回、该不该说话、人设、锐评等；\n"
    "   必须有明确的对话意图；群友之间第三人称讨论机器人（如“他会判断”“这机器人还会撒谎”）不算；\n"
    "   抱怨“回复慢 / API 慢 / 模型慢”算，单纯问 API 价格、去哪找 API、模型选型不算；\n"
    "4. 明确寻求帮助、信息、建议，且机器人能提供；\n"
    "5. 明显的玩笑、梗、求互动，机器人接一句会自然加分；\n"
    "6. 消息引用了某条内容并问“这句话什么意思 / 他说的什么”，"
    "应结合上下文中“引用的是…的这条消息”标注解释。\n\n"
    "不值得回复（reply=false）：\n"
    "1. 明显是对群里其他成员说的：点名别人、@别人、讨论别人的事；\n"
    "2. 群友之间与机器人无关的普通闲聊、寒暄；\n"
    "3. 单纯讨论 API 价格、去哪里找 API、模型选型等技术配置；\n"
    "4. 单个表情、标点、无意义刷屏、重复内容；\n"
    "5. 广告、推广链接、拉人、违规或敏感内容；\n"
    "6. 群友只是在围观、复述、复读机器人的话，没有新的问题或新指向；\n"
    "7. 机器人插话会打断对话，或你拿不准。\n\n"
    "判断“你”指谁时看上一句：上一句是机器人说的，就先按对机器人说话处理。\n"
    "原则：宁可少说，不要抢话；拿不准就不回复。\n"
    "只输出一行 JSON，不要 markdown，不要解释：\n"
    '{"reply": true, "score": 80, "reason": "不超过20字"}'
)


MEME_CAPTION_PROMPT = (
    "你是表情包审核员和标注器。先判断图片是否适合在群聊里当表情包使用，"
    "再用简短中文描述画面和情绪，并给出检索标签、主要情绪、适用场景和风险等级。\n"
    "适合当表情包（is_meme=true）：带文字/梗、夸张表情、搞笑动作、反应图、"
    "卡通形象，能在聊天中表达情绪、吐槽或接梗。\n"
    "不适合（is_meme=false）：普通食物照、饮料照、风景照、普通自拍、商品图、"
    "聊天截图、二维码、文档、表格、证件、屏幕截图、模糊或无意义的图片。\n"
    "emotion 用 2-6 个字，如：开心、无语、震惊、嘲讽、期待、围观、心累、"
    "温柔、危险、其他。\n"
    "use_when 给 2-4 个具体使用场景，每条不超过 12 个字。\n"
    "risk 取值 low / medium / high：竖中指、冒犯、危险动作或容易引战的图用 high，"
    "普通搞笑图用 low。\n"
    "只输出一行 JSON，不要 markdown，不要解释：\n"
    '{"is_meme": true, "emotion": "无语", '
    '"use_when": ["改 bug 改到崩溃", "周一上班"], "risk": "low", '
    '"description": "不超过30字的画面描述", "tags": ["3-6个中文关键词"]}'
)

MEME_SELECTION_RULES = (
    "【表情包】\n"
    "表情包是群聊气氛的一部分。先看当前情绪/场景，再看候选：\n"
    "{catalog}\n"
    "- 只要情绪或使用场景对得上，就配一张，不要总是只发文字；"
    "但对不上就别硬发，别为了发表情包而发表情包。\n"
    "- 普通闲聊、吐槽、接梗、庆祝、围观时都可以考虑；一轮回复最多一张。\n"
    "- 优先选“情绪”和“适合”与当前聊天一致的；标了“风险：高”的图只在明显玩梗的熟人局用。\n"
    "- 需要时在回复最后单独加一行：[MEME:文件名]\n"
    "- 不要解释这个标记，不要编造列表之外的文件名。\n"
    "- 对方认真提问、需要准确信息、争论、安慰、隐私或严肃话题时不要发。"
)

# A GIF expands to four frames, so cap the total image parts sent to the model.
_MAX_CHAT_IMAGE_PARTS = 8


def load_member_aliases(path: Path) -> dict[str, str]:
    """Read the local nickname -> real-name map (never published)."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for nickname, real_name in raw.items():
        if not isinstance(nickname, str) or not isinstance(real_name, str):
            continue
        nickname = nickname.strip()
        real_name = real_name.strip()
        if nickname and real_name and nickname != real_name:
            aliases[nickname] = real_name
    return aliases


def load_group_context(path: Path, limit: int = 2_000) -> str:
    """Read the local, git-ignored background notes for this group.

    The file is optional.  Blank lines and ``#`` comments are dropped so it
    can stay readable without leaking formatting into the prompt.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [
        line.strip()
        for line in raw.replace("\r", "\n").split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    return "\n".join(lines)[:limit]


def build_member_alias_directive(aliases: dict[str, str]) -> str:
    """Build the system-prompt block that prefers real names."""

    if not aliases:
        return ""
    lines = [
        "【称呼】",
        "- 称呼群友时优先使用已知真名，不要用网名或单字简称。",
        "- 只有不知道真名、或对方明确要求用网名时，才使用网名；不要自行猜测。",
        "- 已知昵称与真名的对应关系：",
    ]
    for nickname, real_name in sorted(aliases.items()):
        lines.append(f"  · {nickname} -> {real_name}")
    return "\n".join(lines)


class AIError(RuntimeError):
    """A recoverable AI API failure."""


@dataclass(frozen=True, slots=True)
class MemeCaption:
    """Vision-model caption plus a verdict on whether the image is a meme."""

    description: str
    tags: tuple[str, ...] = ()
    is_meme: bool = True
    emotion: str = ""
    use_when: tuple[str, ...] = ()
    risk: str = "low"


@dataclass(frozen=True, slots=True)
class ImportanceDecision:
    """Whether a group message is worth a bot reply."""

    should_reply: bool
    score: int = 0
    reason: str = ""


class AIClient:
    """OpenAI-compatible chat-completions client with separate reply/judge models."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._endpoint = f"{settings.ai_base_url}/chat/completions"
        self._system_prompt = settings.ai_system_prompt
        context = load_group_context(settings.data_dir / "group_context.txt")
        if context:
            self._system_prompt = (
                f"{self._system_prompt}\n\n【补充背景】\n{context}"
            )
        directive = build_member_alias_directive(
            load_member_aliases(settings.data_dir / "member_aliases.json")
        )
        if directive:
            self._system_prompt = f"{self._system_prompt}\n\n{directive}"

    async def chat(
        self,
        context_text: str,
        *,
        instruction: str | None = None,
        images: Sequence[str] = (),
        meme_catalog: str = "",
    ) -> str:
        context_text = context_text.strip()
        if not context_text:
            raise AIError("上下文为空")
        if not self._settings.ai_api_key:
            raise AIError("未配置 AI_API_KEY")

        instruction = instruction or (
            "下面是最近的聊天记录，最后一条是需要你回复的消息。"
            "请直接给出回复，不要添加“大肥鱼：”前缀。"
            "如果消息里提到图片，但本次没有附带任何图像内容，"
            "不要编造图片内容，直接说明你看不到这张图。"
            "如果最后一条消息下面标注了“引用的是…的这条消息”，"
            "那是当前消息引用/回复的原消息；当对方问“这句话/他说的什么意思”时，"
            "必须优先以引用内容为准，不要拿错成聊天记录里的上一条；"
            "引用内容缺失时直说看不到。"
        )
        user_text = f"{instruction}\n\n{context_text}"
        user_content: str | list[dict[str, Any]] = user_text
        image_urls = [url for url in images if url][:4]
        if image_urls:
            payloads, gif_frames = await self._image_payloads(image_urls)
            if gif_frames:
                user_text = (
                    f"{user_text}\n\n"
                    f"（本次包含动图，已按时间顺序抽取 {gif_frames} 帧；"
                    "请把连续帧理解为同一张动图的画面变化，而不是多张图。）"
                )
            parts: list[dict[str, Any]] = [
                {"type": "text", "text": user_text}
            ]
            for payload in payloads:
                parts.append(
                    {"type": "image_url", "image_url": {"url": payload}}
                )
            if len(parts) > 1:
                user_content = parts
        system_content = self._system_prompt
        if meme_catalog.strip():
            system_content = (
                f"{system_content}\n\n"
                f"{MEME_SELECTION_RULES.format(catalog=meme_catalog.strip())}"
            )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        return await self._complete(
            messages,
            model=self._settings.ai_model,
            max_tokens=self._settings.ai_max_tokens,
            temperature=self._settings.ai_temperature,
            purpose="对话",
        )

    async def interject(
        self, context_text: str, *, meme_catalog: str = ""
    ) -> str:
        """Say one short line after the group has gone quiet for a while."""

        return await self.chat(
            context_text,
            instruction=(
                "你已经在群里潜水很久了。请根据最近的聊天记录，自然地插一句话，"
                "像普通群友一样参与讨论。不要总结聊天记录，不要说“我潜水很久了”，"
                "不要连续提问，控制在 1-2 句，直接给出这句话。"
            ),
            meme_catalog=meme_catalog,
        )

    async def describe_image(self, path: Path) -> MemeCaption:
        """Ask the vision model to caption and vet a candidate meme image."""

        if not self._settings.ai_api_key:
            raise AIError("未配置 AI_API_KEY")
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise AIError(f"读取图片失败: {exc}") from exc
        if not data:
            return MemeCaption("", ())
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"

        content: list[dict[str, Any]] = []
        if data.startswith((b"GIF87a", b"GIF89a")):
            frames = await asyncio.to_thread(
                sample_gif_frames_bytes, data, label=path.name
            )
            if frames:
                content.append(
                    {
                        "type": "text",
                        "text": (
                            "这是一张动图，下面按时间顺序均匀抽取了 "
                            f"{len(frames)} 帧。请综合所有帧描述动作变化、"
                            "表情和情绪，并判断它是否适合当表情包；"
                            "如果像普通录像、聊天截图、二维码、文档等"
                            "不适合的内容，把 is_meme 设为 false。"
                            "只输出 JSON。"
                        ),
                    }
                )
                for frame_data, frame_mime in frames:
                    encoded_frame = base64.b64encode(frame_data).decode("ascii")
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{frame_mime};base64,{encoded_frame}"
                                )
                            },
                        }
                    )
                LOGGER.info(
                    "表情包识别使用 GIF 多帧 | %s 帧 | %s",
                    len(frames),
                    path.name,
                )

        if not content:
            encoded = base64.b64encode(data).decode("ascii")
            content = [
                {
                    "type": "text",
                    "text": "请判断这张图片是否适合当表情包，并描述画面，只输出 JSON。",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}"
                    },
                },
            ]

        messages = [
            {"role": "system", "content": MEME_CAPTION_PROMPT},
            {"role": "user", "content": content},
        ]
        raw = await self._complete(
            messages,
            model=self._settings.ai_model,
            max_tokens=260,
            temperature=0.0,
            purpose="表情包识别",
        )
        return _parse_meme_caption(raw)

    async def judge(
        self,
        context_text: str,
        message_text: str,
        *,
        sender_name: str = "",
    ) -> ImportanceDecision:
        """Ask the model whether a group message deserves a reply.

        Failures raise :class:`AIError`; the caller decides whether to stay
        silent (default) or reply.
        """

        if not self._settings.ai_api_key:
            raise AIError("未配置 AI_API_KEY")

        limit = self._settings.group_judge_context_chars
        context_text = context_text.strip()
        if limit > 0 and len(context_text) > limit:
            context_text = context_text[-limit:]

        who = f"发送者：{sender_name}\n" if sender_name else ""
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "最近聊天记录：\n"
                    f"{context_text or '（无）'}\n\n"
                    "最后一条需要判断的消息：\n"
                    f"{who}内容：{message_text.strip()}\n\n"
                    "请判断大肥鱼是否应该回复，只输出 JSON。"
                ),
            },
        ]
        raw = await self._complete(
            messages,
            model=self._settings.ai_judge_model,
            max_tokens=120,
            temperature=0.0,
            purpose="群聊重要性判断",
        )
        return _parse_decision(raw)

    async def _image_payloads(self, urls: Sequence[str]) -> tuple[list[str], int]:
        """Convert remote image URLs to data URIs so the model never has to
        fetch QQ's signed CDN links itself.

        An animated GIF expands into several evenly spaced frames, so one GIF
        looks like a short animation instead of only its first frame.  Returns
        the data URIs and the total number of GIF frames.
        """

        groups = await asyncio.gather(
            *(self._image_payload_group(url) for url in urls)
        )
        payloads: list[str] = []
        gif_frames = 0
        for group, frames in groups:
            gif_frames += frames
            for payload in group:
                if not payload:
                    continue
                payloads.append(payload)
                if len(payloads) >= _MAX_CHAT_IMAGE_PARTS:
                    return payloads, gif_frames
        return payloads, gif_frames

    async def _image_payload(self, url: str) -> str:
        payloads, _ = await self._image_payload_group(url)
        return payloads[0] if payloads else ""

    async def _image_payload_group(self, url: str) -> tuple[list[str], int]:
        url = (url or "").strip()
        if not url:
            return [], 0
        if url.startswith("data:"):
            return [url], 0

        max_bytes = self._settings.max_local_image_bytes
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                if response.status >= 400:
                    LOGGER.warning(
                        "图片下载失败 HTTP %s，回退原始 URL: %s",
                        response.status,
                        url[:120],
                    )
                    return [url], 0
                content_type = (
                    response.headers.get("Content-Type") or ""
                ).split(";")[0].strip().lower()
                data, exceeded = await read_limited(response, max_bytes)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            LOGGER.warning("图片下载失败，回退原始 URL: %s", exc)
            return [url], 0

        if not data:
            return [url], 0
        if exceeded:
            LOGGER.warning(
                "图片超过 %sMB，回退原始 URL: %s",
                self._settings.max_local_image_mb,
                url[:120],
            )
            return [url], 0

        is_gif = data.startswith((b"GIF87a", b"GIF89a"))
        if is_gif:
            content_type = "image/gif"
        elif not content_type.startswith("image/"):
            guessed = mimetypes.guess_type(urlparse(url).path)[0]
            content_type = (
                guessed
                if guessed and guessed.startswith("image/")
                else "image/jpeg"
            )

        if is_gif:
            label = urlparse(url).path.rsplit("/", 1)[-1][:40] or "remote.gif"
            frames = await asyncio.to_thread(
                sample_gif_frames_bytes, data, label=label
            )
            if frames:
                LOGGER.info(
                    "GIF 图片转为 %s 帧内联数据 | %s",
                    len(frames),
                    url[:120],
                )
                return (
                    [
                        f"data:{frame_mime};base64,"
                        f"{base64.b64encode(frame_data).decode('ascii')}"
                        for frame_data, frame_mime in frames
                    ],
                    len(frames),
                )

        LOGGER.info(
            "图片已转为内联数据 %sKB | %s", len(data) // 1024, url[:120]
        )
        encoded = base64.b64encode(data).decode("ascii")
        return [f"data:{content_type};base64,{encoded}"], 0

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        purpose: str,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._settings.ai_enable_thinking is not None:
            payload["enable_thinking"] = self._settings.ai_enable_thinking
        headers = {
            "Authorization": f"Bearer {self._settings.ai_api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        last_error = "未知错误"
        # Transient upstream errors ("Inference engine abort", 429, 5xx) are
        # common enough that one retry is not always enough.  Three attempts
        # with a short backoff keeps an @mention from being silently dropped.
        attempts = 3
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                async with self._session.post(
                    self._endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._settings.ai_timeout),
                ) as response:
                    status = response.status
                    raw = await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"网络错误: {exc}"
                if last_attempt:
                    raise AIError(last_error) from exc
                delay = 1.0 + attempt
                LOGGER.warning(
                    "AI 请求失败，%.0f 秒后重试（第 %s/%s 次）：%s",
                    delay,
                    attempt + 1,
                    attempts,
                    exc,
                )
                await asyncio.sleep(delay)
                continue

            if status in {429, 500, 502, 503, 504} and not last_attempt:
                detail = _extract_error(raw)
                last_error = f"HTTP {status}: {detail}"
                delay = 1.0 + attempt
                LOGGER.warning(
                    "AI 返回 HTTP %s，%.0f 秒后重试（第 %s/%s 次）：%s",
                    status,
                    delay,
                    attempt + 1,
                    attempts,
                    detail[:120],
                )
                await asyncio.sleep(delay)
                continue
            if status >= 400:
                detail = _extract_error(raw)
                raise AIError(f"HTTP {status}: {detail}")

            try:
                data = json.loads(raw) if raw.strip() else {}
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise AIError(f"响应格式异常: {raw[:200]}") from exc

            content = str(content or "").strip()
            if not content:
                raise AIError("模型返回了空内容")
            elapsed = time.monotonic() - started
            usage = data.get("usage") or {}
            LOGGER.info(
                "AI %s完成 model=%s 耗时=%.2fs tokens=%s/%s",
                purpose,
                data.get("model", model),
                elapsed,
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
            )
            return content

        raise AIError(last_error)


def _parse_meme_caption(raw: str) -> MemeCaption:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    data: Any = None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            data = None

    if not isinstance(data, dict):
        description = text.splitlines()[0].strip()[:80] if text else ""
        return MemeCaption(description, ())

    description = str(data.get("description") or "").strip()[:80]
    raw_tags = data.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            value = str(tag).strip()
            if value and value not in tags:
                tags.append(value[:16])
    raw_is_meme = data.get("is_meme")
    if isinstance(raw_is_meme, bool):
        is_meme = raw_is_meme
    elif isinstance(raw_is_meme, str):
        is_meme = raw_is_meme.strip().lower() not in {
            "false",
            "0",
            "no",
            "否",
            "不是",
        }
    else:
        is_meme = True

    emotion = str(data.get("emotion") or "").strip()[:12]
    raw_use_when = data.get("use_when")
    use_when: list[str] = []
    if isinstance(raw_use_when, list):
        for phrase in raw_use_when:
            value = str(phrase).strip()
            if value and value not in use_when:
                use_when.append(value[:24])
    risk = str(data.get("risk") or "low").strip().lower()
    risk = {"低": "low", "中": "medium", "高": "high"}.get(risk, risk)
    if risk not in {"low", "medium", "high"}:
        risk = "low"
    return MemeCaption(
        description,
        tuple(tags[:6]),
        is_meme,
        emotion,
        tuple(use_when[:4]),
        risk,
    )


def _parse_decision(raw: str) -> ImportanceDecision:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    data: Any = None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            data = None

    if not isinstance(data, dict):
        lowered = text.lower()
        if '"reply"' in lowered and "true" in lowered:
            return ImportanceDecision(should_reply=True, reason=text[:40])
        if '"reply"' in lowered and "false" in lowered:
            return ImportanceDecision(should_reply=False, reason=text[:40])
        raise AIError(f"重要性判断结果无法解析: {raw[:200]}")

    raw_reply = data.get("reply", data.get("should_reply"))
    if isinstance(raw_reply, str):
        should_reply = raw_reply.strip().lower() in {"1", "true", "yes", "是"}
    else:
        should_reply = bool(raw_reply)

    score = data.get("score", 0)
    try:
        score_value = int(score)
    except (TypeError, ValueError):
        score_value = 0

    reason = str(data.get("reason") or "").strip()[:80]
    return ImportanceDecision(
        should_reply=should_reply,
        score=max(0, min(100, score_value)),
        reason=reason,
    )


def _extract_error(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return raw[:300]