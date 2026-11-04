from __future__ import annotations

import unittest
from typing import Any

from config import Settings
from utils.api import QQApiClient


class FakeTokens:
    async def get_token(self, force: bool = False) -> str:
        return "TOKEN"


class ApiPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = Settings(app_id="test", app_secret="test")
        self.api = QQApiClient(self.settings, session=None, token_manager=FakeTokens())  # type: ignore[arg-type]
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

        async def fake_request(
            method: str,
            path: str,
            *,
            json_body: dict[str, Any] | None = None,
            data: Any = None,
            params: Any = None,
            retry_auth: bool = True,
        ) -> dict[str, Any]:
            self.calls.append((method, path, json_body))
            if path.endswith("/files"):
                return {"file_info": "FILE-INFO", "ttl": 300}
            return {"id": "BOT-MSG"}

        self.api._request = fake_request  # type: ignore[method-assign]

    async def test_group_text_increments_msg_seq(self) -> None:
        await self.api.send_group_text("GROUP", "hello", "MSG")
        await self.api.send_group_text("GROUP", "again", "MSG")
        first, second = self.calls
        self.assertEqual(first[2]["msg_id"], "MSG")
        self.assertEqual(first[2]["msg_seq"], 1)
        self.assertEqual(second[2]["msg_seq"], 2)

    async def test_group_proactive_text_omits_passive_fields(self) -> None:
        await self.api.send_group_text("GROUP", "水一下", "")
        _, path, body = self.calls[0]
        self.assertTrue(path.endswith("/v2/groups/GROUP/messages"))
        self.assertEqual(body["msg_type"], 0)
        self.assertEqual(body["content"], "水一下")
        self.assertNotIn("msg_id", body)
        self.assertNotIn("msg_seq", body)

    async def test_group_url_image_uses_upload_then_media(self) -> None:
        await self.api.send_group_image("GROUP", "https://example.com/a.png", "MSG")
        upload, message = self.calls
        self.assertTrue(upload[1].endswith("/files"))
        self.assertEqual(upload[2]["url"], "https://example.com/a.png")
        self.assertFalse(upload[2]["srv_send_msg"])
        self.assertEqual(message[2]["msg_type"], 7)
        self.assertEqual(message[2]["msg_id"], "MSG")
        self.assertEqual(message[2]["media"]["file_info"], "FILE-INFO")

    async def test_channel_url_image_carries_msg_id(self) -> None:
        await self.api.send_channel_image(
            "CHANNEL", "https://example.com/a.png", "MSG"
        )
        method, path, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/channels/CHANNEL/messages"))
        self.assertEqual(body, {"image": "https://example.com/a.png", "msg_id": "MSG"})

    async def test_c2c_text_carries_passive_fields(self) -> None:
        await self.api.send_c2c_text("USER", "hello", "MSG")
        _, path, body = self.calls[0]
        self.assertTrue(path.endswith("/v2/users/USER/messages"))
        self.assertEqual(body["msg_type"], 0)
        self.assertEqual(body["msg_id"], "MSG")
        self.assertEqual(body["msg_seq"], 1)

    async def test_c2c_url_image_uses_user_upload(self) -> None:
        await self.api.send_c2c_image("USER", "https://example.com/a.png", "MSG")
        upload, message = self.calls
        self.assertTrue(upload[1].endswith("/v2/users/USER/files"))
        self.assertEqual(upload[2]["url"], "https://example.com/a.png")
        self.assertTrue(message[1].endswith("/v2/users/USER/messages"))
        self.assertEqual(message[2]["media"]["file_info"], "FILE-INFO")
        self.assertEqual(message[2]["msg_id"], "MSG")


if __name__ == "__main__":
    unittest.main()
