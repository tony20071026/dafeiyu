from __future__ import annotations

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

from config import Settings
from handlers.message_handler import (
    IncomingMessage,
    MessageHandler,
    _is_meme_request,
)
from utils.ai import AIError, ImportanceDecision
from utils.api import QQApiError
from utils.context_window import ContextWindowManager


class FakeApi:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str, str, str]] = []
        self.images: list[tuple[str, str, str, str]] = []
        self.c2c_texts: list[tuple[str, str, str]] = []
        self.c2c_images: list[tuple[str, str, str]] = []

    async def send_group_text(self, target: str, content: str, message_id: str) -> dict:
        self.texts.append(("group", target, content, message_id))
        return {}

    async def send_channel_text(
        self, target: str, content: str, message_id: str
    ) -> dict:
        self.texts.append(("channel", target, content, message_id))
        return {}

    async def send_group_image(
        self, target: str, image: str | Path, message_id: str
    ) -> dict:
        self.images.append(("group", target, str(image), message_id))
        return {}

    async def send_channel_image(
        self, target: str, image: str | Path, message_id: str
    ) -> dict:
        self.images.append(("channel", target, str(image), message_id))
        return {}

    async def send_c2c_text(
        self, target: str, content: str, message_id: str
    ) -> dict:
        self.c2c_texts.append((target, content, message_id))
        return {}

    async def send_c2c_image(
        self, target: str, image: str | Path, message_id: str
    ) -> dict:
        self.c2c_images.append((target, str(image), message_id))
        return {}


def _message(
    content: str,
    scene: str = "group",
    *,
    mentioned: bool = False,
    image_urls: tuple[str, ...] = (),
    quoted_author: str = "",
    quoted_content: str = "",
    quoted_image_urls: tuple[str, ...] = (),
) -> IncomingMessage:
    return IncomingMessage(
        scene=scene,  # type: ignore[arg-type]
        message_id="MSG-1",
        target_id="TARGET-1",
        conversation_id=f"{scene}:TARGET-1",
        content=content,
        sender_id="USER-1",
        sender_name="测试用户",
        mentioned=mentioned,
        image_urls=image_urls,
        quoted_author=quoted_author,
        quoted_content=quoted_content,
        quoted_image_urls=quoted_image_urls,
    )


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        assets = self.root / "assets"
        (assets / "memes").mkdir(parents=True)
        (assets / "memes" / "test.png").write_bytes(b"not-a-real-png")
        self.settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=assets,
            data_dir=self.root / "data",
            max_local_image_mb=5,
        )
        self.contexts = ContextWindowManager(
            self.root / "context.json", default_size=5_000
        )
        await self.contexts.load()
        self.api = FakeApi()
        self.handler = MessageHandler(self.settings, self.api, self.contexts)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_ping_carries_message_id(self) -> None:
        await self.handler.handle(_message("/ping"))
        self.assertEqual(
            self.api.texts,
            [("group", "TARGET-1", "pong! 大肥鱼已在线", "MSG-1")],
        )

    async def test_mute_blocks_replies_until_allowed(self) -> None:
        await self.handler.handle(_message("闭嘴", mentioned=True))
        self.assertEqual(len(self.api.texts), 1)
        self.assertIn("闭嘴十分钟", self.api.texts[-1][2])

        await self.handler.handle(_message("/ping", mentioned=True))
        self.assertEqual(len(self.api.texts), 1)

        await self.handler.handle(_message("允许说话", mentioned=True))
        self.assertEqual(len(self.api.texts), 2)
        self.assertIn("回来", self.api.texts[-1][2])

        await self.handler.handle(_message("/ping", mentioned=True))
        self.assertEqual(self.api.texts[-1][2], "pong! 大肥鱼已在线")

    async def test_mute_requires_at_and_expires(self) -> None:
        await self.handler.handle(_message("闭嘴"))
        self.assertEqual(self.api.texts, [])

        await self.handler.handle(_message("闭嘴", mentioned=True))
        self.assertEqual(len(self.api.texts), 1)

        self.handler._mute_until["group:TARGET-1"] = time.monotonic() - 1
        await self.handler.handle(_message("/ping", mentioned=True))
        self.assertEqual(self.api.texts[-1][2], "pong! 大肥鱼已在线")

    async def test_mute_only_affects_its_group(self) -> None:
        await self.handler.handle(_message("闭嘴", mentioned=True))
        other = dataclasses.replace(
            _message("/ping", mentioned=True),
            message_id="MSG-2",
            target_id="TARGET-2",
            conversation_id="group:TARGET-2",
        )
        await self.handler.handle(other)
        self.assertEqual(self.api.texts[-1][2], "pong! 大肥鱼已在线")

    async def test_unmute_variants_work(self) -> None:
        await self.handler.handle(_message("闭嘴", mentioned=True))
        await self.handler.handle(_message("别闭嘴了", mentioned=True))
        self.assertIn("回来", self.api.texts[-1][2])

        await self.handler.handle(_message("闭嘴", mentioned=True))
        await self.handler.handle(_message("可以说话", mentioned=True))
        self.assertIn("回来", self.api.texts[-1][2])

    async def test_water_timer_sends_one_message_after_idle_hours(self) -> None:
        await self.handler.handle(_message("/ping", mentioned=True))
        self.api.texts.clear()
        self.handler._group_last_spoke["group:TARGET-1"] = (
            time.monotonic() - self.settings.group_water_hours * 3600 - 1
        )

        sent = await self.handler.water_idle_groups()
        self.assertEqual(sent, 1)
        self.assertEqual(self.api.texts[-1][0], "group")
        self.assertEqual(self.api.texts[-1][1], "TARGET-1")
        self.assertEqual(self.api.texts[-1][3], "")

        # The timestamp was refreshed, so the next tick must stay quiet.
        self.assertEqual(await self.handler.water_idle_groups(), 0)
        self.assertEqual(len(self.api.texts), 1)

    async def test_water_timer_is_per_group(self) -> None:
        await self.handler.handle(_message("/ping", mentioned=True))
        other = dataclasses.replace(
            _message("/ping", mentioned=True),
            message_id="MSG-2",
            target_id="TARGET-2",
            conversation_id="group:TARGET-2",
        )
        await self.handler.handle(other)
        self.api.texts.clear()
        self.handler._group_last_spoke["group:TARGET-1"] = (
            time.monotonic() - self.settings.group_water_hours * 3600 - 1
        )

        self.assertEqual(await self.handler.water_idle_groups(), 1)
        self.assertEqual([text[1] for text in self.api.texts], ["TARGET-1"])

    async def test_water_timer_skips_muted_groups(self) -> None:
        await self.handler.handle(_message("闭嘴", mentioned=True))
        self.api.texts.clear()
        self.handler._group_last_spoke["group:TARGET-1"] = (
            time.monotonic() - self.settings.group_water_hours * 3600 - 1
        )

        self.assertEqual(await self.handler.water_idle_groups(), 0)
        self.assertEqual(self.api.texts, [])

    async def test_water_timer_does_not_retry_after_send_failure(self) -> None:
        class FailingApi(FakeApi):
            def __init__(self) -> None:
                super().__init__()
                self.group_calls = 0

            async def send_group_text(
                self, target: str, content: str, message_id: str
            ) -> dict:
                self.group_calls += 1
                raise QQApiError("boom")

        api = FailingApi()
        handler = MessageHandler(self.settings, api, self.contexts)
        await handler.handle(_message("/ping", mentioned=True))
        api.group_calls = 0
        handler._group_last_spoke["group:TARGET-1"] = (
            time.monotonic() - self.settings.group_water_hours * 3600 - 1
        )

        self.assertEqual(await handler.water_idle_groups(), 0)
        self.assertEqual(api.group_calls, 1)
        self.assertEqual(await handler.water_idle_groups(), 0)
        self.assertEqual(api.group_calls, 1)

    async def test_group_image_is_passed_to_ai(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.image_calls: list[tuple[str, ...]] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.image_calls.append(tuple(images))
                return "看到啦（歪头）"

        ai = FakeAI()
        handler = MessageHandler(
            self.settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            _message(
                "[图片]",
                mentioned=True,
                image_urls=("https://example.com/cat.png",),
            )
        )
        self.assertEqual(
            ai.image_calls, [("https://example.com/cat.png",)]
        )
        self.assertEqual(self.api.texts[-1][2], "看到啦（歪头）")

    async def test_followup_question_reuses_recent_image(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.image_calls: list[tuple[str, ...]] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.image_calls.append(tuple(images))
                return "看到了"

        ai = FakeAI()
        handler = MessageHandler(
            self.settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            _message(
                "[图片]",
                mentioned=True,
                image_urls=("https://example.com/fish.png",),
            )
        )
        await handler.handle(_message("这张图写了啥？", mentioned=True))
        self.assertEqual(
            ai.image_calls,
            [
                ("https://example.com/fish.png",),
                ("https://example.com/fish.png",),
            ],
        )

    async def test_quoted_message_is_added_to_ai_context(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.contexts.append(context_text)
                return "他说你只会这一招（歪头）"

        ai = FakeAI()
        handler = MessageHandler(
            self.settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            _message(
                "他这句话说的是什么？",
                mentioned=True,
                quoted_author="Tony1026",
                quoted_content="tm只会这个是吧",
            )
        )
        self.assertIn(
            "引用的是 Tony1026 的这条消息：「tm只会这个是吧」",
            ai.contexts[-1],
        )

    async def test_unresolved_quote_tells_model_not_to_guess(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.contexts.append(context_text)
                return "看不到引用原文"

        ai = FakeAI()
        handler = MessageHandler(
            self.settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-Q",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="这句话什么意思？",
                sender_id="USER-1",
                sender_name="Tony1026",
                mentioned=True,
                quoted_index="UNKNOWN-INDEX",
            )
        )
        self.assertIn("没有带被引用的原文", ai.contexts[-1])
        self.assertIn("不要拿聊天记录里的上一条作答", ai.contexts[-1])

    async def test_quoted_image_is_passed_to_ai(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.image_calls: list[tuple[str, ...]] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.image_calls.append(tuple(images))
                return "看到了"

        ai = FakeAI()
        handler = MessageHandler(
            self.settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            _message(
                "这张图是什么？",
                mentioned=True,
                quoted_image_urls=("https://example.com/quoted.png",),
            )
        )
        self.assertEqual(
            ai.image_calls, [("https://example.com/quoted.png",)]
        )

    async def test_help_mentions_context_options(self) -> None:
        await self.handler.handle(_message("/help"))
        reply = self.api.texts[-1][2]
        self.assertIn("/表情包", reply)
        self.assertIn("/context", reply)
        self.assertIn("/reset", reply)
        self.assertIn("5w", reply)

    async def test_context_switch(self) -> None:
        await self.handler.handle(_message("/context 1w"))
        self.assertEqual(self.contexts.limit_for("group:TARGET-1"), 10_000)
        self.assertIn("上下文窗口已调整", self.api.texts[-1][2])

    async def test_reset_clears_context(self) -> None:
        await self.handler.handle(_message("之前的聊天内容"))
        self.assertIn("之前的聊天内容", self.contexts.build("group:TARGET-1"))
        await self.handler.handle(_message("/reset"))
        self.assertEqual(self.contexts.build("group:TARGET-1"), "")
        self.assertIn("已清空", self.api.texts[-1][2])

    async def test_keyword(self) -> None:
        await self.handler.handle(_message("你好呀"))
        self.assertTrue(self.api.texts)
        self.assertIn("大肥鱼", self.api.texts[-1][2])

    async def test_meme_and_network_image(self) -> None:
        await self.handler.handle(_message("/meme"))
        self.assertEqual(self.api.images[-1][0], "group")
        self.assertTrue(self.api.images[-1][2].endswith("test.png"))

        await self.handler.handle(
            _message("/image https://example.com/cat.png", scene="channel")
        )
        self.assertEqual(self.api.images[-1][:3], ("channel", "TARGET-1", "https://example.com/cat.png"))

        await self.handler.handle(
            _message("https://example.com/dog.png", scene="channel")
        )
        self.assertEqual(self.api.images[-1][:3], ("channel", "TARGET-1", "https://example.com/dog.png"))

    async def test_invalid_image_url_is_rejected(self) -> None:
        await self.handler.handle(_message("/image file:///etc/passwd"))
        self.assertIn("合法", self.api.texts[-1][2])
        self.assertFalse(self.api.images)

    async def test_c2c_ping_and_meme_without_at(self) -> None:
        await self.handler.handle(_message("/ping", scene="c2c"))
        self.assertEqual(
            self.api.c2c_texts,
            [("TARGET-1", "pong! 大肥鱼已在线", "MSG-1")],
        )

        await self.handler.handle(_message("/meme", scene="c2c"))
        self.assertEqual(self.api.c2c_images[0][0], "TARGET-1")
        self.assertTrue(self.api.c2c_images[0][1].endswith("test.png"))

        await self.handler.handle(_message("发个表情包看看", scene="c2c"))
        self.assertEqual(self.api.c2c_images[1][0], "TARGET-1")
        self.assertTrue(self.api.c2c_images[1][1].endswith("test.png"))

    async def test_meme_question_is_not_a_meme_request(self) -> None:
        self.assertFalse(_is_meme_request("这个表情包是什么意思？"))
        self.assertFalse(_is_meme_request("你能看懂这个表情包吗"))
        self.assertFalse(_is_meme_request("表情包"))
        self.assertTrue(_is_meme_request("发个表情包看看"))
        self.assertTrue(_is_meme_request("来张表情"))
        self.assertTrue(_is_meme_request("send me a meme"))

    async def test_meme_question_attaches_recent_image(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.images: list[tuple[str, ...]] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.images.append(tuple(images))
                return "这是一张表情包"

        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            group_reply_judge=False,
        )
        ai = FakeAI()
        handler = MessageHandler(
            settings, self.api, self.contexts, ai  # type: ignore[arg-type]
        )
        await handler.handle(
            _message(
                "[图片]", image_urls=("https://example.com/meme.gif",)
            )
        )
        await handler.handle(
            _message("这个表情包是什么意思？", mentioned=True)
        )
        self.assertEqual(ai.images[-1], ("https://example.com/meme.gif",))
        # A question must not trigger the random-meme sender.
        self.assertEqual(self.api.images, [])

    async def test_ai_replaces_fixed_keyword_reply(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            async def chat(self, context_text: str) -> str:
                self.contexts.append(context_text)
                return "AI 来啦"

        ai = FakeAI()
        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            ai_enabled=True,
            fixed_replies_enabled=False,
        )
        handler = MessageHandler(settings, self.api, self.contexts, ai)  # type: ignore[arg-type]
        await handler.handle(_message("你好呀", scene="c2c"))
        self.assertEqual(
            self.api.c2c_texts[-1],
            ("TARGET-1", "AI 来啦", "MSG-1"),
        )
        self.assertIn("你好呀", ai.contexts[0])
        self.assertIn("AI 来啦", self.contexts.build("c2c:TARGET-1"))

    def _judge_handler(self, decision):
        class JudgeAI:
            def __init__(self) -> None:
                self.judge_calls: list[tuple[str, str, str]] = []
                self.chat_calls: list[str] = []
                self.interject_calls: list[str] = []

            async def judge(self, context_text, message_text, *, sender_name=""):
                self.judge_calls.append((context_text, message_text, sender_name))
                if isinstance(decision, Exception):
                    raise decision
                return decision

            async def chat(self, context_text: str) -> str:
                self.chat_calls.append(context_text)
                return "这条值得聊"

            async def interject(self, context_text: str) -> str:
                self.interject_calls.append(context_text)
                return "潜水太久，出来冒个泡"

        ai = JudgeAI()
        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            ai_enabled=True,
            fixed_replies_enabled=False,
            group_reply_judge=True,
        )
        handler = MessageHandler(settings, self.api, self.contexts, ai)  # type: ignore[arg-type]
        return handler, ai

    async def test_group_skips_message_when_ai_says_unimportant(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=10, reason="群友闲聊")
        )
        await handler.handle(_message("今天中午吃什么"))
        self.assertEqual(len(ai.judge_calls), 1)
        self.assertEqual(ai.chat_calls, [])
        self.assertEqual(self.api.texts, [])

    async def test_group_replies_when_ai_says_important(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=True, score=85, reason="在问机器人")
        )
        await handler.handle(_message("大肥鱼你觉得呢"))
        self.assertEqual(len(ai.judge_calls), 1)
        self.assertEqual(self.api.texts[-1][2], "这条值得聊")

    async def test_group_cooldown_prevents_burst_replies(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=True, score=90, reason="值得回复")
        )
        await handler.handle(_message("那这个呢"))
        await handler.handle(_message("还有这个呢"))
        self.assertEqual(len(ai.judge_calls), 2)
        self.assertEqual(len(ai.chat_calls), 1)
        self.assertEqual(len(self.api.texts), 1)

    async def test_group_interjects_after_fifteen_silent_messages(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=10, reason="闲聊")
        )
        for index in range(14):
            await handler.handle(_message(f"闲聊 {index}"))
        self.assertEqual(ai.interject_calls, [])
        self.assertEqual(self.api.texts, [])

        await handler.handle(_message("第十五句"))
        self.assertEqual(len(ai.interject_calls), 1)
        self.assertEqual(len(ai.judge_calls), 14)
        self.assertEqual(self.api.texts[-1][2], "潜水太久，出来冒个泡")

    async def test_group_interject_counter_resets_after_reply(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=10, reason="闲聊")
        )
        for index in range(10):
            await handler.handle(_message(f"闲聊 {index}"))
        await handler.handle(_message("/ping"))
        self.assertEqual(self.api.texts[-1][2], "pong! 大肥鱼已在线")

        for index in range(14):
            await handler.handle(_message(f"第二轮 {index}"))
        self.assertEqual(ai.interject_calls, [])
        await handler.handle(_message("第二轮第十五句"))
        self.assertEqual(len(ai.interject_calls), 1)

    async def test_group_interject_can_be_disabled(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=10, reason="闲聊")
        )
        handler._settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            ai_enabled=True,
            group_idle_speak_every=0,
        )
        for index in range(20):
            await handler.handle(_message(f"闲聊 {index}"))
        self.assertEqual(ai.interject_calls, [])
        self.assertEqual(self.api.texts, [])

    async def test_at_messages_do_not_fill_silence_counter(self) -> None:
        class FailingAI:
            async def judge(self, context_text, message_text, *, sender_name=""):
                raise AssertionError("@ 消息不应进入判断")

            async def chat(self, context_text: str) -> str:
                raise AIError("boom")

        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            ai_enabled=True,
            group_reply_judge=True,
        )
        handler = MessageHandler(settings, self.api, self.contexts, FailingAI())  # type: ignore[arg-type]
        for _ in range(20):
            await handler.handle(_message("在吗", mentioned=True))
        self.assertEqual(handler._group_silence.get("group:TARGET-1", 0), 0)

    async def test_group_mention_bypasses_judge(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=0, reason="不该走到这里")
        )
        await handler.handle(_message("在吗", mentioned=True))
        self.assertEqual(ai.judge_calls, [])
        self.assertEqual(self.api.texts[-1][2], "这条值得聊")

    async def test_group_stays_silent_when_judge_fails(self) -> None:
        handler, ai = self._judge_handler(AIError("timed out"))
        await handler.handle(_message("随便聊聊"))
        self.assertEqual(len(ai.judge_calls), 1)
        self.assertEqual(self.api.texts, [])

    async def test_group_stays_silent_when_chat_fails(self) -> None:
        class FailingAI:
            async def judge(self, context_text, message_text, *, sender_name=""):
                return ImportanceDecision(should_reply=True, score=90, reason="值得")

            async def chat(self, context_text: str) -> str:
                raise AIError("boom")

        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            ai_enabled=True,
            group_reply_judge=True,
        )
        handler = MessageHandler(settings, self.api, self.contexts, FailingAI())  # type: ignore[arg-type]
        await handler.handle(_message("大肥鱼帮个忙"))
        self.assertEqual(self.api.texts, [])

    async def test_c2c_does_not_call_judge(self) -> None:
        handler, ai = self._judge_handler(
            ImportanceDecision(should_reply=False, score=0, reason="不该调用")
        )
        await handler.handle(_message("你好", scene="c2c"))
        self.assertEqual(ai.judge_calls, [])
        self.assertEqual(self.api.c2c_texts[-1][1], "这条值得聊")


class GatewayEventParsingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_group_full_and_c2c_events_are_parsed(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG",
                "group_openid": "GROUP",
                "content": "/ping",
                "author": {
                    "id": "U1",
                    "member_openid": "M1",
                    "username": "小明",
                    "bot": False,
                },
            },
        )
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-2",
                "group_openid": "GROUP",
                "content": "<@!BOTID> /help",
                "author": {
                    "id": "U1",
                    "member_openid": "M1",
                    "username": "小明",
                    "bot": False,
                },
            },
        )
        await gateway._handle_event(
            "C2C_MESSAGE_CREATE",
            {
                "id": "C-MSG",
                "content": "/help",
                "author": {
                    "id": "U2",
                    "user_openid": "USER2",
                    "username": "小红",
                    "bot": False,
                },
            },
        )
        self.assertEqual(handler.messages[0].scene, "group")
        self.assertEqual(handler.messages[0].message_id, "G-MSG")
        self.assertFalse(handler.messages[0].mentioned)
        self.assertTrue(handler.messages[1].mentioned)
        self.assertEqual(handler.messages[1].content, "/help")
        self.assertEqual(handler.messages[2].scene, "c2c")
        self.assertEqual(handler.messages[2].target_id, "USER2")
        self.assertEqual(handler.messages[2].conversation_id, "c2c:USER2")
        self.assertTrue(handler.messages[2].mentioned)

    async def test_group_mention_of_known_member_keeps_name(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-3",
                "group_openid": "GROUP",
                "content": "我先开个嗓",
                "author": {
                    "id": "U3",
                    "member_openid": "BRUCE1",
                    "username": "落阳深处的布鲁斯",
                    "bot": False,
                },
            },
        )
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-4",
                "group_openid": "GROUP",
                "content": "<@!BRUCE1> 是谁？",
                "author": {
                    "id": "U4",
                    "member_openid": "LI1",
                    "username": "李",
                    "bot": False,
                },
            },
        )
        self.assertEqual(handler.messages[-1].content, "@落阳深处的布鲁斯 是谁？")

    async def test_leading_unknown_mention_is_learned_as_bot(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-5",
                "group_openid": "GROUP",
                "content": "<@!BOTOPENID> 我们萨克斯手是谁？",
                "author": {
                    "id": "U5",
                    "member_openid": "TONY1",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        message = handler.messages[-1]
        self.assertTrue(message.mentioned)
        self.assertEqual(message.content, "我们萨克斯手是谁？")
        self.assertIn("BOTOPENID", gateway._bot_mentions)

    async def test_bot_mention_guess_is_removed_when_it_speaks(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-6",
                "group_openid": "GROUP",
                "content": "<@!NEWBIE1> 你好呀",
                "author": {
                    "id": "U6",
                    "member_openid": "TONY1",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        self.assertIn("NEWBIE1", gateway._bot_mentions)

        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-7",
                "group_openid": "GROUP",
                "content": "大家好",
                "author": {
                    "id": "U7",
                    "member_openid": "NEWBIE1",
                    "username": "新群友",
                    "bot": False,
                },
            },
        )
        self.assertNotIn("NEWBIE1", gateway._bot_mentions)
        self.assertEqual(gateway._member_names.get("NEWBIE1"), "新群友")

    async def test_group_image_attachment_is_parsed(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-8",
                "group_openid": "GROUP",
                "content": "",
                "attachments": [
                    {
                        "content_type": "image/png",
                        "filename": "cat.png",
                        "url": "https://example.com/cat.png",
                        "size": 1234,
                    }
                ],
                "author": {
                    "id": "U8",
                    "member_openid": "TONY1",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        message = handler.messages[-1]
        self.assertEqual(message.content, "[图片]")
        self.assertEqual(
            message.image_urls, ("https://example.com/cat.png",)
        )

    async def test_group_quote_reply_is_parsed(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-9",
                "group_openid": "GROUP",
                "content": "他这句话说的是什么？",
                "message_scene": {
                    "source": "default",
                    "ext": [
                        "msg_idx=REFIDX_NEW",
                        "ref_msg_idx=REFIDX_OLD",
                    ],
                },
                "msg_elements": [
                    {
                        "message_type": 103,
                        "author": {"username": "Tony1026"},
                        "content": "tm只会这个是吧",
                        "attachments": [
                            {
                                "content_type": "image/png",
                                "filename": "old.png",
                                "url": "https://example.com/old.png",
                            }
                        ],
                    }
                ],
                "author": {
                    "id": "U9",
                    "member_openid": "TONY1",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        message = handler.messages[-1]
        self.assertEqual(message.quoted_author, "Tony1026")
        self.assertEqual(message.quoted_content, "tm只会这个是吧")
        self.assertEqual(message.quoted_index, "REFIDX_OLD")
        self.assertEqual(
            message.quoted_image_urls, ("https://example.com/old.png",)
        )

    async def test_event_level_quote_type_is_collected(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        # Some clients mark the whole event as 103 and put the quoted text in
        # msg_elements without a per-element message_type.
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-10",
                "group_openid": "GROUP",
                "content": "这句话什么意思？",
                "message_type": 103,
                "message_scene": {
                    "ext": ["msg_idx=NEW", "ref_msg_idx=OLD"],
                },
                "msg_elements": [
                    {
                        "author": {"username": "李"},
                        "content": "今天天气不错",
                    }
                ],
                "author": {
                    "id": "U10",
                    "member_openid": "USER10",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        message = handler.messages[-1]
        self.assertEqual(message.quoted_author, "李")
        self.assertEqual(message.quoted_content, "今天天气不错")
        self.assertEqual(message.quoted_index, "OLD")

    async def test_nested_quote_element_uses_wrapper_author(self) -> None:
        from utils.gateway import QQGatewayClient

        class Handler:
            def __init__(self) -> None:
                self.messages = []

            async def handle(self, message) -> None:
                self.messages.append(message)

        handler = Handler()
        gateway = QQGatewayClient(
            Settings(
                app_id="test",
                app_secret="test",
                data_dir=self.data_dir,
            ),
            api=None,  # type: ignore[arg-type]
            tokens=None,  # type: ignore[arg-type]
            handler=handler,  # type: ignore[arg-type]
        )
        gateway._bot_id = "BOTID"
        await gateway._handle_event(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "G-MSG-11",
                "group_openid": "GROUP",
                "content": "解释下",
                "message_scene": {"ext": ["msg_idx=N", "ref_msg_idx=O"]},
                "msg_elements": [
                    {
                        "message_type": 103,
                        "author": {"username": "小明"},
                        "msg_elements": [
                            {
                                "message_type": 1,
                                "content": "今天天气不错",
                            }
                        ],
                    }
                ],
                "author": {
                    "id": "U11",
                    "member_openid": "USER11",
                    "username": "Tony1026",
                    "bot": False,
                },
            },
        )
        message = handler.messages[-1]
        self.assertEqual(message.quoted_author, "小明")
        self.assertEqual(message.quoted_content, "今天天气不错")

    async def test_cache_resolves_quote_without_inline_content(self) -> None:
        class FakeAI:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            async def chat(self, context_text: str, *, images=()) -> str:
                self.contexts.append(context_text)
                return "好"

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=root / "assets",
            data_dir=root / "data",
        )
        contexts = ContextWindowManager(
            root / "context.json", default_size=5_000
        )
        await contexts.load()
        ai = FakeAI()
        handler = MessageHandler(
            settings, FakeApi(), contexts, ai  # type: ignore[arg-type]
        )
        first = _message("上一句引用目标", mentioned=True)
        first = dataclasses.replace(first, message_index="IDX-A")
        await handler.handle(first)
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-2",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="这句话什么意思？",
                sender_id="USER-2",
                sender_name="Tony1026",
                mentioned=True,
                message_index="IDX-B",
                quoted_index="IDX-A",
            )
        )
        self.assertIn(
            "（Tony1026 引用的是 测试用户 的这条消息：「上一句引用目标」）",
            ai.contexts[-1],
        )


if __name__ == "__main__":
    unittest.main()
