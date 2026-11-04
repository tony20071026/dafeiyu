from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from utils.context_window import (
    ContextWindowManager,
    normalize_context_size,
    context_size_label,
)


class ContextWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases(self) -> None:
        self.assertEqual(normalize_context_size("5000"), 5_000)
        self.assertEqual(normalize_context_size("1w"), 10_000)
        self.assertEqual(normalize_context_size("1万"), 10_000)
        self.assertEqual(normalize_context_size("5w"), 50_000)
        self.assertEqual(normalize_context_size("5万"), 50_000)
        self.assertIsNone(normalize_context_size("2w"))
        self.assertIn("10,000", context_size_label(10_000))

    async def test_window_trims_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "context.json"
            manager = ContextWindowManager(state, default_size=5_000)
            await manager.load()
            for index in range(200):
                manager.append(
                    "group:test",
                    author="用户",
                    content=f"第 {index} 条消息 " + ("x" * 80),
                )
            self.assertEqual(manager.limit_for("group:test"), 5_000)
            self.assertLessEqual(len(manager.build("group:test")), 5_000)

            await manager.set_limit("group:test", 10_000)
            self.assertTrue(state.exists())

            reloaded = ContextWindowManager(state, default_size=5_000)
            await reloaded.load()
            self.assertEqual(reloaded.limit_for("group:test"), 10_000)

    async def test_rejects_unsupported_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = ContextWindowManager(Path(temporary) / "context.json")
            with self.assertRaises(ValueError):
                await manager.set_limit("group:test", 20_000)


if __name__ == "__main__":
    unittest.main()
