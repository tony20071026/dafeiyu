from __future__ import annotations

import unittest

from utils.text import clean_message_text, is_http_url, truncate_text


class TextTests(unittest.TestCase):
    def test_strips_channel_mention_prefix(self) -> None:
        self.assertEqual(
            clean_message_text("<@!123456>  /ping", bot_name="大肥鱼", bot_id="123456"),
            "/ping",
        )

    def test_strips_name_mention_prefix(self) -> None:
        self.assertEqual(
            clean_message_text("@大肥鱼 /help", bot_name="大肥鱼"), "/help"
        )

    def test_keeps_inner_mentions(self) -> None:
        self.assertEqual(
            clean_message_text("<@!1> 请转告 @小明 开会", bot_id="1"),
            "请转告 @小明 开会",
        )

    def test_keeps_other_member_mention(self) -> None:
        self.assertEqual(
            clean_message_text("<@!A1B2C3D4E5F60718> 是谁？", bot_id="BOT"),
            "@某群友 是谁？",
        )

    def test_resolves_known_mention_to_name(self) -> None:
        self.assertEqual(
            clean_message_text(
                "<@!BRUCE1> 是谁？",
                bot_id="BOT",
                mention_names={"BRUCE1": "落阳深处的布鲁斯"},
            ),
            "@落阳深处的布鲁斯 是谁？",
        )

    def test_keeps_multiple_plain_name_mentions(self) -> None:
        self.assertEqual(
            clean_message_text(
                "@大肥鱼 @落阳深处的布鲁斯 是谁？", bot_name="大肥鱼"
            ),
            "@落阳深处的布鲁斯 是谁？",
        )

    def test_strips_learned_bot_mention(self) -> None:
        self.assertEqual(
            clean_message_text(
                "<@!BOTOPENID> 我们萨克斯手是谁？",
                bot_id="BOT",
                bot_mention_ids={"BOTOPENID"},
            ),
            "我们萨克斯手是谁？",
        )

    def test_url_validation(self) -> None:
        self.assertTrue(is_http_url("https://example.com/a.png"))
        self.assertFalse(is_http_url("file:///etc/passwd"))
        self.assertFalse(is_http_url("not-a-url"))

    def test_truncate(self) -> None:
        self.assertEqual(truncate_text("abcdef", 4), "abc…")


if __name__ == "__main__":
    unittest.main()
