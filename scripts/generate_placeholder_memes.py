#!/usr/bin/env python3
"""Generate two placeholder memes so /meme works out of the box.

Pillow is used for the nice version.  A small standard-library PNG fallback is
included so the script still works on a machine where Pillow is unavailable.
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only on minimal installs
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets" / "memes"


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu") / name,
        Path("/usr/share/fonts/truetype/liberation2")
        / name.replace("DejaVuSans", "LiberationSans"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def _background(
    top: tuple[int, int, int], bottom: tuple[int, int, int], size: int
) -> Image.Image:
    image = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def _draw_fish(
    image: Image.Image,
    *,
    body: tuple[int, int, int],
    belly: tuple[int, int, int],
    accent: tuple[int, int, int],
    title: str,
    subtitle: str,
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    scale = width / 900

    def box(left: float, top: float, right: float, bottom: float) -> list[int]:
        return [
            int(left * scale),
            int(top * scale),
            int(right * scale),
            int(bottom * scale),
        ]

    draw.rounded_rectangle(
        box(60, 90, 840, 810),
        radius=int(44 * scale),
        fill=(255, 255, 255, 236),
        outline=(12, 74, 110, 255),
        width=max(2, int(4 * scale)),
    )

    draw.polygon(
        [
            (int(620 * scale), int(470 * scale)),
            (int(790 * scale), int(350 * scale)),
            (int(760 * scale), int(500 * scale)),
            (int(790 * scale), int(650 * scale)),
            (int(620 * scale), int(545 * scale)),
        ],
        fill=accent + (255,),
    )
    draw.polygon(
        [
            (int(390 * scale), int(350 * scale)),
            (int(500 * scale), int(260 * scale)),
            (int(530 * scale), int(380 * scale)),
        ],
        fill=accent + (255,),
    )
    draw.polygon(
        [
            (int(390 * scale), int(610 * scale)),
            (int(500 * scale), int(690 * scale)),
            (int(530 * scale), int(600 * scale)),
        ],
        fill=accent + (255,),
    )

    draw.ellipse(
        box(190, 345, 690, 640),
        fill=body + (255,),
        outline=(8, 55, 80, 255),
        width=max(2, int(4 * scale)),
    )
    draw.ellipse(box(245, 470, 650, 620), fill=belly + (255,))

    for x in (350, 430, 510):
        draw.arc(
            box(x, 405, x + 105, 530),
            start=285,
            end=75,
            fill=(255, 255, 255, 125),
            width=max(2, int(3 * scale)),
        )

    draw.ellipse(box(285, 430, 345, 490), fill=(255, 255, 255, 255))
    draw.ellipse(box(305, 450, 334, 479), fill=(21, 35, 45, 255))
    draw.ellipse(box(316, 457, 327, 468), fill=(255, 255, 255, 255))
    draw.arc(
        box(300, 505, 385, 570),
        start=12,
        end=145,
        fill=(8, 55, 80, 255),
        width=max(2, int(4 * scale)),
    )

    for cx, cy, radius in ((735, 260, 25), (780, 205, 16), (715, 180, 11)):
        draw.ellipse(
            box(cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(255, 255, 255, 90),
            outline=(255, 255, 255, 220),
            width=max(1, int(2 * scale)),
        )

    image.paste(overlay, (0, 0), overlay)

    draw = ImageDraw.Draw(image)
    title_font = _font(int(68 * scale), bold=True)
    subtitle_font = _font(int(30 * scale), bold=False)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    title_x = (width - (title_box[2] - title_box[0])) / 2
    draw.text((title_x, 655 * scale), title, font=title_font, fill=(9, 55, 78))
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    subtitle_x = (width - (subtitle_box[2] - subtitle_box[0])) / 2
    draw.text((subtitle_x, 745 * scale), subtitle, font=subtitle_font, fill=(51, 85, 100))


def _generate_with_pillow(force: bool) -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = [
        (
            "dafeiyu_01.png",
            (74, 184, 232),
            (221, 246, 255),
            (32, 128, 190),
            "DA FEI YU",
            "PING PONG, ALWAYS ONLINE",
        ),
        (
            "dafeiyu_02.png",
            (255, 154, 82),
            (255, 233, 205),
            (224, 104, 44),
            "MO YU MODE",
            "NO WORK, ONLY FISH",
        ),
    ]
    generated: list[Path] = []
    for filename, body, belly, accent, title, subtitle in specs:
        output = OUT_DIR / filename
        if output.exists() and not force:
            generated.append(output)
            continue
        image = _background((226, 245, 255), (156, 212, 239), 900)
        _draw_fish(
            image,
            body=body,
            belly=belly,
            accent=accent,
            title=title,
            subtitle=subtitle,
        )
        image.save(output, format="PNG", optimize=True)
        generated.append(output)
        print(f"generated: {output}")
    return generated


# --------------------------------------------------------------- PNG fallback
def _chunk(tag: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", checksum)


def _save_rgb_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    stride = width * 3
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        start = y * (stride + 1)
        raw[start] = 0
        raw[start + 1 : start + 1 + stride] = pixels[y * stride : (y + 1) * stride]
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _put(pixels: bytearray, width: int, height: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < width and 0 <= y < height:
        index = (y * width + x) * 3
        pixels[index : index + 3] = bytes(color)


def _ellipse(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    rx: int,
    ry: int,
    color: tuple[int, int, int],
) -> None:
    for y in range(max(0, cy - ry), min(height, cy + ry + 1)):
        dy = (y - cy) / max(1, ry)
        span = int(rx * (1 - dy * dy) ** 0.5)
        for x in range(max(0, cx - span), min(width, cx + span + 1)):
            _put(pixels, width, height, x, y, color)


def _polygon(
    pixels: bytearray,
    width: int,
    height: int,
    points: list[tuple[int, int]],
    color: tuple[int, int, int],
) -> None:
    for y in range(max(0, min(p[1] for p in points)), min(height, max(p[1] for p in points) + 1)):
        nodes: list[float] = []
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            if (y1 <= y < y2) or (y2 <= y < y1):
                nodes.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        nodes.sort()
        for left, right in zip(nodes[0::2], nodes[1::2]):
            for x in range(max(0, int(left)), min(width, int(right) + 1)):
                _put(pixels, width, height, x, y, color)


def _generate_stdlib(force: bool) -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = [
        ("dafeiyu_01.png", (74, 184, 232), (221, 246, 255), (32, 128, 190)),
        ("dafeiyu_02.png", (255, 154, 82), (255, 233, 205), (224, 104, 44)),
    ]
    generated: list[Path] = []
    size = 900
    for filename, body, belly, accent in specs:
        output = OUT_DIR / filename
        if output.exists() and not force:
            generated.append(output)
            continue
        pixels = bytearray(size * size * 3)
        for y in range(size):
            ratio = y / (size - 1)
            color = tuple(int((226, 245, 255)[i] * (1 - ratio) + (156, 212, 239)[i] * ratio) for i in range(3))
            row = bytes(color) * size
            pixels[y * size * 3 : (y + 1) * size * 3] = row

        # Light card
        for y in range(90, 810):
            for x in range(60, 840):
                edge = x < 66 or x > 833 or y < 96 or y > 803
                _put(pixels, size, size, x, y, (12, 74, 110) if edge else (247, 252, 255))

        # Tail, fins, body, belly
        _polygon(pixels, size, size, [(620, 470), (790, 350), (760, 500), (790, 650), (620, 545)], accent)
        _polygon(pixels, size, size, [(390, 350), (500, 260), (530, 380)], accent)
        _polygon(pixels, size, size, [(390, 610), (500, 690), (530, 600)], accent)
        _ellipse(pixels, size, size, 440, 492, 252, 150, (8, 55, 80))
        _ellipse(pixels, size, size, 440, 492, 244, 142, body)
        _ellipse(pixels, size, size, 447, 545, 202, 75, belly)

        # Eye and bubbles
        _ellipse(pixels, size, size, 315, 460, 32, 32, (255, 255, 255))
        _ellipse(pixels, size, size, 319, 463, 15, 15, (21, 35, 45))
        _ellipse(pixels, size, size, 322, 466, 5, 5, (255, 255, 255))
        for cx, cy, radius in ((735, 260, 25), (780, 205, 16), (715, 180, 11)):
            _ellipse(pixels, size, size, cx, cy, radius, radius, (255, 255, 255))

        _save_rgb_png(output, size, size, pixels)
        generated.append(output)
        print(f"generated (stdlib fallback): {output}")
    return generated


def generate(force: bool = False) -> list[Path]:
    if Image is None:
        return _generate_stdlib(force)
    return _generate_with_pillow(force)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing images")
    args = parser.parse_args()
    generate(force=args.force)


if __name__ == "__main__":
    main()
