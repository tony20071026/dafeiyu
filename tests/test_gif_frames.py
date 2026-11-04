from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from utils.gif_frames import even_frame_indices, sample_gif_frames


def _write_gif(path: Path, colors: list[tuple[int, int, int]]) -> None:
    frames = [Image.new("RGB", (24, 24), color) for color in colors]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=60,
        loop=0,
        disposal=2,
    )


class GifFrameTests(unittest.TestCase):
    def test_even_frame_indices_include_first_and_last(self) -> None:
        self.assertEqual(even_frame_indices(8, 4), [0, 2, 5, 7])
        self.assertEqual(even_frame_indices(4, 4), [0, 1, 2, 3])
        self.assertEqual(even_frame_indices(2, 4), [0, 1])
        self.assertEqual(even_frame_indices(1, 4), [0])
        self.assertEqual(even_frame_indices(0, 4), [])

    def test_samples_four_evenly_spaced_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "animation.gif"
            _write_gif(
                path,
                [
                    (0, 0, 0),
                    (32, 32, 32),
                    (64, 64, 64),
                    (96, 96, 96),
                    (128, 128, 128),
                    (160, 160, 160),
                    (192, 192, 192),
                    (224, 224, 224),
                ],
            )

            frames = sample_gif_frames(path, count=4)
            self.assertEqual(len(frames), 4)
            brightness = []
            for data, mime in frames:
                self.assertEqual(mime, "image/jpeg")
                with Image.open(io.BytesIO(data)) as frame:
                    grayscale = frame.convert("L")
                    brightness.append(sum(grayscale.getdata()) / (
                        grayscale.width * grayscale.height
                    ))
            self.assertEqual(brightness, sorted(brightness))
            self.assertLess(brightness[0], 20)
            self.assertGreater(brightness[-1], 200)

    def test_short_gif_returns_all_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "short.gif"
            _write_gif(path, [(0, 0, 0), (128, 128, 128), (255, 255, 255)])

            frames = sample_gif_frames(path, count=4)
            self.assertEqual(len(frames), 3)

    def test_duplicate_frames_are_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "static.gif"
            _write_gif(path, [(40, 80, 120)] * 6)

            frames = sample_gif_frames(path, count=4)
            self.assertEqual(len(frames), 1)

    def test_non_gif_returns_no_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "still.png"
            Image.new("RGB", (24, 24), (10, 20, 30)).save(path)

            self.assertEqual(sample_gif_frames(path), [])


if __name__ == "__main__":
    unittest.main()