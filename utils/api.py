"""Thin async client for the QQ Open Platform HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiofiles
import aiohttp

from config import Settings
from utils.auth import TokenManager
from utils.text import truncate_text

LOGGER = logging.getLogger(__name__)

GROUP_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
CHANNEL_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class QQApiError(RuntimeError):
    """An HTTP/business error returned by QQ Open Platform."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | str | None = None,
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.path = path


class ImageAssetError(ValueError):
    """A local image cannot be sent (missing, too large, wrong format)."""


class QQApiClient:
    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        token_manager: TokenManager,
    ) -> None:
        self._settings = settings
        self._session = session
        self._tokens = token_manager
        self._base_url = settings.api_base
        self._seq: OrderedDict[str, int] = OrderedDict()
        self._seq_lock = asyncio.Lock()

    # ------------------------------------------------------------------ HTTP
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: aiohttp.FormData | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = await self._tokens.get_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "User-Agent": "DafeiyuQQBot/1.0 (+python-aiohttp)",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._settings.http_timeout),
            ) as response:
                status = response.status
                raw = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise QQApiError(f"请求 {method} {path} 失败: {exc}", path=path) from exc

        payload: dict[str, Any] = {}
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
                else:
                    payload = {"data": parsed}
            except json.JSONDecodeError:
                payload = {"raw": raw[:600]}

        if retry_auth and (
            status == 401 or str(payload.get("code")) in {"11244", "11245"}
        ):
            LOGGER.warning("Access Token 被服务端拒绝，强制刷新后重试 %s", path)
            await self._tokens.invalidate()
            return await self._request(
                method,
                path,
                json_body=json_body,
                data=data,
                params=params,
                retry_auth=False,
            )

        code = payload.get("code")
        if status >= 400 or code not in (None, 0, "0"):
            message = payload.get("message") or payload.get("msg") or raw[:300]
            raise QQApiError(
                f"{method} {path} 失败: HTTP {status}, code={code}, message={message}",
                status=status,
                code=code,
                path=path,
            )
        return payload

    async def get_gateway(self) -> str:
        payload = await self._request("GET", self._settings.gateway_path)
        url = str(payload.get("url") or "").strip()
        if not url:
            raise QQApiError("网关响应中没有 url", path=self._settings.gateway_path)
        return url

    # --------------------------------------------------------------- messages
    async def _next_seq(self, message_id: str) -> int:
        async with self._seq_lock:
            seq = self._seq.get(message_id, 1)
            self._seq[message_id] = seq + 1
            self._seq.move_to_end(message_id)
            while len(self._seq) > 4096:
                self._seq.popitem(last=False)
            return seq

    async def send_group_text(
        self, group_openid: str, content: str, message_id: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "msg_type": 0,
            "content": truncate_text(content, self._settings.max_message_chars),
        }
        # An empty message_id means a proactive message: QQ expects msg_id and
        # msg_seq to be omitted rather than sent as empty/passive fields.
        if message_id:
            body["msg_id"] = message_id
            body["msg_seq"] = await self._next_seq(message_id)
        return await self._request(
            "POST", f"/v2/groups/{quote(group_openid, safe='')}/messages", json_body=body
        )

    async def send_channel_text(
        self, channel_id: str, content: str, message_id: str
    ) -> dict[str, Any]:
        body = {
            "content": truncate_text(content, self._settings.max_message_chars),
            "msg_id": message_id,
        }
        return await self._request(
            "POST", f"/channels/{quote(channel_id, safe='')}/messages", json_body=body
        )

    async def send_c2c_text(
        self, user_openid: str, content: str, message_id: str
    ) -> dict[str, Any]:
        body = {
            "msg_type": 0,
            "content": truncate_text(content, self._settings.max_message_chars),
            "msg_id": message_id,
            "msg_seq": await self._next_seq(message_id),
        }
        return await self._request(
            "POST", f"/v2/users/{quote(user_openid, safe='')}/messages", json_body=body
        )

    # ---------------------------------------------------------------- images
    async def send_group_image(
        self, group_openid: str, image: str | Path, message_id: str
    ) -> dict[str, Any]:
        payload = await self._upload_image("groups", group_openid, image)
        file_info = payload.get("file_info")
        if not file_info:
            raise QQApiError("群图片上传成功但响应缺少 file_info", path="/v2/groups/files")
        body = {
            "msg_type": 7,
            "msg_id": message_id,
            "msg_seq": await self._next_seq(message_id),
            "media": {"file_info": file_info},
        }
        return await self._request(
            "POST", f"/v2/groups/{quote(group_openid, safe='')}/messages", json_body=body
        )

    async def send_c2c_image(
        self, user_openid: str, image: str | Path, message_id: str
    ) -> dict[str, Any]:
        payload = await self._upload_image("users", user_openid, image)
        file_info = payload.get("file_info")
        if not file_info:
            raise QQApiError("单聊图片上传成功但响应缺少 file_info", path="/v2/users/files")
        body = {
            "msg_type": 7,
            "msg_id": message_id,
            "msg_seq": await self._next_seq(message_id),
            "media": {"file_info": file_info},
        }
        return await self._request(
            "POST", f"/v2/users/{quote(user_openid, safe='')}/messages", json_body=body
        )

    async def send_channel_image(
        self, channel_id: str, image: str | Path, message_id: str
    ) -> dict[str, Any]:
        endpoint = f"/channels/{quote(channel_id, safe='')}/messages"
        if isinstance(image, str) and image.lower().startswith(("http://", "https://")):
            return await self._request(
                "POST",
                endpoint,
                json_body={"image": image, "msg_id": message_id},
            )

        data, filename, content_type = await self._read_local_image(
            Path(image), CHANNEL_IMAGE_SUFFIXES
        )
        form = aiohttp.FormData()
        form.add_field("msg_id", message_id)
        form.add_field("content", "")
        form.add_field(
            "file_image",
            data,
            filename=filename,
            content_type=content_type,
        )
        return await self._request("POST", endpoint, data=form)

    async def _upload_image(
        self, scope: str, target_id: str, image: str | Path
    ) -> dict[str, Any]:
        if isinstance(image, str) and image.lower().startswith(("http://", "https://")):
            return await self._upload_image_url(scope, target_id, image)
        return await self._upload_image_file(scope, target_id, Path(image))

    async def _upload_image_url(
        self, scope: str, target_id: str, url: str
    ) -> dict[str, Any]:
        body = {"file_type": 1, "url": url, "srv_send_msg": False}
        return await self._request(
            "POST",
            f"/v2/{scope}/{quote(target_id, safe='')}/files",
            json_body=body,
        )

    async def _upload_image_file(
        self, scope: str, target_id: str, path: Path
    ) -> dict[str, Any]:
        data, filename, content_type = await self._read_local_image(
            path, GROUP_IMAGE_SUFFIXES
        )
        # The official recommended path is the chunked-upload flow: prepare,
        # PUT every part to its presigned URL, confirm each part, then merge with
        # upload_id.  It also supports files larger than the 5MB direct limit.
        try:
            return await self._upload_file_chunked(
                scope, target_id, data, filename
            )
        except QQApiError as exc:
            LOGGER.warning("%s 图片分片上传失败，回退到 multipart 直传：%s", scope, exc)

        # Compatibility fallback: direct binary via multipart form-data.  Keeping
        # srv_send_msg=false returns file_info so the reply can be sent with the
        # triggering message_id (passive-message compliance).
        form = aiohttp.FormData()
        form.add_field("file_type", "1")
        form.add_field("file_name", filename)
        form.add_field("srv_send_msg", "false")
        form.add_field("file_data", data, filename=filename, content_type=content_type)
        return await self._request(
            "POST",
            f"/v2/{scope}/{quote(target_id, safe='')}/files",
            data=form,
        )

    async def _upload_file_chunked(
        self, scope: str, target_id: str, data: bytes, filename: str
    ) -> dict[str, Any]:
        endpoint = f"/v2/{scope}/{quote(target_id, safe='')}"
        prepare_body = {
            "file_type": 1,
            "file_size": str(len(data)),
            "file_name": filename,
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "md5_10m": hashlib.md5(data[:10_002_432]).hexdigest(),
        }
        prepared = await self._request(
            "POST", f"{endpoint}/upload_prepare", json_body=prepare_body
        )
        upload_id = str(prepared.get("upload_id") or "")
        parts = prepared.get("parts") or []
        if not upload_id or not isinstance(parts, list) or not parts:
            raise QQApiError("分片预上传响应缺少 upload_id 或 parts", path=endpoint)
        if not all(isinstance(item, dict) for item in parts):
            raise QQApiError("分片预上传响应格式错误", path=endpoint)

        offset = 0
        for part in sorted(parts, key=lambda item: int(item.get("index", 0))):
            index = int(part.get("index", 0))
            block_size = int(part.get("block_size") or 0)
            presigned_url = str(part.get("presigned_url") or "")
            if block_size <= 0 or not presigned_url:
                raise QQApiError("分片预上传响应缺少 URL 或大小", path=endpoint)
            chunk = data[offset : offset + block_size]
            if not chunk:
                raise QQApiError("分片数据为空，预上传大小不匹配", path=endpoint)
            await self._put_presigned_part(presigned_url, chunk)
            await self._request(
                "POST",
                f"{endpoint}/upload_part_finish",
                json_body={
                    "upload_id": upload_id,
                    "part_index": index,
                    "block_size": str(len(chunk)),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
            )
            offset += len(chunk)

        if offset != len(data):
            raise QQApiError(
                f"分片总大小不匹配：已上传 {offset} / {len(data)} 字节",
                path=endpoint,
            )

        merge_body = {
            "file_type": 1,
            "file_name": filename,
            "srv_send_msg": False,
            "upload_id": upload_id,
        }
        return await self._request(
            "POST", f"{endpoint}/files", json_body=merge_body
        )

    async def _put_presigned_part(self, url: str, data: bytes) -> None:
        try:
            async with self._session.put(
                url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=self._settings.http_timeout),
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise QQApiError(
                        f"分片 PUT 失败: HTTP {response.status}, {body[:200]}"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise QQApiError(f"分片 PUT 请求失败: {exc}") from exc

    async def _read_local_image(
        self, path: Path, allowed_suffixes: set[str]
    ) -> tuple[bytes, str, str]:
        if not path.is_file():
            raise ImageAssetError(f"图片文件不存在：{path.name}")
        suffix = path.suffix.lower()
        if suffix not in allowed_suffixes:
            supported = "/".join(sorted(item.lstrip(".") for item in allowed_suffixes))
            raise ImageAssetError(f"不支持的图片格式：{suffix or '未知'}（支持 {supported}）")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ImageAssetError(f"无法读取图片：{path.name}") from exc
        if size <= 0:
            raise ImageAssetError(f"图片为空：{path.name}")
        if size > self._settings.max_local_image_bytes:
            limit = self._settings.max_local_image_mb
            raise ImageAssetError(
                f"图片 {path.name} 为 {size / 1024 / 1024:.1f}MB，"
                f"超过本地上传上限 {limit:g}MB"
            )
        try:
            async with aiofiles.open(path, "rb") as fh:
                data = await fh.read()
        except OSError as exc:
            raise ImageAssetError(f"读取图片失败：{path.name}") from exc
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return data, path.name, content_type
