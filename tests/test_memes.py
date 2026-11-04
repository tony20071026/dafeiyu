from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from config import Settings
from handlers.message_handler import (
    IncomingMessage,
    MessageHandler,
    _extract_meme_marker,
    _manual_meme_save_alias,
    _meme_save_alias,
)
from utils.ai import MemeCaption
from utils.context_window import ContextWindowManager
from utils.memes import MemeLibrary, _gif_complete

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"
GIF_BYTES = b"GIF89a" + b"fake-gif-body"
COMPLETE_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


class FakeCaptionAI:
    def __init__(
        self,
        descriptions: list[tuple[str, tuple[str, ...]] | MemeCaption],
    ) -> None:
        self._descriptions = descriptions
        self.calls = 0

    async def describe_image(self, path: Path) -> MemeCaption:
        index = min(self.calls, len(self._descriptions) - 1)
        self.calls += 1
        item = self._descriptions[index]
        if isinstance(item, MemeCaption):
            return item
        return MemeCaption(item[0], item[1])


class MemeLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _library(self, ai=None) -> MemeLibrary:
        library = MemeLibrary(self.settings, ai_client=ai)
        await library.load()
        return library

    async def test_add_image_indexes_dedupes_and_persists(self) -> None:
        library = await self._library()
        entry = await library.add_bytes(PNG_BYTES, "image/png")
        self.assertIsNotNone(entry)
        assert entry is not None
        path = self.settings.memes_dir / entry.filename
        self.assertTrue(path.is_file())

        duplicate = await library.add_bytes(PNG_BYTES, "image/png")
        self.assertIsNone(duplicate)
        self.assertEqual(len(library), 1)

        reloaded = await self._library()
        self.assertEqual(len(reloaded), 1)
        self.assertIn(entry.filename, reloaded._entries)
        self.assertEqual(
            reloaded._entries[entry.filename].sha256, entry.sha256
        )

    async def test_manual_duplicate_files_are_deduped_by_hash(self) -> None:
        library = await self._library()
        directory = self.settings.memes_dir
        (directory / "a.png").write_bytes(PNG_BYTES)
        (directory / "b.png").write_bytes(PNG_BYTES)

        await library.refresh(max_age=0)

        self.assertEqual(len(library), 1)
        self.assertEqual(len(list(directory.glob("*.png"))), 1)
        entry = next(iter(library._entries.values()))
        self.assertEqual(entry.sha256, hashlib.sha256(PNG_BYTES).hexdigest())

    async def test_duplicate_with_other_suffix_is_removed(self) -> None:
        library = await self._library()
        directory = self.settings.memes_dir
        (directory / "a.png").write_bytes(PNG_BYTES)
        (directory / "b.jpg").write_bytes(PNG_BYTES)

        await library.refresh(max_age=0)

        self.assertEqual(len(library), 1)
        self.assertEqual(
            [path.name for path in directory.iterdir() if path.is_file()],
            ["a.png"],
        )

    async def test_duplicate_cleanup_keeps_described_entry(self) -> None:
        library = await self._library()
        entry = await library.add_bytes(PNG_BYTES, "image/png")
        assert entry is not None
        await library.apply_alias(entry, "测试表情")
        duplicate = self.settings.memes_dir / "manual_copy.png"
        duplicate.write_bytes(PNG_BYTES)

        await library.refresh(max_age=0)

        self.assertEqual(len(library), 1)
        self.assertTrue((self.settings.memes_dir / entry.filename).is_file())
        self.assertFalse(duplicate.exists())
        self.assertIn("测试表情", library._entries[entry.filename].description)

    async def test_ai_caption_is_used_for_topic_matching(self) -> None:
        ai = FakeCaptionAI(
            [
                ("开心的鲸鱼表情", ("开心", "鲸鱼")),
                ("生气的猫咪表情", ("生气", "猫")),
            ]
        )
        library = await self._library(ai)
        first = await library.add_bytes(PNG_BYTES + b"1", "image/png")
        second = await library.add_bytes(PNG_BYTES + b"2", "image/png")
        assert first is not None and second is not None
        self.assertEqual(first.description, "开心的鲸鱼表情")
        self.assertEqual(first.tags, ("开心", "鲸鱼"))

        catalog = library.catalog_for("今天真的很开心")
        self.assertEqual(catalog[0].filename, first.filename)
        text = library.catalog_text("今天真的很开心")
        self.assertIn(first.filename, text)
        self.assertIn("开心的鲸鱼表情", text)

    async def test_screenshot_is_removed_from_library(self) -> None:
        ai = FakeCaptionAI([("微信聊天截图", ("截图", "聊天记录"))])
        library = await self._library(ai)
        entry = await library.add_bytes(PNG_BYTES, "image/png")
        self.assertIsNone(entry)
        self.assertEqual(len(library), 0)
        self.assertEqual(list(self.settings.memes_dir.glob("*.png")), [])

    async def test_non_meme_photo_is_removed(self) -> None:
        ai = FakeCaptionAI(
            [MemeCaption("一盘三文鱼刺身", ("食物", "三文鱼"), False)]
        )
        library = await self._library(ai)
        entry = await library.add_bytes(PNG_BYTES, "image/png")
        self.assertIsNone(entry)
        self.assertEqual(len(library), 0)
        self.assertEqual(list(self.settings.memes_dir.glob("*.png")), [])

    async def test_non_meme_pending_file_is_removed_on_analysis(self) -> None:
        ai = FakeCaptionAI([MemeCaption("普通风景照", ("风景",), False)])
        library = await self._library(ai)
        path = self.settings.memes_dir / "photo.png"
        path.write_bytes(PNG_BYTES)

        self.assertTrue(await library.refresh(max_age=0))
        await library.analyze_pending()

        self.assertEqual(len(library), 0)
        self.assertFalse(path.exists())

    async def test_manual_alias_keeps_non_meme_image(self) -> None:
        ai = FakeCaptionAI(
            [MemeCaption("一盘三文鱼刺身", ("食物", "三文鱼"), False)]
        )
        library = await self._library(ai)
        entry = await library.add_bytes(
            PNG_BYTES, "image/png", alias="三文鱼"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(len(library), 1)
        self.assertEqual(library.search("三文鱼")[0].tags[0], "三文鱼")

    async def test_gif_is_collected_and_sendable(self) -> None:
        ai = FakeCaptionAI([("哈哈哈的黄色小人", ("哈哈哈", "黄色"))])
        library = await self._library(ai)
        entry = await library.add_bytes(GIF_BYTES, "image/gif")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(entry.filename.endswith(".gif"))
        self.assertEqual(entry.description, "哈哈哈的黄色小人")
        self.assertIsNotNone(library.resolve(entry.filename))

    async def test_unsupported_format_is_skipped(self) -> None:
        library = await self._library()
        self.assertIsNone(
            await library.add_bytes(b"webp-data", "image/webp")
        )
        self.assertEqual(len(library), 0)

    async def test_search_matches_tag_filename_and_description(self) -> None:
        ai = FakeCaptionAI(
            [
                ("开心的鲸鱼表情", ("开心", "鲸鱼")),
                ("生气的猫咪表情", ("生气", "猫")),
            ]
        )
        library = await self._library(ai)
        whale = await library.add_bytes(PNG_BYTES + b"1", "image/png")
        cat = await library.add_bytes(PNG_BYTES + b"2", "image/png")
        assert whale is not None and cat is not None

        self.assertEqual(library.search("鲸鱼")[0].filename, whale.filename)
        self.assertEqual(library.search("开心")[0].filename, whale.filename)
        self.assertEqual(library.search("猫")[0].filename, cat.filename)
        self.assertEqual(
            library.search(whale.filename[:12])[0].filename, whale.filename
        )
        self.assertEqual(library.search("不存在"), [])

    async def test_unknown_content_type_falls_back_to_magic_bytes(self) -> None:
        library = await self._library()
        entry = await library.add_bytes(
            PNG_BYTES, "application/octet-stream"
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertTrue(entry.filename.endswith(".png"))

    async def test_apply_alias_renames_for_search(self) -> None:
        ai = FakeCaptionAI([("黄黄的一坨小人", ("黄色", "小人"))])
        library = await self._library(ai)
        entry = await library.add_bytes(GIF_BYTES, "image/gif")
        assert entry is not None

        tagged = await library.apply_alias(entry, "哈哈哈")
        self.assertEqual(tagged.tags[0], "哈哈哈")
        self.assertIn("哈哈哈", tagged.description)
        self.assertEqual(library.search("哈哈哈")[0].filename, entry.filename)

        empty = await library.apply_alias(tagged, "")
        self.assertEqual(empty.tags[0], "哈哈哈")

    async def test_refresh_picks_up_images_added_while_running(self) -> None:
        library = await self._library(
            FakeCaptionAI([("手动上传的小鱼", ("小鱼",))])
        )
        (self.settings.memes_dir / "manual.png").write_bytes(PNG_BYTES)
        pending = await library.refresh(max_age=0)
        self.assertTrue(pending)
        self.assertEqual(len(library), 1)
        self.assertIn("manual.png", library._entries)


class MemeMarkerTests(unittest.TestCase):
    def test_complete_and_truncated_gif_detection(self) -> None:
        self.assertTrue(_gif_complete(COMPLETE_GIF))
        self.assertFalse(_gif_complete(COMPLETE_GIF[:-8]))
        self.assertFalse(_gif_complete(b"GIF89a" + b"short"))

    def test_extracts_half_and_full_width_markers(self) -> None:
        clean, name = _extract_meme_marker("好呀\n[MEME:group_1.png]")
        self.assertEqual(clean, "好呀")
        self.assertEqual(name, "group_1.png")

        clean, name = _extract_meme_marker("【MEME：cat.jpg】")
        self.assertEqual(clean, "")
        self.assertEqual(name, "cat.jpg")

    def test_reply_without_marker_is_unchanged(self) -> None:
        self.assertEqual(_extract_meme_marker("普通回复"), ("普通回复", ""))

    def test_save_alias_parsing(self) -> None:
        self.assertEqual(_meme_save_alias("保存 哈哈哈"), "哈哈哈")
        self.assertEqual(_meme_save_alias("收藏：小黄脸"), "小黄脸")
        self.assertEqual(_meme_save_alias("保存"), "")
        self.assertIsNone(_meme_save_alias("哈哈哈"))
        self.assertIsNone(_meme_save_alias(""))

    def test_manual_save_command_detection(self) -> None:
        self.assertEqual(
            _manual_meme_save_alias("/表情包 保存 哈哈哈"), "哈哈哈"
        )
        self.assertEqual(
            _manual_meme_save_alias("/meme 收藏 cat"), "cat"
        )
        self.assertIsNone(_manual_meme_save_alias("/表情包 哈哈哈"))
        self.assertIsNone(_manual_meme_save_alias("保存 哈哈哈"))


class FakeApi:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str, str, str]] = []
        self.images: list[tuple[str, str, str, str]] = []

    async def send_group_text(
        self, target: str, content: str, message_id: str
    ) -> dict:
        self.texts.append(("group", target, content, message_id))
        return {}

    async def send_group_image(
        self, target: str, image: str | Path, message_id: str
    ) -> dict:
        self.images.append(("group", target, str(image), message_id))
        return {}


class FakeChatAI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.catalogs: list[str] = []

    async def chat(self, context_text: str, *, images=(), meme_catalog="") -> str:
        self.catalogs.append(meme_catalog)
        return self.reply


class StubDownloadLibrary(MemeLibrary):
    """Library whose downloads always return the same fake GIF."""

    async def _download(self, url: str) -> tuple[bytes, str]:
        return GIF_BYTES, "image/gif"


class HandlerMemeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
        )
        self.contexts = ContextWindowManager(
            self.root / "context.json", default_size=5_000
        )
        await self.contexts.load()
        self.api = FakeApi()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_handler_sends_text_then_selected_meme(self) -> None:
        library = MemeLibrary(
            self.settings,
            ai_client=FakeCaptionAI([("开心的鲸鱼表情", ("开心", "鲸鱼"))]),
        )
        await library.load()
        entry = await library.add_bytes(PNG_BYTES, "image/png")
        assert entry is not None

        ai = FakeChatAI(f"好呀\n[MEME:{entry.filename}]")
        handler = MessageHandler(
            self.settings,
            self.api,  # type: ignore[arg-type]
            self.contexts,
            ai,  # type: ignore[arg-type]
            library,
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-1",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="今天很开心",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
            )
        )

        self.assertEqual(self.api.texts[-1][2], "好呀")
        self.assertEqual(len(self.api.images), 1)
        self.assertTrue(self.api.images[0][2].endswith(entry.filename))
        self.assertTrue(ai.catalogs[-1])

    async def test_meme_command_matches_keyword(self) -> None:
        library = MemeLibrary(
            self.settings,
            ai_client=FakeCaptionAI(
                [
                    ("开心的鲸鱼表情", ("开心", "鲸鱼")),
                    ("生气猫咪表情", ("生气", "猫")),
                ]
            ),
        )
        await library.load()
        whale = await library.add_bytes(PNG_BYTES + b"1", "image/png")
        cat = await library.add_bytes(GIF_BYTES, "image/gif")
        assert whale is not None and cat is not None

        handler = MessageHandler(
            self.settings,
            self.api,  # type: ignore[arg-type]
            self.contexts,
            None,
            library,
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-2",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="/表情包 鲸鱼",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
            )
        )
        self.assertTrue(self.api.images[-1][2].endswith(whale.filename))

        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-3",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="/表情包 猫",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
            )
        )
        self.assertTrue(self.api.images[-1][2].endswith(cat.filename))

        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-4",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="/表情包 不存在",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
            )
        )
        self.assertIn("没找到", self.api.texts[-1][2])

    async def test_manual_save_of_quoted_gif_sends_once(self) -> None:
        library = StubDownloadLibrary(
            self.settings,
            ai_client=FakeCaptionAI([("黄黄的一坨小人", ("黄色", "小人"))]),
        )
        await library.load()
        library._session = object()  # _download is stubbed out
        handler = MessageHandler(
            self.settings,
            self.api,  # type: ignore[arg-type]
            self.contexts,
            None,
            library,
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-5",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="/表情包 保存 哈哈哈",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
                quoted_image_urls=("https://example.com/yellow.gif",),
            )
        )
        self.assertEqual(len(self.api.images), 1)
        self.assertTrue(self.api.images[0][2].endswith(".gif"))
        self.assertIn("已保存为「哈哈哈」", self.api.texts[-1][2])

        matches = library.search("哈哈哈")
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].filename.endswith(".gif"))

    async def test_manual_save_falls_back_to_recent_image(self) -> None:
        settings = Settings(
            app_id="test",
            app_secret="test",
            assets_dir=self.root / "assets",
            data_dir=self.root / "data",
            meme_auto_collect=False,
        )
        library = StubDownloadLibrary(
            settings,
            ai_client=FakeCaptionAI([("黄黄的一坨小人", ("黄色", "小人"))]),
        )
        await library.load()
        library._session = object()  # _download is stubbed out
        handler = MessageHandler(
            settings, self.api, self.contexts, None, library  # type: ignore[arg-type]
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-6",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="[图片]",
                sender_id="USER-1",
                sender_name="测试用户",
                image_urls=("https://example.com/yellow.gif",),
            )
        )
        await handler.handle(
            IncomingMessage(
                scene="group",
                message_id="MSG-7",
                target_id="TARGET-1",
                conversation_id="group:TARGET-1",
                content="/表情包 保存 哈哈哈",
                sender_id="USER-1",
                sender_name="测试用户",
                mentioned=True,
            )
        )
        self.assertEqual(len(self.api.images), 1)
        self.assertEqual(len(library.search("哈哈哈")), 1)


if __name__ == "__main__":
    unittest.main()