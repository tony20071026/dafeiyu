"""Local meme library with optional AI captions and topic matching.

The library keeps image files in ``assets/memes`` and a small JSON index in
``data/memes.json``.  Images received in groups can be collected automatically;
each new image is described by the multimodal chat model so the bot can decide
whether it fits the current topic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiofiles
import aiohttp

from config import Settings
from utils.media import read_limited

if TYPE_CHECKING:
    from utils.ai import AIClient

LOGGER = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# The group image upload endpoint accepts these formats (gif verified live).
GROUP_SENDABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}

# QQ's media CDN serves a small preview to some non-browser clients.  A normal
# browser UA is enough to get the original file for animated GIFs.
_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CONTENT_TYPE_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
}

# Screenshots, QR codes and documents make poor memes (and may contain private
# information), so the caption step removes them again.
_SKIP_KEYWORDS = (
    "截图",
    "屏幕截图",
    "聊天记录",
    "二维码",
    "文档",
    "表格",
    "扫描件",
    "证件",
    "身份证",
    "银行卡",
)

_INDEX_VERSION = 1


@dataclass(slots=True)
class MemeEntry:
    filename: str
    sha256: str = ""
    size: int = 0
    mtime: float = 0.0
    description: str = ""
    tags: tuple[str, ...] = ()
    added_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "mtime": self.mtime,
            "description": self.description,
            "tags": list(self.tags),
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, filename: str, data: dict[str, Any]) -> "MemeEntry":
        tags = data.get("tags")
        if not isinstance(tags, list):
            tags = []
        return cls(
            filename=filename,
            sha256=str(data.get("sha256") or ""),
            size=_safe_int(data.get("size")),
            mtime=_safe_float(data.get("mtime")),
            description=str(data.get("description") or "").strip(),
            tags=tuple(str(tag).strip() for tag in tags if str(tag).strip()),
            added_at=_safe_float(data.get("added_at")),
        )


def _download_variants(url: str) -> list[tuple[str, dict[str, str]]]:
    """Alternate requests to try when QQ returns a truncated preview."""

    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    urls: list[str] = [url]
    no_spec = [(key, value) for key, value in pairs if key != "spec"]
    for spec in ("0", "1", "2"):
        query = urlencode([*no_spec, ("spec", spec)])
        urls.append(urlunparse(parsed._replace(query=query)))
    if len(no_spec) != len(pairs):
        urls.append(urlunparse(parsed._replace(query=urlencode(no_spec))))

    variants: list[tuple[str, dict[str, str]]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for candidate in dict.fromkeys(urls):
        if candidate == url:
            continue
        for headers in (
            {"User-Agent": _DOWNLOAD_UA},
            {"User-Agent": _DOWNLOAD_UA, "Range": "bytes=0-"},
            {"User-Agent": _DOWNLOAD_UA, "Referer": "https://q.qq.com/"},
        ):
            key = (candidate, tuple(sorted(headers.items())))
            if key in seen:
                continue
            seen.add(key)
            variants.append((candidate, headers))
    return variants


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _suffix_from_magic(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ""


def _gif_complete(data: bytes) -> bool:
    """Return True when every GIF block (and the trailer) is present.

    QQ sometimes hands out a 16 KiB preview URL.  Such a file starts with a
    valid GIF header but is cut off in the middle of a frame, which makes the
    animation look wrong after saving.  This strict walk detects that.
    """

    if not data.startswith((b"GIF87a", b"GIF89a")) or len(data) < 13:
        return False
    pos = 6
    try:
        packed = data[pos + 4]
        pos += 7
        if packed & 0x80:
            pos += 3 * (2 ** ((packed & 0x07) + 1))
        while pos < len(data):
            marker = data[pos]
            if marker == 0x3B:
                return True
            if marker == 0x21:
                pos += 2
                while pos < len(data):
                    size = data[pos]
                    pos += 1
                    if size == 0:
                        break
                    pos += size
            elif marker == 0x2C:
                pos += 10
                frame_packed = data[pos - 1]
                if frame_packed & 0x80:
                    pos += 3 * (2 ** ((frame_packed & 0x07) + 1))
                pos += 1  # LZW minimum code size
                while pos < len(data):
                    size = data[pos]
                    pos += 1
                    if size == 0:
                        break
                    pos += size
            else:
                return False
            if pos > len(data):
                return False
    except IndexError:
        return False
    return False


class MemeLibrary:
    """Meme files plus an AI-generated description/tag index."""

    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession | None = None,
        ai_client: "AIClient | None" = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._ai = ai_client
        self._entries: dict[str, MemeEntry] = {}
        self._lock = asyncio.Lock()
        self._last_scan = 0.0
        self._analyzing = False

    @property
    def directory(self) -> Path:
        return self._settings.memes_dir

    @property
    def index_path(self) -> Path:
        return self._settings.meme_index_file

    def __len__(self) -> int:
        return len(self._entries)

    def stats(self) -> tuple[int, int]:
        ready = sum(1 for entry in self._entries.values() if entry.description)
        return len(self._entries), ready

    async def load(self) -> None:
        """Load the index and reconcile it with the files on disk."""

        self.directory.mkdir(parents=True, exist_ok=True)
        data: Any = {}
        try:
            async with aiofiles.open(self.index_path, "r", encoding="utf-8") as fh:
                data = json.loads(await fh.read())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            LOGGER.warning("读取表情包索引失败，将重建: %s", exc)
            data = {}

        raw_entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(raw_entries, dict):
            for filename, item in raw_entries.items():
                if isinstance(filename, str) and isinstance(item, dict):
                    self._entries[filename] = MemeEntry.from_dict(filename, item)
        elif isinstance(data, dict) and data:
            # Tolerate a plain ``filename -> metadata`` map from an older build.
            for filename, item in data.items():
                if isinstance(filename, str) and isinstance(item, dict):
                    self._entries[filename] = MemeEntry.from_dict(filename, item)

        async with self._lock:
            changed = await self._scan_locked()
            if changed:
                await self._save_locked()
        self._last_scan = time.monotonic()

    async def refresh(self, max_age: float = 30.0) -> bool:
        """Pick up images the user dropped into the directory while running.

        Returns ``True`` when at least one entry still needs an AI caption.
        """

        if time.monotonic() - self._last_scan < max_age:
            return self.has_pending()
        async with self._lock:
            changed = await self._scan_locked()
            if changed:
                await self._save_locked()
        self._last_scan = time.monotonic()
        return self.has_pending()

    def has_pending(self) -> bool:
        if self._ai is None or not self._settings.meme_analyze:
            return False
        return any(
            not entry.description for entry in self._entries.values()
        )

    async def _scan_locked(self) -> bool:
        changed = False
        files: dict[str, Path] = {}
        try:
            for path in self.directory.iterdir():
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    files[path.name] = path
        except OSError as exc:
            LOGGER.warning("读取表情包目录失败 %s: %s", self.directory, exc)
            return False

        for filename in list(self._entries):
            if filename not in files:
                LOGGER.info("表情包文件已删除，移出索引: %s", filename)
                del self._entries[filename]
                changed = True

        seen_hashes: dict[str, str] = {}
        for filename, path in sorted(files.items()):
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = self._entries.get(filename)
            if entry is None:
                entry = MemeEntry(filename=filename, added_at=time.time())
                self._entries[filename] = entry
                changed = True
            if entry.size != stat.st_size or abs(entry.mtime - stat.st_mtime) > 0.5:
                entry.size = stat.st_size
                entry.mtime = stat.st_mtime
                entry.description = ""
                entry.tags = ()
                entry.sha256 = await asyncio.to_thread(_hash_file, path)
                changed = True
            elif not entry.sha256:
                entry.sha256 = await asyncio.to_thread(_hash_file, path)
                changed = True
            if not entry.added_at:
                entry.added_at = stat.st_mtime or time.time()
                changed = True

            digest = entry.sha256
            if not digest:
                continue
            keeper_name = seen_hashes.get(digest)
            if keeper_name is None:
                seen_hashes[digest] = filename
                continue

            # Two files with identical bytes: keep the entry that already has
            # a description, otherwise keep the first one, and remove the
            # redundant file so it never shows up as a second random meme.
            keeper = self._entries.get(keeper_name)
            if (
                keeper is not None
                and not keeper.description
                and entry.description
            ):
                self._entries.pop(keeper_name, None)
                self._delete_duplicate_file(keeper_name)
                seen_hashes[digest] = filename
                changed = True
                LOGGER.info(
                    "发现重复表情包，保留有描述的 %s，删除 %s",
                    filename,
                    keeper_name,
                )
                continue

            self._entries.pop(filename, None)
            self._delete_duplicate_file(filename)
            changed = True
            LOGGER.info(
                "发现重复表情包（sha256=%s），删除 %s，保留 %s",
                digest[:12],
                filename,
                keeper_name,
            )
        return changed

    def _delete_duplicate_file(self, filename: str) -> None:
        path = self.directory / filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("删除重复表情包文件失败 %s: %s", filename, exc)

    def _find_by_hash(self, digest: str) -> MemeEntry | None:
        """Return an indexed entry with the same content hash, if any."""

        if not digest:
            return None
        for entry in self._entries.values():
            if entry.sha256 == digest:
                return entry
        return None

    async def _save_locked(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _INDEX_VERSION,
            "entries": {
                filename: entry.to_dict()
                for filename, entry in sorted(self._entries.items())
            },
        }
        temp_file = self.index_path.with_name(self.index_path.name + ".tmp")
        try:
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as fh:
                await fh.write(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                )
            os.replace(temp_file, self.index_path)
        except OSError as exc:
            LOGGER.warning("写入表情包索引失败: %s", exc)

    async def analyze_pending(self) -> None:
        """Describe images that do not have a caption yet (one call each)."""

        if self._ai is None or not self._settings.meme_analyze:
            return
        if self._analyzing:
            return
        self._analyzing = True
        try:
            await self._analyze_entries()
        finally:
            self._analyzing = False

    async def _analyze_entries(self) -> None:
        for filename in list(self._entries):
            entry = self._entries.get(filename)
            if entry is None or entry.description:
                continue
            path = self.directory / filename
            if not path.exists():
                continue
            try:
                caption = await self._ai.describe_image(path)
            except Exception as exc:  # AIError and unexpected provider failures
                LOGGER.warning("表情包识别失败 %s: %s", filename, exc)
                continue
            description = caption.description
            tags = caption.tags
            if not description:
                continue
            if not caption.is_meme or self._looks_like_non_meme(description, tags):
                LOGGER.info(
                    "表情包识别为普通照片/截图/二维码等，移除: %s | %s",
                    filename,
                    description,
                )
                await self._remove(filename)
                continue
            async with self._lock:
                current = self._entries.get(filename)
                if current is None:
                    continue
                current.description = description
                current.tags = tags
                await self._save_locked()
            LOGGER.info(
                "表情包已识别 %s | %s | 标签=%s",
                filename,
                description,
                ",".join(tags) or "-",
            )

    async def collect_from_urls(
        self, urls: Sequence[str], *, alias: str = ""
    ) -> list[MemeEntry]:
        """Download group image URLs into the meme library.

        Returns the new (or, for duplicates, existing) entries so callers can
        immediately tag and send a manually saved image.
        """

        if not urls or self._session is None:
            return []
        collected: list[MemeEntry] = []
        for url in list(urls)[:4]:
            try:
                data, content_type = await self._download(url)
            except Exception as exc:
                LOGGER.warning("下载群图片失败 %s: %s", url[:120], exc)
                continue
            if not data:
                continue

            digest = hashlib.sha256(data).hexdigest()
            async with self._lock:
                existing = self._find_by_hash(digest)
            if existing is not None:
                if alias:
                    existing = await self.apply_alias(existing, alias)
                collected.append(existing)
                continue

            entry = await self.add_bytes(
                data, content_type, source_url=url, alias=alias
            )
            if entry is not None:
                collected.append(entry)
        return collected

    async def apply_alias(self, entry: MemeEntry, alias: str) -> MemeEntry:
        """Add a human-provided name/tag (e.g. “哈哈哈”) to an entry."""

        alias = (alias or "").strip()[:20]
        if not alias:
            return entry
        async with self._lock:
            current = self._entries.get(entry.filename)
            if current is None:
                return entry
            tags = [tag for tag in current.tags if tag != alias]
            current.tags = tuple([alias, *tags][:8])
            if not current.description:
                current.description = alias
            elif alias not in current.description:
                current.description = f"{alias}：{current.description}"[:120]
            await self._save_locked()
            return current

    async def _download(self, url: str) -> tuple[bytes, str]:
        if not url.lower().startswith(("http://", "https://")):
            return b"", ""
        data, content_type, length = await self._fetch_image(
            url, {"User-Agent": _DOWNLOAD_UA}
        )
        if not data:
            return b"", ""

        # QQ may hand out a 16 KiB preview even when the original GIF is much
        # larger.  Retry with a browser-like request and alternate URL specs.
        looks_gif = data.startswith((b"GIF87a", b"GIF89a")) or content_type == "image/gif"
        if looks_gif and not _gif_complete(data):
            LOGGER.warning(
                "GIF 下载不完整（收到 %s 字节，Content-Length=%s），尝试获取原图 | %s",
                len(data),
                length or "?",
                url[:120],
            )
            best_data, best_type = data, content_type or "image/gif"
            for variant_url, headers in _download_variants(url):
                alt, alt_type, alt_length = await self._fetch_image(
                    variant_url, headers
                )
                if not alt:
                    continue
                alt_is_gif = alt.startswith((b"GIF87a", b"GIF89a"))
                if alt_is_gif and not _gif_complete(alt):
                    continue
                if len(alt) > len(best_data):
                    best_data = alt
                    best_type = alt_type or best_type
                    if not alt_is_gif or _gif_complete(alt):
                        LOGGER.info(
                            "已获取完整 GIF：%s 字节（Content-Length=%s）| %s",
                            len(alt),
                            alt_length or "?",
                            variant_url[:120],
                        )
                        break
            data, content_type = best_data, best_type
            if not _gif_complete(data):
                LOGGER.warning(
                    "GIF 仍然不完整，放弃入库以避免保存损坏的动画 | %s",
                    url[:120],
                )
                return b"", ""

        max_bytes = self._settings.max_local_image_bytes
        if len(data) > max_bytes:
            LOGGER.warning(
                "群图片超过 %sMB，不加入表情包库: %s",
                self._settings.max_local_image_mb,
                url[:120],
            )
            return b"", ""
        if not content_type.startswith("image/"):
            guessed = mimetypes.guess_type(urlparse(url).path)[0] or ""
            if guessed.startswith("image/"):
                content_type = guessed
        if not content_type.startswith("image/"):
            magic = _suffix_from_magic(data)
            if magic:
                content_type = f"image/{magic.lstrip('.')}"
            else:
                LOGGER.info("忽略非图片附件: %s", url[:120])
                return b"", ""
        LOGGER.info(
            "图片下载完成 | %s 字节 | Content-Length=%s | type=%s",
            len(data),
            length or "?",
            content_type or "?",
        )
        return data, content_type

    async def _fetch_image(
        self, url: str, headers: dict[str, str]
    ) -> tuple[bytes, str, int]:
        max_bytes = self._settings.max_local_image_bytes
        async with self._session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status >= 400:
                LOGGER.warning(
                    "群图片下载 HTTP %s: %s", response.status, url[:120]
                )
                return b"", "", 0
            content_type = (
                response.headers.get("Content-Type") or ""
            ).split(";")[0].strip().lower()
            try:
                length = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            data, exceeded = await read_limited(response, max_bytes)
        if length and len(data) < length:
            LOGGER.warning(
                "群图片只收到 %s/%s 字节 | %s",
                len(data),
                length,
                url[:120],
            )
        if exceeded:
            LOGGER.warning(
                "群图片超过 %sMB，不加入表情包库 | %s",
                self._settings.max_local_image_mb,
                url[:120],
            )
            return b"", "", length
        return data, content_type, length

    async def add_bytes(
        self,
        data: bytes,
        content_type: str,
        *,
        source_url: str = "",
        alias: str = "",
    ) -> MemeEntry | None:
        """Store an image if it is new and still within the library limit."""

        if not data:
            return None
        suffix = _CONTENT_TYPE_SUFFIX.get(content_type.lower())
        if suffix is None:
            guessed = mimetypes.guess_type(urlparse(source_url).path)[0] or ""
            suffix = _CONTENT_TYPE_SUFFIX.get(guessed.lower())
        if suffix is None:
            suffix = _suffix_from_magic(data)
        if suffix not in GROUP_SENDABLE_SUFFIXES:
            LOGGER.info(
                "跳过非群聊可发送格式的表情包: content_type=%s", content_type or "?"
            )
            return None

        digest = hashlib.sha256(data).hexdigest()
        async with self._lock:
            if len(self._entries) >= self._settings.meme_max_files:
                LOGGER.warning(
                    "表情包库已达上限 %s，跳过新图片",
                    self._settings.meme_max_files,
                )
                return None
            if self._find_by_hash(digest) is not None:
                LOGGER.info("重复表情包，跳过: %s", digest[:12])
                return None
            filename = f"group_{digest[:12]}{suffix}"
            path = self.directory / filename
            if path.exists():
                return None
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                async with aiofiles.open(path, "wb") as fh:
                    await fh.write(data)
            except OSError as exc:
                LOGGER.warning("保存表情包失败 %s: %s", filename, exc)
                return None
            stat = path.stat()
            entry = MemeEntry(
                filename=filename,
                sha256=digest,
                size=stat.st_size,
                mtime=stat.st_mtime,
                added_at=time.time(),
            )
            self._entries[filename] = entry
            await self._save_locked()
        LOGGER.info("新表情包已入库 %s（%sKB）", filename, len(data) // 1024)

        if self._ai is not None and self._settings.meme_analyze:
            try:
                caption = await self._ai.describe_image(path)
            except Exception as exc:
                LOGGER.warning("新表情包识别失败 %s: %s", filename, exc)
                if alias:
                    await self.apply_alias(entry, alias)
                return entry
            description = caption.description
            tags = caption.tags
            if (
                (not caption.is_meme or self._looks_like_non_meme(description, tags))
                and not alias
            ):
                LOGGER.info(
                    "新图片识别为普通照片/截图/二维码等，不加入表情包库: %s | %s",
                    filename,
                    description,
                )
                await self._remove(filename)
                return None
            if description:
                async with self._lock:
                    current = self._entries.get(filename)
                    if current is not None:
                        current.description = description
                        current.tags = tags
                        await self._save_locked()
                LOGGER.info(
                    "新表情包已识别 %s | %s | 标签=%s",
                    filename,
                    description,
                    ",".join(tags) or "-",
                )
        if alias:
            entry = await self.apply_alias(entry, alias)
            LOGGER.info("表情包已命名 %s | %s", filename, alias)
        return entry

    async def _remove(self, filename: str) -> None:
        path = self.directory / filename
        async with self._lock:
            self._entries.pop(filename, None)
            await self._save_locked()
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError as exc:
            LOGGER.warning("删除表情包文件失败 %s: %s", filename, exc)

    @staticmethod
    def _looks_like_non_meme(description: str, tags: Iterable[str]) -> bool:
        combined = f"{description} {' '.join(tags)}"
        return any(keyword in combined for keyword in _SKIP_KEYWORDS)

    def resolve(self, filename: str) -> Path | None:
        name = (filename or "").strip().strip("`'\"")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        entry = self._entries.get(name)
        if entry is None and name not in {p.name for p in self._image_files()}:
            return None
        path = self.directory / name
        return path if path.is_file() else None

    def random_entry(self) -> Path | None:
        files = self._image_files()
        if not files:
            return None
        return random.choice(files)

    def search(self, query: str, limit: int = 1) -> list[MemeEntry]:
        """Find memes by filename, description or tag (case-insensitive)."""

        needle = (query or "").strip().lower()
        if not needle:
            return []
        scored: list[tuple[int, MemeEntry]] = []
        for entry in self._entries.values():
            score = 0
            if needle in entry.filename.lower():
                score += 5
            if needle in entry.description.lower():
                score += 4
            for tag in entry.tags:
                tag_lower = tag.lower()
                if needle == tag_lower:
                    score += 6
                elif needle in tag_lower:
                    score += 3
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], -item[1].added_at))
        return [entry for _, entry in scored[:limit]]

    def _image_files(self) -> list[Path]:
        try:
            return [
                path
                for path in self.directory.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            ]
        except OSError:
            return []

    def catalog_for(self, text: str, limit: int | None = None) -> list[MemeEntry]:
        """Pick memes whose tags/description overlap with the current topic."""

        limit = limit or self._settings.meme_catalog_limit
        lowered = (text or "").lower()
        entries = [
            entry for entry in self._entries.values() if entry.description
        ]
        if not entries:
            return []

        scored: list[tuple[int, MemeEntry]] = []
        for entry in entries:
            score = 0
            for tag in entry.tags:
                tag_lower = tag.lower().strip()
                if len(tag_lower) >= 2 and tag_lower in lowered:
                    score += 3
            for token in re.split(r"[\s，。！？、,.!?；;：:（）()\[\]【】/|]+", entry.description):
                token = token.strip().lower()
                if 2 <= len(token) <= 8 and token in lowered:
                    score += 1
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda item: (-item[0], -item[1].added_at))
        selected = [entry for _, entry in scored[:limit]]
        if len(selected) < limit:
            remaining = [entry for entry in entries if entry not in selected]
            random.shuffle(remaining)
            # Give the model a few unrelated options too, but never a huge list.
            fill = min(limit - len(selected), 4 if selected else 5)
            selected.extend(remaining[:fill])
        return selected

    def catalog_text(self, text: str, limit: int | None = None) -> str:
        entries = self.catalog_for(text, limit)
        if not entries:
            return ""
        lines = []
        for entry in entries:
            tags = "、".join(entry.tags[:5]) or "-"
            description = entry.description.replace("\n", " ")[:60]
            lines.append(f"- {entry.filename}：{description}（标签：{tags}）")
        return "\n".join(lines)