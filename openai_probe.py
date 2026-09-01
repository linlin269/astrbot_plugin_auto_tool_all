"""Probe OpenAI-compatible endpoints: list models and time real replies.

对 /models 列出的每个模型发一条流式 "hi"，记录模型名、首字时间与回复总时间。
只有收到真实内容回复的模型才算可用。key 只在内存中传递，绝不写日志或落盘。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

_TEST_PROMPT = "hi"
_MAX_MODELS = 200
# /v1、/v3/api、/api/v1、/v1beta 这类“url 已自带版本段”的结尾。
_VERSION_TAIL_RE = re.compile(r"/(?:api/)?v\d+[a-z]*(?:/api)?$", re.IGNORECASE)


class ProbeError(RuntimeError):
    """Raised for user-correctable probe failures."""


@dataclass(frozen=True)
class ModelTiming:
    model: str
    ok: bool
    first_token_seconds: float | None = None
    total_seconds: float | None = None
    error: str = ""
    streamed: bool = True

    def line(self) -> str:
        if not self.ok:
            reason = self.error or "无回复"
            return f"✗ {self.model} - 不可用（{reason}）"
        first = (
            f"{self.first_token_seconds:.1f}s"
            if self.first_token_seconds is not None
            else "N/A"
        )
        note = "" if self.streamed else "（非流式，首字=总时间）"
        return f"✓ {self.model} - 首字 {first} - 总回复 {self.total_seconds:.1f}s{note}"


@dataclass
class ProbeResult:
    base_url: str
    results: list[ModelTiming] = field(default_factory=list)

    @property
    def available(self) -> list[ModelTiming]:
        return sorted(
            (item for item in self.results if item.ok),
            key=lambda item: (item.total_seconds or 0.0),
        )

    @property
    def unavailable(self) -> list[ModelTiming]:
        return [item for item in self.results if not item.ok]

    def summary(self, *, include_unavailable: bool = True) -> str:
        good = self.available
        lines = ["可用模型有："] if good else ["没有可用模型。"]
        lines.extend(item.line() for item in good)
        bad = self.unavailable
        if include_unavailable and bad:
            lines.append("")
            lines.append(f"不可用 {len(bad)} 个：")
            lines.extend(item.line() for item in bad)
        return "\n".join(lines)


def normalize_base_url(raw: str, api_prefix: str = "") -> str:
    """Compose the API base from user input and an optional explicit prefix.

    规则（与需求确认一致）：url 已以版本段结尾则直接用；否则默认补 /v1；
    用户显式给了前缀（如 v3/api）则覆盖到该前缀上。api_prefix 形如 "v1"、"v3/api"。
    """
    base = str(raw or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ProbeError("url 必须以 http:// 或 https:// 开头。")

    prefix = str(api_prefix or "").strip().strip("/").lower()
    if prefix:
        if not all(part and part.isalnum() for part in prefix.split("/")):
            raise ProbeError(f"接口前缀 {api_prefix!r} 不合法，示例：v1、v3/api。")
        base = _VERSION_TAIL_RE.sub("", base)
        if base.lower().endswith("/" + prefix):
            return base
        return f"{base}/{prefix}"

    if _VERSION_TAIL_RE.search(base):
        return base
    return f"{base}/v1"


def _redact(message: str, key: str) -> str:
    """Never leak the key into error text that might reach logs or the user."""
    if not key:
        return message
    while key and key in message:
        message = message.replace(key, "***")
    return message


# --------------------------------------------------------------------------
# 消息解析：从自然语言消息里提取 url / key / 显式接口前缀，并识别触发语。
# --------------------------------------------------------------------------

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)】\]]+", re.IGNORECASE)
_MEDIA_EXT_RE = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|mov|mkv|webm|avi|m4v|flv)(?:[?#]|$)",
    re.IGNORECASE,
)
_QLOGO_RE = re.compile(r"qlogo\.cn|headimg_dl", re.IGNORECASE)

_KEY_LABELED_RE = re.compile(
    r"(?:apikey|api_key|key|密钥|口令|令牌|token)\s*[:：=]\s*[\"']?"
    r"([A-Za-z0-9._~+/=\-]{12,})",
    re.IGNORECASE,
)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")
_BEARER_RE = re.compile(r"\bBearer\s+([A-Za-z0-9._\-]{12,})")
_BARE_TOKEN_RE = re.compile(r"[A-Za-z0-9._~\-]{16,}")

_PREFIX_AFTER_VERB_RE = re.compile(
    r"(?:用|使用|走|通过|以)\s*(v\d+(?:\s*/\s*[A-Za-z0-9_\-]+)?)", re.IGNORECASE
)
_PREFIX_SLASHED_RE = re.compile(r"\bv\d+\s*/\s*[A-Za-z0-9_\-]+", re.IGNORECASE)

_TEST_RE = re.compile(r"测试|测一下|测测|测一遍|测速|跑一下|试一下|帮我测")
_LIST_RE = re.compile(
    r"什么模型|哪些模型|有什么模型|模型列表|列出|列一下|看看|查看|看一下|有哪些|有多少"
)
_HOW_RE = re.compile(r"怎么|如何|怎样|什么意思|为什么")


def first_api_url(text: str) -> str:
    """第一个像 API 服务地址的 http(s) 链接（排除媒体文件与头像服务）。"""
    for raw in _HTTP_URL_RE.findall(str(text or "")):
        url = raw.rstrip(".,;、）)】>\"'")
        if not _MEDIA_EXT_RE.search(url) and not _QLOGO_RE.search(url):
            return url
    return ""


def extract_api_key(text: str) -> str:
    """识别 key：标注写法 > sk- 前缀 > Bearer > 纯 token 整句。"""
    value = str(text or "").strip()
    labeled = _KEY_LABELED_RE.findall(value)
    if labeled:
        return labeled[-1].strip("\"'")
    sk_prefixed = _SK_RE.findall(value)
    if sk_prefixed:
        return sk_prefixed[-1]
    bearer = _BEARER_RE.findall(value)
    if bearer:
        return bearer[-1]
    if _BARE_TOKEN_RE.fullmatch(value):
        return value
    return ""


def extract_explicit_prefix(text: str) -> str:
    """提取用户显式指定的接口前缀，如“用 v3/api 查看”→ v3/api。"""
    cleaned = _HTTP_URL_RE.sub(" ", str(text or ""))
    match = _PREFIX_AFTER_VERB_RE.search(cleaned)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = _PREFIX_SLASHED_RE.search(cleaned)
    if match:
        return re.sub(r"\s+", "", match.group(0))
    return ""


def detect_probe_mode(text: str) -> str:
    """返回 "test" / "list" / ""。两词同现时测试优先（测试包含列表信息）。"""
    value = str(text or "")
    if "模型" not in value:
        return ""
    if _HOW_RE.search(value) and not re.search(r"里面|全部|所有", value):
        return ""
    if _TEST_RE.search(value):
        return "test"
    if _LIST_RE.search(value):
        return "list"
    return ""


class OpenAIProbeService:
    """Small aiohttp client for /models and streamed chat completions."""

    def __init__(self, config: Any | None = None, logger: Any | None = None) -> None:
        self.config = config or {}
        self.logger = logger

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def concurrency(self) -> int:
        return self._int("probe_concurrency", 3, 1, 10)

    def timeout_seconds(self) -> int:
        return self._int("probe_timeout_seconds", 10, 5, 120)

    async def list_models(
        self, base_url: str, key: str, api_prefix: str = ""
    ) -> list[str]:
        import aiohttp

        url = f"{normalize_base_url(base_url, api_prefix)}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session, session.get(
                url, headers=headers
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise ProbeError(
                        _redact(f"models 接口返回 HTTP {response.status}：{body}", key)
                    )
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise ProbeError("models 接口请求超时。") from exc
        except ProbeError:
            raise
        except Exception as exc:
            raise ProbeError(_redact(f"models 接口请求失败：{exc}", key)) from exc

        return self._extract_model_ids(payload)

    @staticmethod
    def _extract_model_ids(payload: Any) -> list[str]:
        models: list[str] = []
        if isinstance(payload, dict):
            data = payload.get("data", payload.get("models", []))
        elif isinstance(payload, list):
            data = payload
        else:
            data = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("model") or item.get("name")
            else:
                model_id = item
            text = str(model_id or "").strip()
            if text and text not in models:
                models.append(text)
        if not models:
            raise ProbeError("models 接口没有返回任何模型。")
        return models[:_MAX_MODELS]

    async def probe_models(
        self, base_url: str, key: str, models: list[str], *, api_prefix: str = ""
    ) -> ProbeResult:
        """Test every model with one streamed "hi", bounded by a semaphore."""
        base = normalize_base_url(base_url, api_prefix)
        result = ProbeResult(base_url=base)
        semaphore = asyncio.Semaphore(self.concurrency())
        timeout = self.timeout_seconds()

        async def runner(model: str) -> ModelTiming:
            async with semaphore:
                return await self._time_one_model(base, key, model, timeout)

        timings = await asyncio.gather(*(runner(model) for model in models))
        result.results = list(timings)
        return result

    async def _time_one_model(
        self, base: str, key: str, model: str, timeout: int
    ) -> ModelTiming:
        timing = await self._streamed_probe(base, key, model, timeout)
        # 个别中转站不支持 stream 参数，返回 400 时按非流式重试一次。
        if (
            not timing.ok
            and timing.error.startswith("HTTP 400")
            and "stream" in timing.error.lower()
        ):
            return await self._plain_probe(base, key, model, timeout)
        return timing

    async def _streamed_probe(
        self, base: str, key: str, model: str, timeout: int
    ) -> ModelTiming:
        import aiohttp

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": _TEST_PROMPT}],
            "stream": True,
            "max_tokens": 32,
        }
        started = time.monotonic()
        first_at: float | None = None
        received = ""
        try:
            request_timeout = aiohttp.ClientTimeout(total=timeout, connect=5)
            async with aiohttp.ClientSession(
                timeout=request_timeout, trust_env=False
            ) as session, session.post(url, headers=headers, json=body) as response:
                if response.status != 200:
                    text = (await response.text())[:160]
                    return ModelTiming(
                        model=model,
                        ok=False,
                        error=_redact(f"HTTP {response.status} {text}".strip(), key),
                    )
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" not in content_type:
                    try:
                        payload = await response.json(content_type=None)
                    except (TypeError, ValueError):
                        payload = {}
                    piece = self._extract_message_text(payload)
                    if not piece.strip():
                        return ModelTiming(model=model, ok=False, error="空回复", streamed=False)
                    total = time.monotonic() - started
                    return ModelTiming(
                        model=model,
                        ok=True,
                        first_token_seconds=total,
                        total_seconds=total,
                        streamed=False,
                    )
                async for raw_line in response.content:
                    line = raw_line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data_text = line[len(b"data:") :].strip()
                    if data_text == b"[DONE]":
                        break
                    try:
                        chunk = json.loads(data_text)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    piece = self._extract_delta_text(chunk)
                    if piece:
                        if first_at is None:
                            first_at = time.monotonic()
                        received += piece
        except asyncio.TimeoutError:
            if first_at is not None:
                # 超时前已经流出真实内容，按“收到真实回复”计。
                return ModelTiming(
                    model=model,
                    ok=True,
                    first_token_seconds=first_at - started,
                    total_seconds=time.monotonic() - started,
                )
            return ModelTiming(model=model, ok=False, error=f"超时（{timeout}s）")
        except Exception as exc:
            return ModelTiming(model=model, ok=False, error=_redact(str(exc)[:160], key))

        total = time.monotonic() - started
        if not received.strip():
            return ModelTiming(model=model, ok=False, error="空回复")
        return ModelTiming(
            model=model,
            ok=True,
            first_token_seconds=(first_at - started) if first_at is not None else total,
            total_seconds=total,
            streamed=first_at is not None,
        )

    async def _plain_probe(
        self, base: str, key: str, model: str, timeout: int
    ) -> ModelTiming:
        """Non-streaming fallback; the whole reply counts as both timings."""
        import aiohttp

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": _TEST_PROMPT}],
            "stream": False,
            "max_tokens": 32,
        }
        started = time.monotonic()
        try:
            request_timeout = aiohttp.ClientTimeout(total=timeout, connect=5)
            async with aiohttp.ClientSession(
                timeout=request_timeout, trust_env=False
            ) as session, session.post(url, headers=headers, json=body) as response:
                if response.status != 200:
                    text = (await response.text())[:160]
                    return ModelTiming(
                        model=model,
                        ok=False,
                        error=_redact(f"HTTP {response.status} {text}".strip(), key),
                    )
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError:
            return ModelTiming(model=model, ok=False, error=f"超时（{timeout}s）")
        except Exception as exc:
            return ModelTiming(model=model, ok=False, error=_redact(str(exc)[:160], key))

        total = time.monotonic() - started
        content = ""
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    content = message["content"]
        if not content.strip():
            return ModelTiming(model=model, ok=False, error="空回复", streamed=False)
        return ModelTiming(
            model=model,
            ok=True,
            first_token_seconds=total,
            total_seconds=total,
            streamed=False,
        )

    @staticmethod
    def _extract_delta_text(chunk: dict) -> str:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            return delta["content"]
        text = first.get("text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _extract_message_text(payload: dict) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        text = first.get("text")
        return text if isinstance(text, str) else ""
