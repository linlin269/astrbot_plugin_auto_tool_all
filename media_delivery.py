"""Deliver tool-result media as base64 image/video messages, with cleanup.

搜索等工具的返回里出现的图片/视频 URL，统一由本模块下载、转为 base64 后
通过消息组件发送，避免 QQ 客户端拉取 URL 失败（防盗链/内网/被墙图床）。
临时文件在发送流程结束（无论成败）后立即删除，不在服务器留存媒体。
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .avatar_service import AvatarService

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
_VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "m4v", "flv", "3gp", "ts"}
_DOWNLOAD_CHUNK = 64 * 1024
# 3 的倍数分块，保证分块 base64 编码不对齐出错；最后一块单独收尾。
_ENCODE_CHUNK = 3 * 1024 * 1024
_MAX_REDIRECTS = 4
_USER_AGENT = "AstrBot-auto-tool-all/0.2"
_TEMP_MAX_AGE_SECONDS = 30 * 60


class MediaSkipError(RuntimeError):
    """The URL is not deliverable media (non-media type, over limit, unsafe)."""


@dataclass
class MediaDeliveryReport:
    sent_images: int = 0
    sent_videos: int = 0
    fallback_urls: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.sent_images:
            parts.append(f"{self.sent_images} 张图片")
        if self.sent_videos:
            parts.append(f"{self.sent_videos} 个视频")
        lines = []
        if parts:
            lines.append(f"已以 base64 方式发送 {'、'.join(parts)}。")
        if self.fallback_urls:
            lines.append("以下内容无法以媒体消息发送，可点开链接查看：")
            lines.extend(f"- {url}" for url in self.fallback_urls)
        return "\n".join(lines)


def extract_media_urls_from_text(text: str) -> list[str]:
    """Pull image/video URLs out of plain tool-result text (order preserved)."""
    urls: list[str] = []
    for raw in _HTTP_URL_RE.findall(str(text or "")):
        url = raw.rstrip(".,;、）)】>\"'")
        if _classify_by_extension(url) and url not in urls:
            urls.append(url)
    return urls


_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)】\]]+", re.IGNORECASE)

# 页面抓图：og/twitter 封面优先，其次 <img>；content 属性位置不限。
_META_IMAGE_TAG_RE = re.compile(
    r"<meta\b[^>]*?\b(?:property|name)\s*=\s*['\"]"
    r"(?:og:image(?::secure_url)?|twitter:image(?::src)?)['\"][^>]*>",
    re.IGNORECASE,
)
_META_CONTENT_RE = re.compile(r"\bcontent\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def extract_media_urls_from_html(
    html: str, base_url: str, limit: int = 10
) -> list[str]:
    """Pull image candidates out of page HTML: og/twitter meta first, then <img>.

    Relative URLs are resolved against base_url; only public http(s) addresses
    pass (data URIs and intranet hosts are dropped). Order preserved, deduped.
    """
    text = str(html or "")
    base = str(base_url or "")
    candidates: list[str] = []

    def _push(raw: str) -> None:
        if len(candidates) >= limit:
            return
        url = urljoin(base, str(raw or "").strip())
        if not url.lower().startswith(("http://", "https://")):
            return
        if not AvatarService.is_safe_http_url(url):
            return
        if url not in candidates:
            candidates.append(url)

    for tag in _META_IMAGE_TAG_RE.finditer(text):
        content = _META_CONTENT_RE.search(tag.group(0))
        if content:
            _push(content.group(1))
    for raw in _IMG_SRC_RE.findall(text):
        _push(raw)
    return candidates


def _classify_by_extension(url: str) -> str:
    try:
        suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    except (TypeError, ValueError):
        return ""
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    return ""


def _classify_by_content_type(content_type: str) -> str:
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("video/"):
        return "video"
    return ""


def _file_to_base64(path: Path) -> str:
    """Encode a file to base64 in chunks to keep peak memory bounded."""
    parts: list[str] = []
    remainder = b""
    with open(path, "rb") as handle:
        while True:
            data = handle.read(_ENCODE_CHUNK)
            if not data:
                break
            buffer = remainder + data
            cut = len(buffer) - len(buffer) % 3
            parts.append(base64.b64encode(buffer[:cut]).decode("ascii"))
            remainder = buffer[cut:]
    if remainder:
        parts.append(base64.b64encode(remainder).decode("ascii"))
    return "".join(parts)


class MediaDeliveryService:
    """Download http(s) media, send it as base64, and never leave files behind."""

    def __init__(
        self, data_dir: str | Path, config: Any | None = None, logger: Any | None = None
    ) -> None:
        self.config = config or {}
        self.logger = logger
        self.temp_dir = Path(data_dir) / "media_tmp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale()

    def _int_config(self, key: str, default: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, maximum))

    def image_max_bytes(self) -> int:
        return self._int_config("media_image_max_mb", 10, 50) * 1024 * 1024

    def video_max_bytes(self) -> int:
        return self._int_config("media_video_max_mb", 100, 500) * 1024 * 1024

    def max_count(self) -> int:
        return self._int_config("media_max_count", 5, 10)

    def page_max_bytes(self) -> int:
        return self._int_config("fetch_media_page_max_kb", 2048, 20480) * 1024

    def enabled(self) -> bool:
        value = self.config.get("deliver_media_base64", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)

    def _sweep_stale(self) -> None:
        """Remove leftovers from a crashed run so the temp dir stays empty."""
        now = time.time()
        try:
            candidates = list(self.temp_dir.iterdir())
        except OSError:
            return
        for path in candidates:
            try:
                if path.is_file() and now - path.stat().st_mtime > _TEMP_MAX_AGE_SECONDS:
                    path.unlink()
            except OSError:
                continue

    async def deliver(self, event: Any, urls: list[str]) -> MediaDeliveryReport:
        """Send up to max_count media items; fall back per item on any failure."""
        try:
            import astrbot.api.message_components as Comp
            from astrbot.core.message.message_event_result import MessageChain
        except ImportError as exc:  # pragma: no cover - only outside AstrBot
            raise MediaSkipError("无法导入 AstrBot 消息组件。") from exc

        report = MediaDeliveryReport()
        candidates: list[str] = []
        for raw in urls or []:
            url = str(raw or "").strip()
            if (
                url
                and url not in candidates
                and AvatarService.is_safe_http_url(url)
                and len(candidates) < self.max_count()
            ):
                candidates.append(url)

        images: list[tuple[str, str]] = []
        for url in candidates:
            try:
                kind, payload = await self._prepare(url)
            except MediaSkipError as exc:
                self._log("debug", "media skipped %s: %s", url, exc)
                report.fallback_urls.append(url)
                continue
            if kind == "image":
                images.append((url, payload))
                continue
            sent = await self._send_one(event, Comp, MessageChain, "video", url, payload)
            if sent:
                report.sent_videos += 1
            else:
                report.fallback_urls.append(url)

        if images:
            try:
                chain = MessageChain(
                    chain=[Comp.Image.fromBase64(payload) for _, payload in images]
                )
                await event.send(chain)
                report.sent_images = len(images)
            except Exception:
                self._log("warning", "batch image send failed, retrying one by one")
                for url, payload in images:
                    sent = await self._send_one(
                        event, Comp, MessageChain, "image", url, payload
                    )
                    if sent:
                        report.sent_images += 1
                    else:
                        report.fallback_urls.append(url)
        return report

    async def fetch_page_html(self, url: str) -> str:
        """Download one HTML page (type and size limited) for image extraction."""
        if not AvatarService.is_safe_http_url(url):
            raise MediaSkipError("地址不安全（内网/回环/非 http）。")
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - requirements installs it
            raise MediaSkipError("缺少 aiohttp 依赖。") from exc

        max_bytes = self.page_max_bytes()
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        current_url = url
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for _ in range(_MAX_REDIRECTS):
                if not AvatarService.is_safe_http_url(current_url):
                    raise MediaSkipError("重定向地址不安全。")
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"User-Agent": _USER_AGENT},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise MediaSkipError("无效重定向。")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise MediaSkipError(f"HTTP {response.status}。")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" not in content_type and not content_type.startswith(
                        "text/"
                    ):
                        raise MediaSkipError(f"非网页类型（{content_type or 'unknown'}）。")
                    body = await response.content.read(max_bytes + 1)
                    if not body:
                        raise MediaSkipError("页面为空。")
                    if len(body) > max_bytes:
                        raise MediaSkipError(f"页面超过 {max_bytes // 1024}KB 上限。")
                    charset = "utf-8"
                    for part in content_type.split(";"):
                        piece = part.strip()
                        if piece.startswith("charset="):
                            charset = piece.split("=", 1)[1].strip("'\" ") or "utf-8"
                    try:
                        return body.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        return body.decode("utf-8", errors="replace")
        raise MediaSkipError("重定向次数过多。")

    async def fetch_media_result(
        self, event: Any, urls: list[str], *, page_extract: bool = True
    ) -> str:
        """Download and send media from direct links or page links.

        Direct media links go straight to deliver(); page links are fetched and
        their og:image / <img> candidates extracted first. Extension-less links
        fall back to the content-type check in _download. Returns an LLM-facing
        summary that reports failure honestly instead of promising media.
        """
        candidates: list[str] = []
        page_urls: list[str] = []
        for raw in urls or []:
            url = str(raw or "").strip()
            if not url or not AvatarService.is_safe_http_url(url):
                continue
            if url in candidates or url in page_urls:
                continue
            if _classify_by_extension(url) or not page_extract:
                candidates.append(url)
            else:
                page_urls.append(url)
        cap = self.max_count()
        candidates = candidates[:cap]
        page_urls = page_urls[:cap]

        for url in page_urls:
            try:
                html = await self.fetch_page_html(url)
            except MediaSkipError as exc:
                self._log("debug", "page fetch skipped %s: %s", url, exc)
                # Not a page (or unreachable): let _download classify by type.
                candidates.append(url)
                continue
            found = extract_media_urls_from_html(html, url, limit=cap)
            if found:
                candidates.extend(found)
            else:
                candidates.append(url)

        if not candidates:
            return "没有可处理的链接：所有 URL 都为空或未通过安全校验。"

        report = await self.deliver(event, candidates)
        if report.sent_images or report.sent_videos:
            return report.summary()
        lines = ["这些链接里没有找到能下载发送的图片/视频。"]
        if report.fallback_urls:
            lines.append("可以把下面的原始链接发给用户自行查看：")
            lines.extend(f"- {url}" for url in report.fallback_urls)
        return "\n".join(lines)

    async def _send_one(
        self,
        event: Any,
        Comp: Any,
        MessageChain: Any,
        kind: str,
        url: str,
        payload: str,
    ) -> bool:
        """Send a single item as base64; on failure retry once as URL."""
        builder = Comp.Image if kind == "image" else Comp.Video
        try:
            await event.send(MessageChain(chain=[builder.fromBase64(payload)]))
            return True
        except Exception:
            self._log("warning", "%s base64 send failed, falling back to URL", kind)
        try:
            await event.send(MessageChain(chain=[builder.fromURL(url)]))
            return True
        except Exception:
            self._log("warning", "%s URL send failed too: %s", kind, url)
            return False

    async def _prepare(self, url: str) -> tuple[str, str]:
        """Download one media URL and return (kind, base64). Temp file deleted."""
        temp_path = self.temp_dir / f"{uuid.uuid4().hex}.bin"
        try:
            kind, size = await self._download(url, temp_path)
            payload = _file_to_base64(temp_path)
            self._log(
                "debug", "media prepared: kind=%s size=%d url=%s", kind, size, url
            )
            return kind, payload
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _download(self, url: str, destination: Path) -> tuple[str, int]:
        if not AvatarService.is_safe_http_url(url):
            raise MediaSkipError("地址不安全（内网/回环/非 http）。")
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - requirements installs it
            raise MediaSkipError("缺少 aiohttp 依赖。") from exc

        timeout = aiohttp.ClientTimeout(total=120, connect=10)
        current_url = url
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for _ in range(_MAX_REDIRECTS):
                if not AvatarService.is_safe_http_url(current_url):
                    raise MediaSkipError("重定向地址不安全。")
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={"User-Agent": _USER_AGENT},
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise MediaSkipError("无效重定向。")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise MediaSkipError(f"HTTP {response.status}。")
                    content_type = response.headers.get("Content-Type", "")
                    kind = _classify_by_content_type(content_type) or _classify_by_extension(
                        current_url
                    )
                    if not kind:
                        raise MediaSkipError(f"非媒体类型（{content_type or 'unknown'}）。")
                    max_bytes = (
                        self.image_max_bytes() if kind == "image" else self.video_max_bytes()
                    )
                    total = 0
                    try:
                        import aiofiles
                    except ImportError as exc:  # pragma: no cover
                        raise MediaSkipError("缺少 aiofiles 依赖，请更新 requirements.txt。") from exc
                    async with aiofiles.open(destination, "wb") as handle:
                        async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK):
                            total += len(chunk)
                            if total > max_bytes:
                                raise MediaSkipError(
                                    f"超过 {max_bytes // (1024 * 1024)}MB 上限。"
                                )
                            await handle.write(chunk)
                    if total == 0:
                        raise MediaSkipError("响应为空。")
                    return kind, total
        raise MediaSkipError("重定向次数过多。")

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if callable(method):
            try:
                method(message, *args)
            except (AttributeError, TypeError, RuntimeError):
                pass
