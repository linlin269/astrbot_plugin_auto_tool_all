from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from astrbot_plugin_auto_tool_all.avatar_service import AvatarError, AvatarService


@dataclass
class MessageObject:
    self_id: str
    message: list


@dataclass
class Event:
    message_obj: MessageObject

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return "87654321"


def test_resolve_bot_uses_local_cache(tmp_path, monkeypatch):
    async def scenario():
        service = AvatarService(tmp_path, {"avatar_spec": 640})
        cached = service.avatar_dir / "cached.jpg"
        cached.write_bytes(b"avatar")

        async def fake_download(url, *, namespace, key):
            assert namespace == "avatar"
            return cached

        monkeypatch.setattr(service, "get_or_download", fake_download)
        target = await service.resolve_for_event(
            Event(MessageObject("12345678", [])), "bot"
        )

        assert target.qq == "12345678"
        assert "12345678" in target.url
        assert target.path == cached

    asyncio.run(scenario())


def test_sender_identity_uses_sender_id(tmp_path, monkeypatch):
    async def scenario():
        service = AvatarService(tmp_path)

        async def fake_download(url, *, namespace, key):
            return tmp_path / "sender.jpg"

        monkeypatch.setattr(service, "get_or_download", fake_download)
        target = await service.resolve_for_event(
            Event(MessageObject("12345678", [])), "sender"
        )

        assert target.qq == "87654321"

    asyncio.run(scenario())


def test_at_identity_requires_an_at(tmp_path):
    async def scenario():
        service = AvatarService(tmp_path)
        with pytest.raises(AvatarError):
            await service.resolve_for_event(Event(MessageObject("12345678", [])), "at")

    asyncio.run(scenario())
