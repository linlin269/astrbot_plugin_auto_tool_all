"""Extract image and @-mention information from AstrBot/OneBot events."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class EventImage:
    """An image source found in a current or quoted message."""

    source: str
    role: str = "message"


def _type_name(value: Any) -> str:
    return type(value).__name__ if value is not None else ""


def _unique(values: Iterator[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _source_values(value: Any) -> list[str]:
    """Return useful image source fields without interpreting OneBot file IDs."""
    if isinstance(value, Mapping):
        data = value.get("data") if isinstance(value.get("data"), Mapping) else value
        return _unique(
            iter(
                str(data.get(key) or "").strip()
                for key in ("path", "file_path", "file", "url", "src")
                if data.get(key)
            )
        )
    return _unique(
        iter(
            str(getattr(value, key, "") or "").strip()
            for key in ("path", "file_path", "file", "url", "src")
            if getattr(value, key, None)
        )
    )


def _usable_source(source: str) -> bool:
    lower = source.lower()
    # OneBot may provide only a protocol file id (for example, "abc.image").
    # The adapter-specific get_image resolution happens in the image consumer.
    return bool(source) and not lower.startswith(("javascript:", "vbscript:"))


def _walk(
    value: Any, role: str, visited: set[int], depth: int = 0
) -> Iterator[EventImage]:
    if value is None or depth > 16 or id(value) in visited:
        return
    visited.add(id(value))

    if isinstance(value, Mapping):
        kind = str(value.get("type") or "").lower()
        if kind == "image":
            for source in _source_values(value):
                if _usable_source(source):
                    yield EventImage(source, role)
            return
        if kind in {"at", "mention"}:
            return
        for key, child in value.items():
            child_role = (
                "quote" if str(key).lower() in {"reply", "quote", "source"} else role
            )
            yield from _walk(child, child_role, visited, depth + 1)
        return

    name = _type_name(value)
    if name == "Image":
        for source in _source_values(value):
            if _usable_source(source):
                yield EventImage(source, role)
        return

    # AstrBot's aiocqhttp adapter stores quoted content in Reply.chain.
    if name in {"Reply", "Quote"} or any(
        token in name.lower() for token in ("reply", "quote")
    ):
        chain = getattr(value, "chain", None)
        if chain is not None:
            yield from _walk(chain, "quote", visited, depth + 1)
        nested = getattr(value, "message", None)
        if nested is not None:
            yield from _walk(nested, "quote", visited, depth + 1)
        return

    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk(child, role, visited, depth + 1)
        return

    # Restrict object traversal to message-bearing fields. Walking a whole event
    # object would reach bot/context internals and can be very expensive.
    for attr, child_role in (
        ("message", role),
        ("chain", role),
        ("quote", "quote"),
        ("raw_message", role),
    ):
        child = getattr(value, attr, None)
        if child is not None and child is not value:
            yield from _walk(child, child_role, visited, depth + 1)


def extract_image_sources(event: Any, max_count: int | None = None) -> list[EventImage]:
    """Extract direct and quoted image sources from an event."""
    roots = [
        (getattr(event, "message_obj", None), "message"),
        (getattr(event, "message", None), "message"),
        (getattr(event, "raw_message", None), "message"),
    ]
    results: list[EventImage] = []
    seen: set[tuple[str, str]] = set()
    for root, role in roots:
        for item in _walk(root, role, set()):
            key = (item.role, item.source)
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if max_count is not None and len(results) >= max_count:
                return results
    return results


def has_image(event: Any) -> bool:
    return bool(extract_image_sources(event, max_count=1))


def extract_at_ids(event: Any) -> list[str]:
    """Extract QQ IDs from At components/raw OneBot mentions."""
    results: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in {"all", "0"} or text in seen:
            return
        seen.add(text)
        results.append(text)

    def walk(value: Any, visited: set[int], depth: int = 0) -> None:
        if value is None or depth > 16 or id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, Mapping):
            kind = str(value.get("type") or "").lower()
            data = (
                value.get("data") if isinstance(value.get("data"), Mapping) else value
            )
            if kind in {"at", "mention"}:
                add(data.get("qq") or data.get("user_id") or data.get("id"))
                return
            for child in value.values():
                walk(child, visited, depth + 1)
            return
        name = _type_name(value)
        if name in {"At", "AtSomeone"}:
            add(getattr(value, "qq", None) or getattr(value, "id", None))
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                walk(child, visited, depth + 1)
            return
        for attr in ("message", "chain", "quote", "raw_message"):
            child = getattr(value, attr, None)
            if child is not None and child is not value:
                walk(child, visited, depth + 1)

    for root in (
        getattr(event, "message_obj", None),
        getattr(event, "message", None),
        getattr(event, "raw_message", None),
    ):
        walk(root, set())
    return results


def is_http_image_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except (TypeError, ValueError):
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
