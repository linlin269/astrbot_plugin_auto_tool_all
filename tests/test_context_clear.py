from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from astrbot_plugin_auto_tool_all.context_clear import (
    ContextClearError,
    ContextClearService,
    ScheduledClear,
    format_duration,
    parse_duration,
    session_label,
    split_session,
)


class FakeConversationManager:
    def __init__(self):
        self.deleted: list[str] = []
        self.conversations = [
            SimpleNamespace(user_id="aiocqhttp:GroupMessage:111"),
            SimpleNamespace(user_id="aiocqhttp:FriendMessage:222"),
            SimpleNamespace(user_id="aiocqhttp:GroupMessage:111"),
        ]

    async def delete_conversations_by_user_id(self, unified_msg_origin):
        self.deleted.append(unified_msg_origin)

    async def get_conversations(self, unified_msg_origin=None, platform_id=None):
        return self.conversations


class FakeContext:
    def __init__(self, config=None, platforms=()):
        self.conversation_manager = FakeConversationManager()
        self._config = config if config is not None else {"admins_id": []}
        self.platform_manager = SimpleNamespace(
            get_insts=lambda: list(platforms),
        )

    def get_config(self):
        return self._config


def _platform(platform_id="qqbot", name="aiocqhttp", proactive=True):
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(
            id=platform_id, name=name, support_proactive_message=proactive
        )
    )


# ----------------------------------------------------------------------
# duration parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("6s", 6),
        ("6秒", 6),
        ("5min", 300),
        ("5 分钟", 300),
        ("1h", 3600),
        ("1小时", 3600),
        (" 10MIN ", 600),
    ],
)
def test_parse_duration_accepts_supported_units(text, seconds):
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "text",
    ["", "abc", "0s", "-5min", "6x", "5分钟3秒", "1.5h", "s", None],
)
def test_parse_duration_rejects_invalid_input(text):
    assert parse_duration(text) is None


def test_format_duration_human_units():
    assert format_duration(timedelta(seconds=6)) == "6 秒"
    assert format_duration(timedelta(minutes=5)) == "5 分钟"
    assert format_duration(timedelta(hours=1, minutes=2, seconds=3)) == (
        "1 小时 2 分钟 3 秒"
    )


# ----------------------------------------------------------------------
# session helpers
# ----------------------------------------------------------------------


def test_split_session():
    assert split_session("aiocqhttp:GroupMessage:123") == (
        "aiocqhttp",
        "GroupMessage",
        "123",
    )


def test_session_label_group_and_private():
    assert session_label("aiocqhttp:GroupMessage:123") == "群 123"
    assert session_label("aiocqhttp:FriendMessage:456") == "私聊 456"
    assert session_label("webchat:OtherMessage:x") == "OtherMessage x"


# ----------------------------------------------------------------------
# immediate clearing
# ----------------------------------------------------------------------


def test_clear_session_deletes_through_conversation_manager():
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context)

        await service.clear_session("aiocqhttp:GroupMessage:111")

        assert context.conversation_manager.deleted == ["aiocqhttp:GroupMessage:111"]

    asyncio.run(scenario())


def test_clear_all_sessions_deduplicates():
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context)

        count = await service.clear_all_sessions()

        assert count == 2
        assert sorted(context.conversation_manager.deleted) == [
            "aiocqhttp:FriendMessage:222",
            "aiocqhttp:GroupMessage:111",
        ]

    asyncio.run(scenario())


def test_clear_fails_without_conversation_manager():
    async def scenario():
        service = ContextClearService(SimpleNamespace())
        with pytest.raises(ContextClearError):
            await service.clear_session("aiocqhttp:GroupMessage:111")

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# scheduled clearing
# ----------------------------------------------------------------------


def test_schedule_clear_fires_once_and_notifies():
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context, chain_factory=lambda text: text)
        sent: list[tuple[str, str]] = []

        async def fake_send(umo, _chain):
            sent.append((umo, _chain))
            return True

        service.send_to_session = fake_send  # type: ignore[method-assign]

        assert (
            service.schedule_clear("aiocqhttp:GroupMessage:1", timedelta(seconds=0))
            is False
        )
        await asyncio.sleep(0.05)

        assert context.conversation_manager.deleted == ["aiocqhttp:GroupMessage:1"]
        assert sent == [("aiocqhttp:GroupMessage:1", "上下文已定时清空。")]
        assert service.sorted_entries() == []

    asyncio.run(scenario())


def test_schedule_clear_overrides_previous_task():
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context)

        service.schedule_clear("umo", timedelta(hours=1))
        first = service.sorted_entries()[0]
        replaced = service.schedule_clear("umo", timedelta(minutes=5))

        assert replaced is True
        assert len(service.sorted_entries()) == 1
        assert context.conversation_manager.deleted == []
        await asyncio.sleep(0)  # let the cancellation take effect
        # The first task must have been cancelled, not left running.
        assert first.handle is not None and first.handle.cancelled()
        await asyncio.sleep(0.05)  # the replacement must not fire early either

    asyncio.run(scenario())


def test_cancel_by_index_session_and_all():
    async def scenario():
        service = ContextClearService(FakeContext())
        service.schedule_clear("aiocqhttp:GroupMessage:111", timedelta(hours=1))
        service.schedule_clear("aiocqhttp:FriendMessage:222", timedelta(minutes=5))

        entries = service.sorted_entries()
        assert [session_label(entry.umo) for entry in entries] == [
            "私聊 222",
            "群 111",
        ]

        removed = service.cancel_by_index(1)
        assert removed is not None and removed.umo == "aiocqhttp:FriendMessage:222"

        removed_by_id = service.cancel_for_session_id("111")
        assert [entry.umo for entry in removed_by_id] == ["aiocqhttp:GroupMessage:111"]

        service.schedule_clear("aiocqhttp:GroupMessage:333", timedelta(hours=2))
        assert len(service.cancel_all()) == 1
        assert service.sorted_entries() == []

    asyncio.run(scenario())


def test_shutdown_cancels_pending_tasks():
    async def scenario():
        service = ContextClearService(FakeContext())
        service.schedule_clear("umo", timedelta(hours=1))
        entry = service.sorted_entries()[0]

        service.shutdown()
        await asyncio.sleep(0)  # let the cancellation take effect

        assert entry.handle is not None and entry.handle.cancelled()
        assert service.sorted_entries() == []

    asyncio.run(scenario())


def test_scheduled_run_survives_clear_failure(monkeypatch):
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context)

        async def failing_clear(umo):
            raise RuntimeError("boom")

        monkeypatch.setattr(service, "clear_session", failing_clear)
        service.schedule_clear("umo", timedelta(seconds=0))
        await asyncio.sleep(0.05)

        # The error was swallowed by the background task, nothing propagates.
        assert service.sorted_entries() == []

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# proactive messages
# ----------------------------------------------------------------------


def test_send_to_session_returns_platform_result():
    async def scenario():
        context = FakeContext()
        service = ContextClearService(context, chain_factory=lambda text: text)
        calls: list[tuple[str, str]] = []

        async def fake_send(umo, chain):
            calls.append((umo, chain))
            return True

        context.send_message = fake_send  # type: ignore[attr-defined]

        assert await service.send_to_session("umo", "hi") is True
        assert calls == [("umo", "hi")]

    asyncio.run(scenario())


def test_send_to_session_treats_none_platform_as_failure():
    async def scenario():
        context = FakeContext()

        async def fake_send(umo, chain):
            return False

        context.send_message = fake_send  # type: ignore[attr-defined]
        service = ContextClearService(context, chain_factory=lambda text: text)

        assert await service.send_to_session("missing", "hi") is False

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# restart notification
# ----------------------------------------------------------------------


def test_restart_targets_for_aiocqhttp_admins():
    context = FakeContext(
        config={"admins_id": ["10001", "10002"]},
        platforms=[
            _platform(platform_id="qq"),
            _platform(platform_id="w", name="webchat"),
            _platform(platform_id="n", name="discord", proactive=False),
        ],
    )
    service = ContextClearService(context)

    assert service.admin_restart_targets() == [
        "qq:FriendMessage:10001",
        "qq:FriendMessage:10002",
    ]


def test_restart_targets_without_admins_or_platform():
    empty = ContextClearService(FakeContext(config={"admins_id": []}))
    assert empty.admin_restart_targets() == []

    no_platform = ContextClearService(
        FakeContext(config={"admins_id": ["1"]}, platforms=[])
    )
    assert no_platform.admin_restart_targets() == []


def test_notify_restart_counts_delivered_messages():
    async def scenario():
        context = FakeContext(
            config={"admins_id": ["10001", "10002"]},
            platforms=[_platform(platform_id="qq")],
        )
        service = ContextClearService(context)
        sent: list[str] = []

        async def fake_send(umo, chain):
            sent.append(umo)
            return True

        service.send_to_session = fake_send  # type: ignore[method-assign]

        notified = await service.notify_restart_to_admins(attempts=1)

        assert notified == 2
        assert sent == ["qq:FriendMessage:10001", "qq:FriendMessage:10002"]

    asyncio.run(scenario())


def test_notify_restart_retries_then_fails_quietly():
    async def scenario():
        context = FakeContext(
            config={"admins_id": ["10001"]},
            platforms=[_platform(platform_id="qq")],
        )
        service = ContextClearService(context)
        attempts: list[str] = []

        async def failing_send(umo, chain):
            attempts.append(umo)
            raise RuntimeError("not connected")

        service.send_to_session = failing_send  # type: ignore[method-assign]

        notified = await service.notify_restart_to_admins(attempts=2, wait_seconds=0)

        assert notified == 0
        assert len(attempts) == 2

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# dataclass sanity
# ----------------------------------------------------------------------


def test_scheduled_clear_defaults():
    entry = ScheduledClear(umo="umo", fire_at=datetime.now())
    assert entry.handle is None
