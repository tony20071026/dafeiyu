from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from PIL import Image

from config import Settings
from utils.ai import (
    AIClient,
    _parse_meme_caption,
    build_member_alias_directive,
)


class AIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_completion_request_and_response(self) -> None:
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = await request.json()
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": "你好呀"}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_judge_model="judge-model",
                ai_enabled=True,
                ai_timeout=5,
                ai_enable_thinking=False,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                reply = await client.chat("用户: 你好")
        finally:
            await runner.cleanup()

        self.assertEqual(reply, "你好呀")
        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(captured["body"]["model"], "chat-model")
        self.assertFalse(captured["body"]["stream"])
        self.assertIs(captured["body"]["enable_thinking"], False)
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")
        self.assertIn("大白饭", captured["body"]["messages"][0]["content"])
        self.assertIn("tony1026", captured["body"]["messages"][0]["content"])
        self.assertIn("括号", captured["body"]["messages"][0]["content"])
        self.assertIn("用户: 你好", captured["body"]["messages"][1]["content"])

    async def test_chat_with_images_sends_multimodal_content(self) -> None:
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": "我看到了"}}
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_enabled=True,
                ai_timeout=5,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                reply = await client.chat(
                    "用户: [图片]",
                    images=("data:image/png;base64,AAAA",),
                )
        finally:
            await runner.cleanup()

        self.assertEqual(reply, "我看到了")
        content = captured["body"]["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(
            content[1]["image_url"]["url"], "data:image/png;base64,AAAA"
        )

    async def test_image_payload_reads_all_streamed_chunks(self) -> None:
        image = b"a" * 16384 + b"b" * 20000 + b"c" * 5000

        async def handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Length": str(len(image)),
                }
            )
            await response.prepare(request)
            for index in range(0, len(image), 16384):
                await response.write(image[index:index + 16384])
                await asyncio.sleep(0.02)
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/image.jpg", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_enabled=True,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                payload = await client._image_payload(
                    f"http://127.0.0.1:{port}/image.jpg"
                )
        finally:
            await runner.cleanup()

        prefix = "data:image/jpeg;base64,"
        self.assertTrue(payload.startswith(prefix))
        self.assertEqual(
            base64.b64decode(payload[len(prefix):]), image
        )

    async def test_describe_gif_sends_four_sampled_frames(self) -> None:
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"description": "多帧动作", '
                                    '"tags": ["动图", "测试"]}'
                                ),
                            }
                        }
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_enabled=True,
                ai_timeout=5,
            )
            with tempfile.TemporaryDirectory() as temp:
                gif_path = Path(temp) / "motion.gif"
                frames = [
                    Image.new("RGB", (32, 32), (index * 30, 0, 0))
                    for index in range(8)
                ]
                frames[0].save(
                    gif_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=50,
                    loop=0,
                )
                async with aiohttp.ClientSession() as session:
                    client = AIClient(settings, session)
                    caption = await client.describe_image(gif_path)
        finally:
            await runner.cleanup()

        self.assertEqual(caption.description, "多帧动作")
        self.assertEqual(caption.tags, ("动图", "测试"))
        self.assertTrue(caption.is_meme)
        content = captured["body"]["messages"][1]["content"]
        image_parts = [
            part for part in content if part.get("type") == "image_url"
        ]
        self.assertEqual(len(image_parts), 4)
        for part in image_parts:
            self.assertTrue(
                part["image_url"]["url"].startswith(
                    "data:image/jpeg;base64,"
                )
            )

    async def test_gif_image_payload_expands_to_sampled_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gif_path = Path(temp) / "motion.gif"
            frames = [
                Image.new("RGB", (64, 64), (index * 30, 0, 0))
                for index in range(8)
            ]
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=50,
                loop=0,
            )
            gif_bytes = gif_path.read_bytes()

        async def handler(request: web.Request) -> web.Response:
            return web.Response(body=gif_bytes, content_type="image/gif")

        app = web.Application()
        app.router.add_get("/motion.gif", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_enabled=True,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                payloads, gif_frames = await client._image_payloads(
                    [f"http://127.0.0.1:{port}/motion.gif"]
                )
        finally:
            await runner.cleanup()

        self.assertEqual(gif_frames, 4)
        self.assertEqual(len(payloads), 4)
        for payload in payloads:
            self.assertTrue(
                payload.startswith("data:image/jpeg;base64,")
            )

    async def test_chat_gif_mentions_sampled_frames(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as temp:
            gif_path = Path(temp) / "motion.gif"
            frames = [
                Image.new("RGB", (48, 48), (index * 30, 0, 0))
                for index in range(8)
            ]
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=50,
                loop=0,
            )
            gif_bytes = gif_path.read_bytes()

        async def gif_handler(request: web.Request) -> web.Response:
            return web.Response(body=gif_bytes, content_type="image/gif")

        async def ai_handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": "看到了"}}
                    ],
                }
            )

        app = web.Application()
        app.router.add_get("/motion.gif", gif_handler)
        app.router.add_post("/chat/completions", ai_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_enabled=True,
                ai_timeout=5,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                reply = await client.chat(
                    "用户: [图片]",
                    images=(f"http://127.0.0.1:{port}/motion.gif",),
                )
        finally:
            await runner.cleanup()

        self.assertEqual(reply, "看到了")
        content = captured["body"]["messages"][1]["content"]
        self.assertIn("按时间顺序抽取 4 帧", content[0]["text"])
        image_parts = [
            part for part in content if part.get("type") == "image_url"
        ]
        self.assertEqual(len(image_parts), 4)

    async def test_judge_parses_json_decision(self) -> None:
        captured: dict = {}

        async def handler(request: web.Request) -> web.Response:
            captured["body"] = await request.json()
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "```json\n"
                                    '{"reply": false, "score": 12, '
                                    '"reason": "群友闲聊"}\n'
                                    "```"
                                )
                            }
                        }
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_judge_model="judge-model",
                ai_enabled=True,
                ai_timeout=5,
                group_judge_context_chars=500,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                decision = await client.judge(
                    "小明: 今天中午吃什么\n小红: 随便",
                    "今天中午吃什么",
                    sender_name="小明",
                )
        finally:
            await runner.cleanup()

        self.assertFalse(decision.should_reply)
        self.assertEqual(decision.score, 12)
        self.assertEqual(decision.reason, "群友闲聊")
        body = captured["body"]
        self.assertEqual(body["model"], "judge-model")
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["max_tokens"], 120)
        self.assertNotIn("enable_thinking", body)
        self.assertIn("宁可少说", body["messages"][0]["content"])
        self.assertIn("发送者：小明", body["messages"][1]["content"])


class MemeCaptionParserTests(unittest.TestCase):
    def test_parses_non_meme_verdict(self) -> None:
        caption = _parse_meme_caption(
            '{"is_meme": false, "description": "一盘三文鱼刺身", '
            '"tags": ["食物", "三文鱼"]}'
        )
        self.assertFalse(caption.is_meme)
        self.assertEqual(caption.description, "一盘三文鱼刺身")
        self.assertEqual(caption.tags, ("食物", "三文鱼"))

    def test_parses_emotion_scene_and_risk(self) -> None:
        caption = _parse_meme_caption(
            '{"is_meme": true, "emotion": "无语", '
            '"use_when": ["改 bug 改到崩溃", "周一上班"], '
            '"risk": "high", "description": "湿发呆滞男生", '
            '"tags": ["无语", "心累"]}'
        )
        self.assertTrue(caption.is_meme)
        self.assertEqual(caption.emotion, "无语")
        self.assertEqual(caption.use_when, ("改 bug 改到崩溃", "周一上班"))
        self.assertEqual(caption.risk, "high")

    def test_missing_verdict_defaults_to_meme(self) -> None:
        caption = _parse_meme_caption(
            '{"description": "夸张的搞笑表情", "tags": ["搞笑"]}'
        )
        self.assertTrue(caption.is_meme)
        self.assertEqual(caption.emotion, "")
        self.assertEqual(caption.use_when, ())
        self.assertEqual(caption.risk, "low")

    def test_member_alias_directive_prefers_real_names(self) -> None:
        directive = build_member_alias_directive({"李": "李小明"})
        self.assertIn("优先使用已知真名", directive)
        self.assertIn("李 -> 李小明", directive)
        self.assertEqual(build_member_alias_directive({}), "")


class MemberAliasPromptTests(unittest.TestCase):
    def test_alias_file_is_added_to_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            (data_dir / "member_aliases.json").write_text(
                '{"aliases": {"李": "李小明"}}', encoding="utf-8"
            )
            settings = Settings(
                app_id="app", app_secret="secret", data_dir=data_dir
            )
            client = AIClient(settings, None)  # type: ignore[arg-type]
            self.assertIn("李 -> 李小明", client._system_prompt)
            self.assertIn("优先使用已知真名", client._system_prompt)

    def test_call_as_overrides_real_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            (data_dir / "member_aliases.json").write_text(
                '{"aliases": {"某憨不憨": "某甲"},'
                ' "call_as": {"某甲": ["小甲", "甲甲"]}}',
                encoding="utf-8",
            )
            settings = Settings(
                app_id="app", app_secret="secret", data_dir=data_dir
            )
            client = AIClient(settings, None)  # type: ignore[arg-type]
            self.assertIn("某甲 -> 小甲 / 甲甲", client._system_prompt)
            self.assertIn("某憨不憨 -> 某甲（叫 小甲/甲甲）", client._system_prompt)
            self.assertIn("不要用全名", client._system_prompt)
            self.assertIn("任何时候都不要写出这些人的全名", client._system_prompt)

    def test_call_as_accepts_a_single_string(self) -> None:
        from utils.ai import load_call_as

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "member_aliases.json"
            path.write_text(
                '{"call_as": {"某甲": "小甲", "某乙": "  ", "某丙": 1}}',
                encoding="utf-8",
            )
            self.assertEqual(load_call_as(path), {"某甲": ["小甲"]})

    def test_missing_call_as_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            (data_dir / "member_aliases.json").write_text(
                '{"aliases": {"某憨不憨": "某甲"}}', encoding="utf-8"
            )
            settings = Settings(
                app_id="app", app_secret="secret", data_dir=data_dir
            )
            client = AIClient(settings, None)  # type: ignore[arg-type]
            self.assertIn("某憨不憨 -> 某甲", client._system_prompt)
            self.assertNotIn("固定叫法", client._system_prompt)


class DisplayNamesTests(unittest.TestCase):
    def setUp(self) -> None:
        from utils.ai import DisplayNames

        self.display = DisplayNames.build(
            {"某憨不憨": "某甲", "甲": "某甲"},
            {"某甲": ["小甲", "甲甲"]},
        )

    def test_full_name_becomes_preferred_nickname(self) -> None:
        self.assertEqual(
            self.display.apply("刚才是某甲说的"), "刚才是小甲说的"
        )

    def test_alias_becomes_preferred_nickname(self) -> None:
        self.assertEqual(self.display.apply("某憨不憨笑了"), "小甲笑了")

    def test_duplicate_parenthetical_is_collapsed(self) -> None:
        self.assertEqual(
            self.display.apply("某甲（小甲）刚才说的"), "小甲刚才说的"
        )

    def test_alternate_nickname_parenthetical_is_collapsed(self) -> None:
        self.assertEqual(
            self.display.apply("小甲（甲甲）来了"), "小甲来了"
        )

    def test_single_character_alias_is_not_rewritten(self) -> None:
        self.assertEqual(self.display.apply("甲"), "甲")

    def test_unrelated_parenthetical_is_kept(self) -> None:
        self.assertEqual(
            self.display.apply("小甲（甩尾巴）"), "小甲（甩尾巴）"
        )

    def test_no_call_as_is_a_noop(self) -> None:
        from utils.ai import DisplayNames

        empty = DisplayNames.build({"某憨不憨": "某甲"}, {})
        text = "某甲（某甲）说某甲"
        self.assertEqual(empty.apply(text), text)


class GroupContextPromptTests(unittest.TestCase):
    def test_group_context_file_is_appended(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            (data_dir / "group_context.txt").write_text(
                "# 注释不会进入提示词\n\n这个群是一个乐队群。\n",
                encoding="utf-8",
            )
            settings = Settings(
                app_id="app", app_secret="secret", data_dir=data_dir
            )
            client = AIClient(settings, None)  # type: ignore[arg-type]
            self.assertIn("【补充背景】", client._system_prompt)
            self.assertIn("这个群是一个乐队群。", client._system_prompt)
            self.assertNotIn("注释不会进入提示词", client._system_prompt)

    def test_missing_group_context_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(
                app_id="app", app_secret="secret", data_dir=Path(temp)
            )
            client = AIClient(settings, None)  # type: ignore[arg-type]
            self.assertNotIn("【补充背景】", client._system_prompt)

    def test_prompt_forbids_repeating_context(self) -> None:
        settings = Settings(app_id="app", app_secret="secret")
        client = AIClient(settings, None)  # type: ignore[arg-type]
        self.assertIn("不要复述", client._system_prompt)
        self.assertIn("小萝莉", client._system_prompt)


class AIRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_500_is_retried_three_times(self) -> None:
        from unittest.mock import AsyncMock, patch

        calls = 0

        async def handler(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return web.Response(
                    status=500,
                    text='{"error": {"message": "engine abort"}}',
                    content_type="application/json",
                )
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [{"message": {"content": "好了"}}],
                    "usage": {},
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            settings = Settings(
                app_id="app",
                app_secret="secret",
                ai_api_key="test-key",
                ai_base_url=f"http://127.0.0.1:{port}",
                ai_model="chat-model",
                ai_enabled=True,
                ai_timeout=5,
            )
            async with aiohttp.ClientSession() as session:
                client = AIClient(settings, session)
                with patch("utils.ai.asyncio.sleep", new=AsyncMock()):
                    reply = await client.chat("你好")
        finally:
            await runner.cleanup()

        self.assertEqual(reply, "好了")
        self.assertEqual(calls, 3)


class AIChatDisplayNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_output_never_contains_the_full_name(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "model": "chat-model",
                    "choices": [
                        {"message": {"content": "某甲（小甲）刚才说的"}}
                    ],
                    "usage": {},
                }
            )

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            with tempfile.TemporaryDirectory() as temp:
                data_dir = Path(temp)
                (data_dir / "member_aliases.json").write_text(
                    '{"aliases": {"某憨不憨": "某甲"},'
                    ' "call_as": {"某甲": ["小甲"]}}',
                    encoding="utf-8",
                )
                settings = Settings(
                    app_id="app",
                    app_secret="secret",
                    ai_api_key="test-key",
                    ai_base_url=f"http://127.0.0.1:{port}",
                    ai_model="chat-model",
                    ai_enabled=True,
                    ai_timeout=5,
                    data_dir=data_dir,
                )
                async with aiohttp.ClientSession() as session:
                    client = AIClient(settings, session)
                    reply = await client.chat("你好")
        finally:
            await runner.cleanup()

        self.assertEqual(reply, "小甲刚才说的")
        self.assertNotIn("某甲", reply)


if __name__ == "__main__":
    unittest.main()
