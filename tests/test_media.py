from __future__ import annotations

import unittest

from utils.media import read_limited


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.content = FakeContent(chunks)


class ReadLimitedTests(unittest.IsolatedAsyncioTestCase):
    async def test_joins_all_chunks(self) -> None:
        # This is the QQ case: 16 KiB followed by the rest of the image.
        chunks = [b"a" * 16384, b"b" * 20000, b"c" * 5000]
        data, exceeded = await read_limited(FakeResponse(chunks), 5 * 1024 * 1024)
        self.assertFalse(exceeded)
        self.assertEqual(len(data), 41384)
        self.assertTrue(data.startswith(b"a" * 16384))
        self.assertTrue(data.endswith(b"c" * 5000))

    async def test_stops_at_limit(self) -> None:
        chunks = [b"x" * 100, b"y" * 100]
        data, exceeded = await read_limited(FakeResponse(chunks), 150)
        self.assertTrue(exceeded)
        self.assertEqual(len(data), 150)

    async def test_empty_body(self) -> None:
        data, exceeded = await read_limited(FakeResponse([]), 100)
        self.assertEqual(data, b"")
        self.assertFalse(exceeded)


if __name__ == "__main__":
    unittest.main()