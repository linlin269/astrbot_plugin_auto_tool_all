"""Deliver tool-result media as base64 image/video messages, with cleanup.

搜索等工具的返回里出现的图片/视频 URL，统一由本模块下载、转为 base64 后
通过消息组件发送，避免 QQ 客户端拉取 URL 失败（防盗链/内网/被墙图床）。
临时文件在发送流程结束（无论成败）后立即删除，不在服务器留存媒体。
"""

from __future__ import annotations

import base64
import json
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
_USER_AGENT = "AstrBot-auto-tool-all/0.3"
_TEMP_MAX_AGE_SECONDS = 30 * 60

# 已下载待发送的媒体条目：(url, kind, base64)。
_PreparedMedia = tuple[str, str, str]


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

# 页面抓图：og/twitter 封面优先，其次 <video poster> 与 <img>（含懒加载 data-*）。
_META_IMAGE_TAG_RE = re.compile(
    r"<meta\b[^>]*?\b(?:property|name)\s*=\s*['\"]"
    r"(?:og:image(?::secure_url)?|twitter:image(?::src)?)['\"][^>]*>",
    re.IGNORECASE,
)
_META_CONTENT_RE = re.compile(r"\bcontent\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_VIDEO_TAG_RE = re.compile(r"<video\b[^>]*>", re.IGNORECASE)

# 噪声 URL 特征（词边界匹配，避免 catalog/logotype/headphones 之类误杀）：
# 图标、站标、表情、二维码、验证码、占位图与跟踪像素。
_NOISE_URL_TOKEN_RE = re.compile(
    r"(?:^|[/._-])(?:favicon|logo|icon|sprite|emoji|emote|badge|qrcode|qr_code"
    r"|captcha|placeholder|spacer|beacon|tracking|analytics)(?:[/._-]|$)",
    re.IGNORECASE,
)
# 头像类特征（子串匹配：avatar 等词极少出现在内容图 URL 中）。
_NOISE_URL_SUBSTR_RE = re.compile(
    r"avatar|gravatar|qlogo|profile_?pic|user_?icon|member_?icon|/face/",
    re.IGNORECASE,
)
_NOISE_EXT_RE = re.compile(r"\.(?:ico|svg)(?:[?#]|$)", re.IGNORECASE)
# class/id/alt 语义词（子串匹配类名，如 "u-icon"、"site-logo"）。
_NOISE_ATTR_SUBSTR_RE = re.compile(
    r"avatar|logo|icon|favicon|badge|emoji|qrcode|captcha|sponsor", re.IGNORECASE
)
# UI 控件类特征（词边界匹配，避免 closeup 被误判为 close 按钮）。
_NOISE_ATTR_TOKEN_RE = re.compile(
    r"(?:^|[\s_-])(?:btn|button|share|close)(?:[\s_-]|$)", re.IGNORECASE
)
_STYLE_W_RE = re.compile(r"(?:^|[;\s])width\s*:\s*(\d+)px", re.IGNORECASE)
_STYLE_H_RE = re.compile(r"(?:^|[;\s])height\s*:\s*(\d+)px", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_OG_TITLE_TAG_RE = re.compile(
    r"<meta\b[^>]*?\bproperty\s*=\s*['\"]og:title['\"][^>]*>", re.IGNORECASE
)

# 懒加载图片的真实地址属性，按优先级取第一个非空值。
_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-lazyload")


def _tag_attr(tag: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*=\s*['\"]([^'\"]*)['\"]", tag, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _declared_size(tag: str) -> tuple[int, int] | None:
    """(width, height) from width/height attributes or inline style px values."""
    digits = re.compile(r"\d+")

    def _int_of(raw: str) -> int | None:
        match = digits.search(str(raw or ""))
        return int(match.group()) if match else None

    width = _int_of(_tag_attr(tag, "width"))
    height = _int_of(_tag_attr(tag, "height"))
    style = _tag_attr(tag, "style")
    if width is None:
        match = _STYLE_W_RE.search(style)
        width = int(match.group(1)) if match else None
    if height is None:
        match = _STYLE_H_RE.search(style)
        height = int(match.group(1)) if match else None
    if width is None or height is None:
        return None
    return width, height


def _img_tag_is_noise(tag: str, src: str) -> bool:
    """图标/站标/头像/小尺寸装饰图的启发式判断（第一级规则粗筛）。"""
    if _NOISE_EXT_RE.search(src):
        return True
    if _NOISE_URL_TOKEN_RE.search(src) or _NOISE_URL_SUBSTR_RE.search(src):
        return True
    attrs = " ".join(
        (_tag_attr(tag, "class"), _tag_attr(tag, "id"), _tag_attr(tag, "alt"))
    )
    if _NOISE_ATTR_SUBSTR_RE.search(attrs) or _NOISE_ATTR_TOKEN_RE.search(attrs):
        return True
    size = _declared_size(tag)
    return bool(size is not None and max(size) < 64)


def _meta_ref_is_noise(url: str) -> bool:
    """og:image 白名单待遇：只拦头像特征与图标格式，不做全量黑名单。"""
    lowered = str(url or "").lower()
    if _NOISE_EXT_RE.search(lowered):
        return True
    return bool(_NOISE_URL_SUBSTR_RE.search(lowered))


def extract_media_urls_from_html(
    html: str, base_url: str, limit: int = 10, filter_noise: bool = True
) -> list[str]:
    """Pull image candidates out of page HTML.

    Priority: og/twitter meta covers, then <video poster>, then <img> tags
    (lazy-load data-* sources included). With filter_noise, icons, logos,
    avatars, tiny declared sizes and tracking images are dropped; meta covers
    only get avatar/icon-format checks so real content covers stay alive.
    Relative URLs resolve against base_url; only public http(s) addresses
    pass. Order preserved and deduped.
    """
    text = str(html or "")
    base = str(base_url or "")
    candidates: list[str] = []

    def _push(raw: str, *, meta: bool = False) -> None:
        if len(candidates) >= limit:
            return
        url = urljoin(base, str(raw or "").strip())
        if not url.lower().startswith(("http://", "https://")):
            return
        if not AvatarService.is_safe_http_url(url):
            return
        if filter_noise and meta and _meta_ref_is_noise(url):
            return
        if url not in candidates:
            candidates.append(url)

    for tag in _META_IMAGE_TAG_RE.finditer(text):
        content = _META_CONTENT_RE.search(tag.group(0))
        if content:
            _push(content.group(1), meta=True)
    for tag in _VIDEO_TAG_RE.finditer(text):
        poster = _tag_attr(tag.group(0), "poster")
        if poster:
            _push(poster)
    for tag in _IMG_TAG_RE.finditer(text):
        tag_str = tag.group(0)
        src = ""
        for attr in _SRC_ATTRS:
            value = _tag_attr(tag_str, attr)
            # data: 占位（懒加载占位图/内联图）不算真实地址，继续看 data-* 属性。
            if value and not value.lower().startswith("data:"):
                src = value
                break
        if not src:
            continue
        if filter_noise and _img_tag_is_noise(tag_str, src):
            continue
        _push(src)
    return candidates


def extract_page_title(html: str) -> str:
    """og:title / <title> text as LLM review context; empty when absent."""
    text = str(html or "")
    match = _OG_TITLE_TAG_RE.search(text)
    if match:
        content = _META_CONTENT_RE.search(match.group(0))
        if content:
            return content.group(1).strip()[:120]
    match = _TITLE_RE.search(text)
    if match:
        return match.group(1).strip()[:120]
    return ""


def parse_kept_indexes(text: str, count: int) -> list[int] | None:
    """Parse the review model's JSON index list.

    Returns None on any doubt (caller then fails open and sends everything):
    non-JSON output, non-integer items, or out-of-range indexes. An explicit
    empty list is valid and means "everything is noise".
    """
    raw = str(text or "").strip()
    if not raw or count <= 0:
        return None
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("```")
        ).strip()
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[[^\[\]]*\]", raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if not 0 <= item < count:
            return None
    return sorted(set(data))


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

        prepared, failed = await self.prepare_media(candidates)
        report.fallback_urls.extend(failed)
        try:
            sent_report = await self.send_prepared(event, prepared)
        except MediaSkipError:
            # 消息组件不可用：按整批失败处理，URL 进回退列表而不是静默丢弃。
            self._log("warning", "message components unavailable; media not sent")
            report.fallback_urls.extend(url for url, _, _ in prepared)
            return report
        report.sent_images = sent_report.sent_images
        report.sent_videos = sent_report.sent_videos
        report.fallback_urls.extend(sent_report.fallback_urls)
        return report

    async def prepare_media(
        self, urls: list[str]
    ) -> tuple[list[_PreparedMedia], list[str]]:
        """Download candidates into base64 payloads without sending anything."""
        prepared: list[_PreparedMedia] = []
        failed: list[str] = []
        for url in urls:
            try:
                kind, payload = await self._prepare(url)
            except MediaSkipError as exc:
                self._log("debug", "media skipped %s: %s", url, exc)
                failed.append(url)
                continue
            prepared.append((url, kind, payload))
        return prepared, failed

    async def send_prepared(
        self, event: Any, prepared: list[_PreparedMedia]
    ) -> MediaDeliveryReport:
        """Send prepared media as base64 messages; images batched, videos one by one."""
        try:
            import astrbot.api.message_components as Comp
            from astrbot.core.message.message_event_result import MessageChain
        except ImportError as exc:  # pragma: no cover - only outside AstrBot
            raise MediaSkipError("无法导入 AstrBot 消息组件。") from exc

        report = MediaDeliveryReport()
        images = [(url, payload) for url, kind, payload in prepared if kind == "image"]
        for url, kind, payload in prepared:
            if kind != "video":
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
        self,
        event: Any,
        urls: list[str],
        *,
        page_extract: bool = True,
        filter_noise: bool = True,
        review: Any = None,
        intent: str = "",
    ) -> str:
        """Download and send media from direct links or page links.

        Direct media links go straight to prepare; page links are fetched and
        their og:image / poster / <img> candidates extracted first (rule-level
        noise filtering applied here). ``review`` is the optional second
        cleaning stage: an async callable ``(prepared, intent, page_title) ->
        list[int] | None`` returning kept indexes, or None when review is
        unavailable (fail-open, everything is sent). Extension-less links fall
        back to the content-type check in _download. Returns an LLM-facing
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

        page_title = ""
        for url in page_urls:
            try:
                html = await self.fetch_page_html(url)
            except MediaSkipError as exc:
                self._log("debug", "page fetch skipped %s: %s", url, exc)
                # Not a page (or unreachable): let _download classify by type.
                candidates.append(url)
                continue
            if not page_title:
                page_title = extract_page_title(html)
            found = extract_media_urls_from_html(
                html, url, limit=cap, filter_noise=filter_noise
            )
            if found:
                candidates.extend(found)
            else:
                candidates.append(url)

        if not candidates:
            return "没有可处理的链接：所有 URL 都为空或未通过安全校验。"

        prepared, failed = await self.prepare_media(candidates[:cap])
        dropped_by_review = 0
        if review and prepared:
            kept: list[int] | None = None
            try:
                kept = await review(prepared, intent, page_title)
            except Exception:
                self._log("warning", "media review callable failed; fail-open")
            if kept is not None:
                valid = sorted(
                    {
                        index
                        for index in kept
                        if isinstance(index, int) and 0 <= index < len(prepared)
                    }
                )
                dropped_by_review = len(prepared) - len(valid)
                prepared = [prepared[index] for index in valid]

        report = await self.send_prepared(event, prepared)
        report.fallback_urls.extend(failed)
        if report.sent_images or report.sent_videos:
            summary = report.summary()
            if dropped_by_review:
                summary += f"\n（AI 审阅剔除了 {dropped_by_review} 张干扰图。）"
            return summary
        if dropped_by_review and not prepared:
            message = (
                f"AI 审阅判定全部 {dropped_by_review} 张候选都是干扰图"
                "（图标/头像等），没有找到符合要求的内容图片。"
            )
            if report.fallback_urls:
                message += "\n可以把下面的原始链接发给用户自行查看：\n" + "\n".join(
                    f"- {url}" for url in report.fallback_urls
                )
            return message
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
