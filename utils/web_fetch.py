"""Fetch a public web page and turn it into a short prompt-friendly excerpt.

The bot only needs a few hundred characters to answer "看看这个链接".  This
module therefore strips HTML, drops scripts/styles, caps the download size and
never returns more text than the caller asked for.  Every character here is
sent to the model, so the limits are deliberately conservative.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp

LOGGER = logging.getLogger(__name__)

# URLs are matched with an ASCII-only character class so Chinese text directly
# after a link (e.g. "看这个https://a.com/x然后…") is not swallowed.
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'，。！？、；：）】》”’"
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
)
_TEXT_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)
_MAX_REDIRECTS = 3
_CACHE_TTL = 600.0
_FAILURE_CACHE_TTL = 60.0
_CACHE_MAX = 50
_USER_AGENT = (
    "Mozilla/5.0 (compatible; DaFeiYuBot/1.0; "
    "+https://github.com/tony20071026/dafeiyu)"
)
_CHARSET_RE = re.compile(
    rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class WebPage:
    """A page that was successfully fetched and reduced to plain text."""

    url: str
    title: str = ""
    description: str = ""
    text: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.title or self.description or self.text)


def extract_urls(text: str, limit: int = 1) -> tuple[str, ...]:
    """Return unique http(s) URLs in order of appearance."""

    if limit <= 0 or not text:
        return ()
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not url or url in found:
            continue
        found.append(url)
        if len(found) >= limit:
            break
    return tuple(found)


def looks_like_image_url(url: str) -> bool:
    """True when a bare URL clearly points at an image file."""

    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return False
    for suffix in _IMAGE_SUFFIXES:
        if path.endswith(suffix):
            return True
    return False


def is_public_ip(value: str) -> bool:
    """Reject loopback, private, link-local, multicast and reserved addresses."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_global


class _PageParser(HTMLParser):
    """Small HTML-to-text extractor with no third-party dependencies."""

    _SKIP_TAGS = frozenset(
        {
            "script",
            "style",
            "noscript",
            "svg",
            "template",
            "iframe",
            "canvas",
            "object",
            "embed",
        }
    )
    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "section",
            "article",
            "header",
            "footer",
            "nav",
            "main",
            "blockquote",
            "pre",
            "table",
            "ul",
            "ol",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            values = {
                key.lower(): (value or "") for key, value in attrs
            }
            name = (
                values.get("name")
                or values.get("property")
                or values.get("http-equiv")
                or ""
            ).lower()
            if name in {
                "description",
                "og:description",
                "twitter:description",
            }:
                content = " ".join(values.get("content", "").split())
                if content and not self.description:
                    self.description = content[:300]
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)

    def text(self) -> str:
        lines: list[str] = []
        for raw_line in "".join(self._parts).replace("\r", "\n").split("\n"):
            line = " ".join(raw_line.split())
            if not line:
                continue
            if lines and lines[-1] == line:
                continue
            lines.append(line)
        return "\n".join(lines)


def parse_html(url: str, raw_html: str) -> WebPage:
    """Extract title, description and visible text from an HTML document."""

    parser = _PageParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:  # malformed markup should never break a reply
        LOGGER.debug("HTML 解析不完整: %s", url)
    return WebPage(
        url=url,
        title=" ".join(parser.title.split())[:200],
        description=parser.description,
        text=parser.text(),
    )


def parse_plain_text(url: str, raw_text: str) -> WebPage:
    text = "\n".join(
        line for line in (" ".join(row.split()) for row in raw_text.splitlines())
        if line
    )
    return WebPage(url=url, text=text)


def render_pages(pages: tuple[WebPage, ...], max_chars: int) -> str:
    """Render fetched pages into a bounded block for the chat prompt."""

    if max_chars <= 0 or not pages:
        return ""
    blocks: list[str] = []
    for page in pages:
        lines = [f"链接：{page.url}"]
        if page.title:
            lines.append(f"标题：{page.title}")
        if page.description:
            lines.append(f"摘要：{page.description}")
        if page.text:
            lines.append(f"正文摘录：{page.text}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    if not blocks:
        return ""
    header = "【网页内容】\n"
    body = "\n\n".join(blocks)
    budget = max(0, max_chars - len(header) - 1)
    if len(body) > budget:
        body = body[:budget].rstrip() + "…"
    return header + body


def _detect_charset(data: bytes, header_charset: str | None) -> list[str]:
    candidates: list[str] = []
    match = _CHARSET_RE.search(data[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore").lower())
    if header_charset:
        candidates.append(header_charset.lower())
    candidates.extend(["utf-8", "gb18030"])
    normalized: list[str] = []
    for value in candidates:
        if value in {"gb2312", "gbk"}:
            value = "gb18030"
        elif value in {"utf8"}:
            value = "utf-8"
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _decode_body(data: bytes, header_charset: str | None) -> str:
    for charset in _detect_charset(data, header_charset):
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


async def _host_is_public(host: str, port: int) -> bool:
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            ),
            timeout=3.0,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    if not infos:
        return False
    # Reject a hostname if any resolved address is non-public.  This also
    # makes simple DNS-rebinding tricks much less useful.
    return all(is_public_ip(info[4][0]) for info in infos)


def _parse_target(url: str):
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        parsed.port  # validate the port eagerly
    except ValueError:
        return None
    if scheme not in {"http", "https"}:
        return None
    if not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return parsed


class WebFetcher:
    """Fetch public pages with caching, size limits and basic SSRF checks."""

    def __init__(self, settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._cache: dict[str, tuple[float, WebPage | None]] = {}

    async def fetch_for_text(self, text: str) -> tuple[WebPage, ...]:
        """Fetch the first URL in *text*, if it yields useful content."""

        if not self._settings.web_fetch_enabled:
            return ()
        if self._settings.web_fetch_max_chars <= 0:
            return ()
        for url in extract_urls(text, limit=1):
            page = await self.fetch(url)
            if page is None:
                continue
            text_chars = len(page.text)
            if text_chars < 80 and len(page.description) < 30:
                LOGGER.info(
                    "网页正文太少，跳过 | %s | text=%s desc=%s",
                    url[:120],
                    text_chars,
                    len(page.description),
                )
                continue
            return (page,)
        return ()

    async def fetch(self, url: str) -> WebPage | None:
        if not self._settings.web_fetch_enabled:
            return None
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached is not None and cached[0] > now:
            return cached[1]

        page = await self._fetch_uncached(url)
        ttl = _CACHE_TTL if page is not None else _FAILURE_CACHE_TTL
        self._cache[url] = (now + ttl, page)
        if len(self._cache) > _CACHE_MAX:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)
        return page

    async def _fetch_uncached(self, url: str) -> WebPage | None:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = _parse_target(current)
            if parsed is None:
                LOGGER.info("忽略非 http(s) 链接: %s", current[:120])
                return None
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not await _host_is_public(parsed.hostname, port):
                LOGGER.warning("拒绝抓取非公网地址: %s", current[:160])
                return None

            timeout = aiohttp.ClientTimeout(
                total=self._settings.web_fetch_timeout
            )
            try:
                async with self._session.get(
                    current,
                    allow_redirects=False,
                    timeout=timeout,
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Accept": (
                            "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                    },
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        LOGGER.info(
                            "网页抓取失败 HTTP %s | %s",
                            response.status,
                            current[:120],
                        )
                        return None

                    content_type = (
                        response.headers.get("Content-Type") or ""
                    ).split(";")[0].strip().lower()
                    if content_type not in _TEXT_CONTENT_TYPES:
                        LOGGER.info(
                            "跳过非文本内容 %s | %s",
                            content_type or "?",
                            current[:120],
                        )
                        return None

                    data = await self._read_limited(response)
                    charset = response.charset
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                LOGGER.info(
                    "网页抓取异常 %s | %s | %s",
                    type(exc).__name__,
                    current[:120],
                    exc,
                )
                return None
            except Exception:
                LOGGER.exception("网页抓取意外失败 | %s", current[:120])
                return None
        else:
            LOGGER.info("网页重定向过多: %s", url[:120])
            return None

        if not data:
            return None
        decoded = _decode_body(data, charset)
        if content_type == "text/plain":
            page = parse_plain_text(current, decoded)
        else:
            page = parse_html(current, decoded)
        if not page.has_content:
            return None
        return page

    async def _read_limited(
        self, response: aiohttp.ClientResponse
    ) -> bytes:
        max_bytes = self._settings.web_fetch_max_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(16_384):
            if not chunk:
                continue
            remaining = max_bytes - total
            if remaining <= 0:
                break
            piece = chunk[:remaining]
            chunks.append(piece)
            total += len(piece)
            if total >= max_bytes:
                break
        return b"".join(chunks)