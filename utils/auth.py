"""QQ Open Platform access-token acquisition and proactive refresh."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import Settings

LOGGER = logging.getLogger(__name__)


class TokenError(RuntimeError):
    """Raised when an access token could not be obtained."""


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: float  # monotonic clock
    expires_in: int
    refresh_at: float  # monotonic clock; proactive refresh point


class TokenManager:
    """Caches the token and refreshes it before it expires.

    The HTTP client asks :meth:`get_token` for every request, so an expired token
    is refreshed transparently.  A background loop additionally refreshes it
    five minutes early, even while the bot is idle.
    """

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()

    @property
    def refresh_margin(self) -> int:
        return self._settings.token_refresh_margin

    async def get_token(self, *, force: bool = False) -> str:
        if not force:
            token = self._token
            if token and time.monotonic() < token.refresh_at:
                return token.value

        async with self._lock:
            # Another caller may have refreshed while we were waiting.
            token = self._token
            if not force and token and time.monotonic() < token.refresh_at:
                return token.value
            try:
                return await self._fetch_token()
            except TokenError:
                # A failed *proactive* refresh must not break requests while the
                # current token is still valid.  Forced refreshes (e.g. after a
                # 4004 invalid-token close) must not reuse the old token.
                if (
                    not force
                    and token
                    and time.monotonic() < token.expires_at
                ):
                    LOGGER.warning(
                        "Token 提前刷新失败，继续使用尚未过期的旧 Token"
                    )
                    return token.value
                raise

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None

    async def _fetch_token(self) -> str:
        payload = {
            "appId": self._settings.app_id,
            "clientSecret": self._settings.app_secret,
        }
        candidates = [
            url
            for url in (self._settings.token_url, self._settings.token_fallback_url)
            if url
        ]
        errors: list[str] = []

        for url in dict.fromkeys(candidates):
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self._settings.http_timeout),
                ) as response:
                    raw = await response.text()
                    if response.status != 200:
                        raise TokenError(f"HTTP {response.status}: {raw[:200]}")
                    try:
                        data: dict[str, Any] = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise TokenError(f"响应不是合法 JSON: {raw[:120]}") from exc

                code = data.get("code")
                if code not in (None, 0, "0"):
                    raise TokenError(
                        f"业务错误 code={code}, message={data.get('message', '')!r}"
                    )
                access_token = str(data.get("access_token") or "").strip()
                if not access_token:
                    raise TokenError("响应中缺少 access_token")

                try:
                    expires_in = int(data.get("expires_in") or 7200)
                except (TypeError, ValueError):
                    expires_in = 7200
                expires_in = max(60, expires_in)
                now = time.monotonic()
                # QQ may occasionally return a token whose remaining lifetime is
                # shorter than the configured refresh margin.  Using the full
                # margin in that case puts refresh_at in the past, which makes
                # both get_token() and the background loop refresh continuously.
                margin = min(self.refresh_margin, max(5, expires_in // 3))
                refresh_at = now + max(5, expires_in - margin)
                self._token = AccessToken(
                    value=access_token,
                    expires_at=now + expires_in,
                    expires_in=expires_in,
                    refresh_at=refresh_at,
                )
                LOGGER.info(
                    "Access Token 已刷新，有效期 %s 秒，%s 秒后提前刷新",
                    expires_in,
                    max(0, int(refresh_at - now)),
                )
                return access_token
            except (aiohttp.ClientError, asyncio.TimeoutError, TokenError) as exc:
                errors.append(f"{url} -> {exc}")
                LOGGER.warning("获取 Access Token 失败（%s）：%s", url, exc)

        detail = "；".join(errors) if errors else "没有可用的 Token 接口"
        raise TokenError(f"无法获取 Access Token：{detail}")

    async def auto_refresh_loop(self, stop_event: asyncio.Event) -> None:
        """Refresh the token a few minutes before expiry until asked to stop."""

        while not stop_event.is_set():
            try:
                await self.get_token()
                token = self._token
                if token is None:
                    await asyncio.sleep(5)
                    continue
                sleep_for = max(
                    5.0,
                    token.refresh_at - time.monotonic(),
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    # Non-forced: if another coroutine refreshed while we slept,
                    # this returns the new token without another API call.
                    await self.get_token()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Token 后台刷新失败，30 秒后重试")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
