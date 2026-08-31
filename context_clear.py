"""Session context clearing with immediate, scheduled, and global scopes."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_GROUP_MESSAGE_TYPE = "groupmessage"
_FRIEND_MESSAGE_TYPE = "friendmessage"

_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(s|秒|min|分钟|h|小时)\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "秒": 1,
    "min": 60,
    "分钟": 60,
    "h": 3600,
    "小时": 3600,
}


class ContextClearError(RuntimeError):
    """Raised for user-correctable context clearing errors."""


def parse_duration(text: Any) -> timedelta | None:
    """Parse a duration like ``6s``/``6秒``/``5min``/``5分钟``/``1h``/``1小时``.

    Returns ``None`` when the text is not a positive duration in the supported
    units; the digits and the unit may be separated by whitespace and the
    latin units are case-insensitive.
    """
    if not isinstance(text, str):
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None
    unit = match.group(2).lower()
    seconds = value * _UNIT_SECONDS[unit]
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def format_duration(duration: timedelta) -> str:
    """Render a timedelta in human readable Chinese units."""
    total = max(0, int(duration.total_seconds()))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if seconds or not parts:
        parts.append(f"{seconds} 秒")
    return " ".join(parts)


def split_session(umo: str) -> tuple[str, str, str]:
    """Split ``platform_id:message_type:session_id`` into its three parts."""
    platform_id, _, rest = str(umo).partition(":")
    message_type, _, session_id = rest.partition(":")
    return platform_id, message_type, session_id


def session_label(umo: str) -> str:
    """Human readable session label such as ``群 123456`` or ``私聊 10001``."""
    _, message_type, session_id = split_session(umo)
    normalized = message_type.lower()
    if normalized == _GROUP_MESSAGE_TYPE:
        return f"群 {session_id}"
    if normalized == _FRIEND_MESSAGE_TYPE:
        return f"私聊 {session_id}"
    return f"{message_type or '会话'} {session_id}".strip()


@dataclass
class ScheduledClear:
    """A pending delayed clear bound to one session."""

    umo: str
    fire_at: datetime
    handle: asyncio.Task | None = None


class ContextClearService:
    """Delete LLM conversation data through the framework conversation manager.

    Scheduled clears are in-memory only: they live and die with the plugin
    process, which is exactly why the restart notification reports them as
    gone after every AstrBot start.
    """

    RESTART_NOTIFY_TEXT = "报告主人，我刚刚重启啦，清理上下文定时任务全部清空。"
    SCHEDULED_DONE_TEXT = "上下文已定时清空。"

    def __init__(
        self,
        context: Any,
        logger: Any | None = None,
        chain_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.context = context
        self.logger = logger
        self._chain_factory = chain_factory or self._default_message_chain
        self._scheduled: dict[str, ScheduledClear] = {}

    # ------------------------------------------------------------------
    # Immediate clearing
    # ------------------------------------------------------------------

    async def clear_session(self, umo: str) -> None:
        """Delete every stored conversation of one session."""
        manager = self._conversation_manager()
        deleter = getattr(manager, "delete_conversations_by_user_id", None)
        if not callable(deleter):
            raise ContextClearError("当前 AstrBot 版本不支持删除会话对话。")
        await deleter(umo)

    async def clear_all_sessions(self) -> int:
        """Delete conversations of every session; returns the session count."""
        manager = self._conversation_manager()
        getter = getattr(manager, "get_conversations", None)
        if not callable(getter):
            raise ContextClearError("当前 AstrBot 版本不支持列举会话对话。")
        conversations = await getter() or []
        umos: list[str] = []
        seen: set[str] = set()
        for conversation in conversations:
            umo = str(getattr(conversation, "user_id", "") or "")
            if umo and umo not in seen:
                seen.add(umo)
                umos.append(umo)
        for umo in umos:
            await self.clear_session(umo)
        return len(umos)

    def _conversation_manager(self) -> Any:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            raise ContextClearError("无法访问 AstrBot 会话管理器。")
        return manager

    # ------------------------------------------------------------------
    # Scheduled clearing
    # ------------------------------------------------------------------

    def schedule_clear(self, umo: str, delay: timedelta) -> bool:
        """Schedule a delayed clear; overwrites any pending task of the session.

        Returns ``True`` when an existing task for this session was replaced.
        """
        replaced = self.cancel_for_session(umo)
        entry = ScheduledClear(umo=umo, fire_at=datetime.now() + delay)
        entry.handle = asyncio.create_task(self._run_scheduled(entry))
        self._scheduled[umo] = entry
        return replaced

    async def _run_scheduled(self, entry: ScheduledClear) -> None:
        try:
            await asyncio.sleep(
                max(0.0, (entry.fire_at - datetime.now()).total_seconds())
            )
            if self._scheduled.get(entry.umo) is not entry:
                return
            self._scheduled.pop(entry.umo, None)
            await self.clear_session(entry.umo)
            await self.send_to_session(entry.umo, self.SCHEDULED_DONE_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(
                "error",
                "scheduled context clear failed for %s: %s",
                entry.umo,
                exc,
            )

    def sorted_entries(self) -> list[ScheduledClear]:
        """Pending tasks ordered by fire time, matching the reported indexes."""
        return sorted(self._scheduled.values(), key=lambda entry: entry.fire_at)

    def cancel_by_index(self, index: int) -> ScheduledClear | None:
        """Cancel the task shown at the 1-based index of the sorted list."""
        entries = self.sorted_entries()
        if 1 <= index <= len(entries):
            return self._remove(entries[index - 1])
        return None

    def cancel_for_session_id(self, session_id: str) -> list[ScheduledClear]:
        """Cancel every task whose qq/group session id matches."""
        matched = [
            entry
            for entry in self._scheduled.values()
            if split_session(entry.umo)[2] == session_id
        ]
        for entry in matched:
            self._remove(entry)
        return matched

    def cancel_for_session(self, umo: str) -> bool:
        """Cancel the pending task of one exact session, if any."""
        entry = self._scheduled.pop(umo, None)
        if entry is None:
            return False
        self._cancel_handle(entry)
        return True

    def cancel_all(self) -> list[ScheduledClear]:
        """Cancel every pending task."""
        removed = list(self._scheduled.values())
        self._scheduled.clear()
        for entry in removed:
            self._cancel_handle(entry)
        return removed

    def _remove(self, entry: ScheduledClear) -> ScheduledClear:
        if self._scheduled.get(entry.umo) is entry:
            self._scheduled.pop(entry.umo, None)
        self._cancel_handle(entry)
        return entry

    @staticmethod
    def _cancel_handle(entry: ScheduledClear) -> None:
        if entry.handle is not None and not entry.handle.done():
            entry.handle.cancel()

    def shutdown(self) -> None:
        """Cancel every pending task; called on plugin unload."""
        self.cancel_all()

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    async def send_to_session(self, umo: str, text: str) -> bool:
        """Push one proactive message to a session; returns delivery status."""
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            raise ContextClearError("当前环境不支持主动发送消息。")
        result = await sender(umo, self._chain_factory(text))
        return result is not False

    @staticmethod
    def _default_message_chain(text: str) -> Any:
        try:
            from astrbot.api.event import MessageChain
        except ImportError as exc:  # pragma: no cover - outside AstrBot
            raise ContextClearError("无法导入 AstrBot MessageChain。") from exc
        return MessageChain().message(text)

    # ------------------------------------------------------------------
    # Restart notification
    # ------------------------------------------------------------------

    def admin_restart_targets(self) -> list[str]:
        """Private-chat session ids for each admin on each aiocqhttp platform."""
        admins = self._admin_ids()
        if not admins:
            return []
        targets: list[str] = []
        for platform in self._platform_insts():
            meta_getter = getattr(platform, "meta", None)
            meta = meta_getter() if callable(meta_getter) else None
            if str(getattr(meta, "name", "")).lower() != "aiocqhttp":
                continue
            if getattr(meta, "support_proactive_message", True) is False:
                continue
            platform_id = str(getattr(meta, "id", "") or "")
            if not platform_id:
                continue
            for admin_id in admins:
                target = f"{platform_id}:FriendMessage:{admin_id}"
                if target not in targets:
                    targets.append(target)
        return targets

    async def notify_restart_to_admins(
        self,
        *,
        attempts: int = 3,
        wait_seconds: float = 5.0,
    ) -> int:
        """Report a restart to every admin; platforms may still be connecting.

        Returns the number of admins successfully notified.
        """
        targets = self.admin_restart_targets()
        if not targets:
            self._log(
                "info",
                "no aiocqhttp admin targets configured; skip restart notification",
            )
            return 0
        notified = 0
        for umo in targets:
            if await self._send_with_retry(
                umo,
                self.RESTART_NOTIFY_TEXT,
                attempts=attempts,
                wait_seconds=wait_seconds,
            ):
                notified += 1
        return notified

    async def _send_with_retry(
        self,
        umo: str,
        text: str,
        *,
        attempts: int,
        wait_seconds: float,
    ) -> bool:
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                if await self.send_to_session(umo, text):
                    return True
                last_error = ContextClearError(f"会话 {umo} 不属于任何已加载平台。")
            except Exception as exc:  # platform may still be connecting
                last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(wait_seconds)
        self._log(
            "warning",
            "restart notification to %s failed after retries: %s",
            umo,
            last_error,
        )
        return False

    def _admin_ids(self) -> list[str]:
        config_getter = getattr(self.context, "get_config", None)
        config = config_getter() if callable(config_getter) else None
        admins = getattr(config, "get", lambda *_: None)("admins_id", [])
        if isinstance(admins, str):
            admins = [admins]
        if not isinstance(admins, (list, tuple, set)):
            return []
        return [str(item).strip() for item in admins if str(item).strip()]

    def _platform_insts(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        insts = getter() if callable(getter) else []
        return list(insts or [])

    # ------------------------------------------------------------------

    def _log(self, level: str, message: str, *args: Any) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message, *args)
        except (AttributeError, TypeError, ValueError):
            pass
