from __future__ import annotations

import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from config import Settings
from utils.web_fetch import (
    WebFetcher,
    WebPage,
    extract_urls,
    is_public_ip,
    looks_like_image_url,
    parse_html,
    render_pages,
)


class WebTextTests(unittest.TestCase):
    def test_extracts_urls_and_strips_punctuation(self) -> None:
        text = (
            "看这个https://example.com/a?x=1然后再说，"
            "还有 https://example.com/b。"
        )
        self.assertEqual(
            extract_urls(text, limit=2),
            ("https://example.com/a?x=1", "https://example.com/b"),
        )

    def test_deduplicates_and_honours_limit(self) -> None:
        text = "https://a.com/x https://a.com/x https://b.com/y"
        self.assertEqual(extract_urls(text, limit=2), ("https://a.com/x", "https://b.com/y"))
        self.assertEqual(extract_urls(text, limit=0), ())

    def test_image_url_detection(self) -> None:
        self.assertTrue(looks_like_image_url("https://x.com/a.PNG?size=1"))
        self.assertTrue(looks_like_image_url("https://x.com/a.gif"))
        self.assertFalse(looks_like_image_url("https://x.com/article"))
        self.assertFalse(looks_like_image_url("not-a-url"))

    def test_public_ip_filter(self) -> None:
        self.assertFalse(is_public_ip("127.0.0.1"))
        self.assertFalse(is_public_ip("10.1.2.3"))
        self.assertFalse(is_public_ip("192.168.1.10"))
        self.assertFalse(is_public_ip("169.254.1.1"))
        self.assertFalse(is_public_ip("::1"))
        self.assertFalse(is_public_ip("not-an-ip"))
        self.assertTrue(is_public_ip("8.8.8.8"))
        self.assertTrue(is_public_ip("2001:4860:4860::8888"))

    def test_parse_html_drops_scripts_and_keeps_text(self) -> None:
        page = parse_html(
            "https://example.com/a",
            """
            <html><head>
              <title> 测试 标题 </title>
              <meta name="description" content="这是 摘要">
            </head><body>
              <script>alert(1)</script>
              <style>.x{color:red}</style>
              <h1>大标题</h1>
              <p>第一段&nbsp;内容</p>
              <div>第二段</div>
            </body></html>
            """,
        )
        self.assertEqual(page.title, "测试 标题")
        self.assertEqual(page.description, "这是 摘要")
        self.assertIn("大标题", page.text)
        self.assertIn("第一段 内容", page.text)
        self.assertIn("第二段", page.text)
        self.assertNotIn("alert", page.text)
        self.assertNotIn("color:red", page.text)

    def test_render_pages_respects_limit(self) -> None:
        page = WebPage(
            "https://example.com/a",
            title="标题",
            description="摘要",
            text="正文" * 500,
        )
        rendered = render_pages((page,), 120)
        self.assertLessEqual(len(rendered), 120)
        self.assertTrue(rendered.startswith("【网页内容】"))
        self.assertTrue(rendered.endswith("…"))
        self.assertEqual(render_pages((page,), 0), "")
        self.assertEqual(render_pages((), 120), "")


class WebFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests = 0

        async def page(request: web.Request) -> web.Response:
            self.requests += 1
            return web.Response(
                text=(
                    "<html><head><title>示例文章</title>"
                    '<meta name="description" content="文章摘要"></head>'
                    "<body><p>" + "这是正文内容。" * 20 + "</p></body></html>"
                ),
                content_type="text/html",
                charset="utf-8",
            )

        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPFound("/page")

        async def image(request: web.Request) -> web.Response:
            return web.Response(body=b"\x89PNG\r\n", content_type="image/png")

        async def empty(request: web.Request) -> web.Response:
            return web.Response(text="<html><body>hi</body></html>", content_type="text/html")

        async def plain(request: web.Request) -> web.Response:
            return web.Response(
                text="hello\nworld\n" + "这是正文。" * 20,
                content_type="text/plain",
            )

        app = web.Application()
        app.router.add_get("/page", page)
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/image", image)
        app.router.add_get("/empty", empty)
        app.router.add_get("/plain", plain)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        self.session = aiohttp.ClientSession()
        self.settings = Settings(
            app_id="test",
            app_secret="test",
            web_fetch_enabled=True,
            web_fetch_max_chars=800,
            web_fetch_timeout=5,
            web_fetch_max_bytes=1024 * 1024,
        )

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.runner.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def test_private_address_is_rejected(self) -> None:
        fetcher = WebFetcher(self.settings, self.session)
        self.assertIsNone(await fetcher.fetch(self._url("/page")))
        self.assertEqual(self.requests, 0)

    async def test_invalid_targets_are_rejected(self) -> None:
        fetcher = WebFetcher(self.settings, self.session)
        self.assertIsNone(await fetcher.fetch("http://example.com:bad/"))
        self.assertIsNone(await fetcher.fetch("file:///etc/passwd"))
        self.assertIsNone(await fetcher.fetch("http://user:pass@example.com/"))

    async def test_fetch_parses_page_and_caches(self) -> None:
        async def allow(host: str, port: int) -> bool:
            return True

        with patch("utils.web_fetch._host_is_public", allow):
            fetcher = WebFetcher(self.settings, self.session)
            page = await fetcher.fetch(self._url("/page"))
            self.assertIsNotNone(page)
            assert page is not None
            self.assertEqual(page.title, "示例文章")
            self.assertEqual(page.description, "文章摘要")
            self.assertIn("这是正文内容", page.text)

            again = await fetcher.fetch(self._url("/page"))
            self.assertEqual(again, page)
            self.assertEqual(self.requests, 1)

    async def test_follows_redirect(self) -> None:
        async def allow(host: str, port: int) -> bool:
            return True

        with patch("utils.web_fetch._host_is_public", allow):
            fetcher = WebFetcher(self.settings, self.session)
            page = await fetcher.fetch(self._url("/redirect"))
            self.assertIsNotNone(page)
            assert page is not None
            self.assertTrue(page.url.endswith("/page"))
            self.assertEqual(page.title, "示例文章")

    async def test_rejects_non_text_and_tiny_pages(self) -> None:
        async def allow(host: str, port: int) -> bool:
            return True

        with patch("utils.web_fetch._host_is_public", allow):
            fetcher = WebFetcher(self.settings, self.session)
            self.assertIsNone(await fetcher.fetch(self._url("/image")))
            self.assertEqual(
                await fetcher.fetch_for_text(f"看看 {self._url('/empty')}"), ()
            )
            pages = await fetcher.fetch_for_text(f"看看 {self._url('/plain')}")
            self.assertEqual(len(pages), 1)
            self.assertTrue(pages[0].text.startswith("hello\nworld"))
            self.assertIn("这是正文", pages[0].text)


if __name__ == "__main__":
    unittest.main()