from __future__ import annotations

import json
import time
import unittest

from config import Settings
from utils.auth import AccessToken, TokenError, TokenManager


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(payload)

    async def text(self) -> str:
        return self._raw

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls = 0

    def post(self, *args: object, **kwargs: object) -> FakeResponse:
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return FakeResponse(self.payloads[index])


class TokenManagerTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, payloads: list[dict]) -> tuple[TokenManager, FakeSession]:
        settings = Settings(
            app_id="app",
            app_secret="secret",
            token_refresh_margin=300,
        )
        session = FakeSession(payloads)
        manager = TokenManager(settings, session)  # type: ignore[arg-type]
        return manager, session

    async def test_short_lived_token_is_not_refreshed_immediately(self) -> None:
        manager, session = self._manager(
            [{"access_token": "first", "expires_in": 100}]
        )
        first = await manager.get_token()
        second = await manager.get_token()

        self.assertEqual(first, "first")
        self.assertEqual(second, "first")
        self.assertEqual(session.calls, 1)

        token = manager._token
        assert token is not None
        self.assertEqual(token.expires_in, 100)
        self.assertGreater(token.refresh_at, time.monotonic())
        self.assertLessEqual(token.refresh_at, token.expires_at)

    async def test_full_length_token_keeps_configured_margin(self) -> None:
        manager, _ = self._manager(
            [{"access_token": "long", "expires_in": 7200}]
        )
        now = time.monotonic()
        await manager.get_token()

        token = manager._token
        assert token is not None
        self.assertAlmostEqual(token.refresh_at - now, 7200 - 300, delta=2)

    async def test_failed_proactive_refresh_keeps_valid_token(self) -> None:
        manager, session = self._manager(
            [{"access_token": "old", "expires_in": 100}]
        )
        await manager.get_token()

        token = manager._token
        assert token is not None
        manager._token = AccessToken(
            value="old",
            expires_at=time.monotonic() + 30,
            expires_in=100,
            refresh_at=time.monotonic() - 1,
        )

        async def fail() -> str:
            raise TokenError("network down")

        manager._fetch_token = fail  # type: ignore[method-assign]
        self.assertEqual(await manager.get_token(), "old")
        self.assertEqual(session.calls, 1)

    async def test_forced_refresh_does_not_reuse_invalid_token(self) -> None:
        manager, _ = self._manager(
            [{"access_token": "old", "expires_in": 7200}]
        )
        await manager.get_token()

        async def fail() -> str:
            raise TokenError("invalid token")

        manager._fetch_token = fail  # type: ignore[method-assign]
        with self.assertRaises(TokenError):
            await manager.get_token(force=True)


if __name__ == "__main__":
    unittest.main()