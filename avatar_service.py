"""QQ avatar resolution, downloading, and bounded reference-image caching."""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

_QQ_RE = re.compile(r"^\d{4,12}$")
_ALLOWED_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class AvatarTarget:
    """Resolved identity used as an image reference."""

    identity: str
    qq: str
    url: str
    path: Path


class AvatarError(RuntimeError):
    """Raised when an identity cannot be converted to a usable avatar."""


class AvatarService:
    """Resolve QQ identities and cache downloaded images outside the plugin folder."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        config: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self.config = config or {}
        self.logger = logger
        self.data_dir = Path(data_dir)
        self.avatar_dir = self.data_dir / "avatars"
        self.reference_dir = self.data_dir / "references"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self.reference_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_identity(identity: Any) -> str:
        value = str(identity or "bot").strip().lower()
        aliases = {
            "you": "bot",
            "yourself": "bot",
            "你": "bot",
            "机器人": "bot",
            "自己": "bot",
            "我的": "sender",
            "我": "sender",
            "me": "sender",
            "user": "sender",
            "发送者": "sender",
            "本人": "sender",
            "他": "at",
            "她": "at",
            "ta": "at",
            "他人": "at",
            "被@的人": "at",
            "被at的人": "at",
        }
        return aliases.get(value, value if value in {"bot", "sender", "at"} else "bot")

    @staticmethod
    def _first_nonempty(values: Iterable[Any]) -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @classmethod
    def _valid_qq(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not _QQ_RE.fullmatch(text):
            return ""
        return text

    async def get_bot_id(self, event: Any) -> str:
        """Read the OneBot account id, with get_login_info as a fallback."""
        message_obj = getattr(event, "message_obj", None)
        candidates = [
            getattr(event, "get_self_id", lambda: "")(),
            getattr(event, "self_id", ""),
            getattr(message_obj, "self_id", ""),
        ]
        bot_id = self._valid_qq(self._first_nonempty(candidates))
        if bot_id:
            return bot_id

        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            return ""

        try:
            result = await call_action("get_login_info")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._log("debug", "get_login_info failed: %s", exc)
            return ""
        payload = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(payload, dict):
            return self._valid_qq(
                self._first_nonempty(
                    payload.get(key) for key in ("user_id", "qq", "uin", "id")
                )
            )
        return ""

    @staticmethod
    def sender_id(event: Any) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value).strip()
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        return str(
            AvatarService._first_nonempty(
                [
                    getattr(sender, "user_id", ""),
                    getattr(sender, "id", ""),
                    getattr(message_obj, "sender_id", ""),
                ]
            )
        ).strip()

    async def resolve_for_event(
        self, event: Any, identity: Any = "bot"
    ) -> AvatarTarget:
        normalized = self.normalize_identity(identity)
        bot_id = await self.get_bot_id(event)
        if normalized == "bot":
            qq = bot_id
        elif normalized == "sender":
            qq = self.sender_id(event)
        else:
            qq = self._find_first_at_id(event, excluded={bot_id, "all"})

        qq = self._valid_qq(qq)
        if not qq:
            labels = {"bot": "机器人", "sender": "发送者", "at": "被@用户"}
            raise AvatarError(f"无法取得{labels.get(normalized, '目标')}的 QQ 号。")

        try:
            spec = int(self.config.get("avatar_spec", 640))
        except (TypeError, ValueError):
            spec = 640
        url = self.avatar_url(qq, spec=spec)
        path = await self.get_or_download(url, namespace="avatar", key=f"{qq}:{spec}")
        return AvatarTarget(identity=normalized, qq=qq, url=url, path=path)

    @staticmethod
    def avatar_url(qq: str, spec: int = 640) -> str:
        # q4 is the same QQ avatar service already used by selfie_image.
        return f"https://q4.qlogo.cn/headimg_dl?dst_uin={qq}&spec={int(spec)}"

    def _find_first_at_id(self, event: Any, excluded: set[str]) -> str:
        try:
            from .event_images import extract_at_ids
        except ImportError:  # direct execution during a simple local test
            return ""
        for value in extract_at_ids(event):
            if str(value) not in excluded:
                return str(value)
        return ""

    def cache_ttl_seconds(self) -> int:
        try:
            minutes = int(self.config.get("avatar_cache_ttl_minutes", 360))
        except (TypeError, ValueError):
            minutes = 360
        return max(60, min(minutes, 7 * 24 * 60)) * 60

    async def get_or_download(
        self,
        url: str,
        *,
        namespace: str,
        key: str,
    ) -> Path:
        safe_key = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:24]
        directory = self.avatar_dir if namespace == "avatar" else self.reference_dir
        candidates = list(directory.glob(f"{safe_key}.*"))
        for candidate in candidates:
            if candidate.is_file() and self._is_fresh(candidate):
                return candidate
        return await self._download_to_path(url, directory / f"{safe_key}.jpg")

    async def download_external_images(
        self,
        urls: Iterable[Any],
        *,
        max_count: int | None = None,
    ) -> list[Path]:
        try:
            configured_max = int(self.config.get("max_external_images", 3))
        except (TypeError, ValueError):
            configured_max = 3
        limit = max_count if max_count is not None else configured_max
        limit = max(0, min(int(limit), 8))
        if limit == 0:
            return []
        paths: list[Path] = []
        seen: set[str] = set()
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not self.is_safe_http_url(url) or url in seen:
                continue
            seen.add(url)
            try:
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                paths.append(
                    await self.get_or_download(url, namespace="reference", key=digest)
                )
            except Exception as exc:
                self._log("warning", "external reference download failed: %s", exc)
            if len(paths) >= limit:
                break
        return paths

    def _is_fresh(self, path: Path) -> bool:
        try:
            return (time.time() - path.stat().st_mtime) <= self.cache_ttl_seconds()
        except OSError:
            return False

    async def _download_to_path(self, url: str, destination: Path) -> Path:
        if not self.is_safe_http_url(url):
            raise AvatarError("只允许下载 http/https 图片地址。")
        try:
            max_bytes = int(self.config.get("external_image_max_mb", 10)) * 1024 * 1024
        except (TypeError, ValueError):
            max_bytes = 10 * 1024 * 1024
        max_bytes = max(256 * 1024, min(max_bytes, 50 * 1024 * 1024))

        data, content_type = await self._fetch(url, max_bytes)
        if not data:
            raise AvatarError("图片响应为空。")
        extension = _ALLOWED_MIME_EXTENSIONS.get(
            content_type.lower().split(";", 1)[0].strip()
        )
        if not extension:
            extension = (
                mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
                or ".jpg"
            )
            if extension not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                extension = ".jpg"
        destination = destination.with_suffix(extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="avatar-", suffix=extension, dir=destination.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return destination

    async def _fetch(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - requirements installs aiohttp
            raise AvatarError(
                "缺少 aiohttp 依赖，请安装插件 requirements.txt。"
            ) from exc

        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        current_url = url
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for _ in range(4):
                if not self.is_safe_http_url(current_url):
                    raise AvatarError("图片地址或重定向地址不安全。")
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"User-Agent": "AstrBot-auto-tool-all/0.1"},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise AvatarError("图片服务器返回了无效重定向。")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise AvatarError(f"图片服务器返回 HTTP {response.status}。")
                    content_type = response.headers.get("Content-Type", "image/jpeg")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise AvatarError("图片超过大小限制。")
                        chunks.append(chunk)
                    return b"".join(chunks), content_type
        raise AvatarError("图片重定向次数过多。")

    @staticmethod
    def is_safe_http_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except (TypeError, ValueError):
            return False
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        )

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if callable(method):
            try:
                method(message, *args)
            except (AttributeError, TypeError, RuntimeError):
                pass
