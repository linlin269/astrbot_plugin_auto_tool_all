"""AnySearch client helpers used by the built-in search tools."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp


class SearchError(RuntimeError):
    """Raised when AnySearch cannot return a usable response."""


CONTENT_TYPES = {
    "web",
    "news",
    "code",
    "doc",
    "academic",
    "data",
    "image",
    "video",
    "audio",
}
FRESHNESS_VALUES = {"day", "week", "month", "year"}


def split_values(value: Any) -> list[str]:
    """Accept a list, a JSON list, or comma-separated text."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def clamp_results(value: Any, default: int = 5, maximum: int = 20) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def safe_public_url(url: str) -> bool:
    """Reject non-web and literal private/loopback URLs before extraction."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def normalize_batch_queries(value: Any, max_items: int = 5) -> list[dict[str, Any]]:
    """Normalize AnySearch's JSON-list and comma-separated batch syntax."""
    if isinstance(value, (list, tuple)):
        parsed: Any = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [item for item in text.split(",") if item.strip()]
    if not isinstance(parsed, list):
        parsed = [parsed]

    output: list[dict[str, Any]] = []
    for item in parsed[:max_items]:
        if isinstance(item, dict):
            query = str(item.get("query", "")).strip()
            payload = dict(item)
        else:
            query = str(item or "").strip()
            payload = {}
        if query:
            payload["query"] = query
            output.append(payload)
    return output


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[外部内容已截断]"


def _safe_error_text(value: Any, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"\s+", " ", str(value or ""))).strip()
    return text[:limit]


class AnySearchClient:
    """Client for AnySearch's anonymous JSON-RPC MCP endpoint."""

    DEFAULT_ENDPOINT = "https://api.anysearch.com/mcp"
    MAX_RESPONSE_CHARS = 12000
    MAX_RESPONSE_BYTES = 512 * 1024

    def __init__(self, endpoint: str = "", api_key: str = "", timeout: int = 30) -> None:
        endpoint_value = str(endpoint or "").strip() or self.DEFAULT_ENDPOINT
        if not safe_public_url(endpoint_value):
            endpoint_value = self.DEFAULT_ENDPOINT
        self.endpoint = endpoint_value
        self.api_key = str(api_key or "").strip()
        try:
            parsed_timeout = int(timeout)
        except (TypeError, ValueError):
            parsed_timeout = 30
        self.timeout = max(5, min(parsed_timeout, 120))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _redact(self, value: Any) -> str:
        text = str(value or "")
        return text.replace(self.api_key, "***") if self.api_key else text

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session, session.post(
                self.endpoint, json=payload, headers=self._headers()
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > self.MAX_RESPONSE_BYTES:
                        raise SearchError("AnySearch 响应超过大小限制。")
                if response.status >= 400:
                    raise SearchError(f"AnySearch HTTP {response.status}。")
                charset = response.charset or "utf-8"
                text = bytes(body).decode(charset, errors="replace")
        except asyncio.TimeoutError as exc:
            raise SearchError("AnySearch 请求超时。") from exc
        except aiohttp.ClientError as exc:
            raise SearchError(f"AnySearch 连接失败：{type(exc).__name__}。") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SearchError("AnySearch 返回了无法解析的响应。") from exc
        if not isinstance(data, dict):
            raise SearchError("AnySearch 返回了无效的 JSON-RPC 响应。")
        if "error" in data:
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise SearchError(
                "AnySearch API 错误："
                f"{_safe_error_text(self._redact(message)) or '未知错误'}"
            )
        result = data.get("result", {})
        content = result.get("content", []) if isinstance(result, dict) else []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return _truncate_text(item.get("text"), self.MAX_RESPONSE_CHARS)
        return _truncate_text(
            json.dumps(result, ensure_ascii=False), self.MAX_RESPONSE_CHARS
        )

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        freshness: str = "",
        content_types: list[str] | None = None,
    ) -> str:
        arguments: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
        }
        if freshness in FRESHNESS_VALUES:
            arguments["freshness"] = freshness
        if content_types:
            arguments["content_types"] = content_types
        return await self._call("search", arguments)

    async def batch_search(self, queries: list[dict[str, Any]]) -> str:
        return await self._call("batch_search", {"queries": queries[:5]})

    async def extract(self, url: str) -> str:
        return await self._call("extract", {"url": url})

    async def vertical_search(
        self,
        query: str,
        domain: str,
        sub_domain: str = "",
        *,
        max_results: int = 5,
        freshness: str = "",
        content_types: list[str] | None = None,
    ) -> str:
        arguments: dict[str, Any] = {
            "query": query,
            "domain": domain,
            "max_results": max_results,
        }
        if sub_domain.strip():
            arguments["sub_domain"] = sub_domain.strip()
        if freshness in FRESHNESS_VALUES:
            arguments["freshness"] = freshness
        if content_types:
            arguments["content_types"] = content_types
        return await self._call("search", arguments)
