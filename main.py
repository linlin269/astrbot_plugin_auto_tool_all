"""AstrBot plugin: dynamically orchestrate global tools with QQ-avatar references."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .avatar_service import AvatarError, AvatarService
from .context_clear import (
    ContextClearError,
    ContextClearService,
    format_duration,
    parse_duration,
    session_label,
)
from .event_images import extract_image_sources
from .media_delivery import (
    MediaDeliveryService,
    extract_media_urls_from_text,
    parse_kept_indexes,
)
from .openai_probe import (
    OpenAIProbeService,
    ProbeError,
    detect_probe_mode,
    extract_api_key,
    extract_explicit_prefix,
    first_api_url,
)
from .tool_bridge import ToolBridge, ToolBridgeError

PLUGIN_NAME = "astrbot_plugin_auto_tool_all"
_INTERNAL_TOOL_NAMES = ToolBridge.INTERNAL_TOOLS

# 钩子层自动转发跳过名单：内部工具自带交付/桥接逻辑，生图工具自行发送图片，
# 重复转发会造成同一张图双发。
_AUTO_DELIVER_SKIP_TOOLS = frozenset(
    {
        "list_available_tools",
        "call_plugin_tool",
        "fetch_media",
        "avatar_draw",
        "generate_image",
        "generate_selfie",
    }
)

# AI 精筛单图 base64 上限（字符数，约 6MB 原图）；超大的图直接保留不送审，
# 避免一张大图触发视觉接口报错导致整次审阅失败。
_REVIEW_MAX_PAYLOAD_CHARS = 8_000_000

# 模型测速：达到该数量才先发“开始测试”预告，小列表直接等结果。
_TEST_ACK_THRESHOLD = 6
_PROBE_GUIDANCE = (
    "请先把 API 地址（url）和 key 发给我——可以分开几条消息发，也可以写在一起，"
    "我会记住（内存缓存，不落盘）。然后再说一次"
    "“看看里面有什么模型”或“帮我测试一下里面的模型”。"
)

_CLEAR_USAGE = (
    "清空上下文指令用法：\n"
    "/清空上文 —— 立即清空当前会话上下文\n"
    "/清空上文 all —— 立即清空所有会话上下文\n"
    "/清空上文 6s|6秒|5min|5分钟|1h|1小时 —— 定时清空当前会话上下文\n"
    "/查看清空上文定时任务 —— 查看当前定时任务\n"
    "/取消清空上文定时任务 序号|qq号|群号|all —— 取消定时任务"
)

# astrbot_plugin_selfie_image 的 generate_image 会按这些词把请求改道到
# “AI 自拍”流程，并以机器人形象图作为身份锚点，导致画谁都是机器人。
# 为非 bot 身份合成提示词时，把这些触发词改写成不会命中的等价说法。
_LOOKALIKE_REWRITES_ZH: tuple[tuple[str, str], ...] = (
    ("我们一起", "两位人物共同"),
    ("你的照片", "目标人物的照片"),
    ("你自己", "目标人物本人"),
    ("你和我", "两位人物"),
    ("我和你", "两位人物"),
    ("形象照", "个人形象写真"),
    ("一起拍", "共同拍摄"),
    ("一起照", "共同拍摄"),
    ("合影", "多人物同画面"),
    ("合照", "多人物同画面"),
    ("同框", "同画面"),
    ("自拍", "个人生活照"),
    ("陪我", "为发送者"),
    ("和我", "为发送者"),
    ("跟我", "为发送者"),
    ("与我", "为发送者"),
    ("和你", "与机器人"),
    ("跟你", "与机器人"),
    ("与你", "与机器人"),
)
_LOOKALIKE_REWRITES_EN: tuple[tuple[str, str], ...] = (
    ("take a picture together", "people together in one shot"),
    ("take a photo together", "people together in one shot"),
    ("in the same frame", "in one frame"),
    ("group selfie", "multi-person portrait"),
    ("group photo", "multi-person photo"),
    ("photo together", "people together in one shot"),
    ("your photo", "the target person's photo"),
    ("yourself", "the target person"),
    ("next to me", "next to the target person"),
    ("next to you", "next to the bot character"),
    ("with me", "with the target person"),
    ("with you", "with the bot character"),
    ("same frame", "one frame"),
    ("side by side", "side-by-side"),
    ("ai assistant", "the character"),
    ("selfie", "self-portrait"),
    ("catgirl", "original character"),
    ("ahwu", "original character"),
)


def _chunk_lines(lines: list[str], limit: int = 1800) -> list[str]:
    """把多行文本按 QQ 消息长度上限切成若干条，避免超长发送失败。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


class AutoToolAll(Star):
    """Natural-language access to AstrBot tools plus QQ-avatar image orchestration."""

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ):
        super().__init__(context)
        self.config = config or {}
        self._data_dir = self._resolve_data_dir()
        self.avatar_service = AvatarService(self._data_dir, self.config, logger)
        self.tool_bridge = ToolBridge(context, self.config, logger)
        self.context_clear = ContextClearService(context, logger)
        self.media_delivery = MediaDeliveryService(self._data_dir, self.config, logger)
        self.probe = OpenAIProbeService(self.config, logger)
        # 模型探测的 url/key 会话内记忆：{umo: {field: (value, timestamp)}}。
        # 只存内存、带 TTL；key 在一次成功使用后立即丢弃。
        self._probe_memory: dict[str, dict[str, tuple[str, float]]] = {}

    async def initialize(self) -> None:
        """Log the currently visible tools without taking ownership of them."""
        try:
            tools = self.tool_bridge.list_tools()
            logger.info(
                "%s loaded; %d external LLM tools available: %s",
                PLUGIN_NAME,
                len(tools),
                ", ".join(item.name for item in tools[:20]) or "none",
            )
        except (AttributeError, TypeError, ToolBridgeError) as exc:
            logger.warning(
                "%s tool discovery failed during initialization: %s", PLUGIN_NAME, exc
            )

    async def terminate(self) -> None:
        """Cancel scheduled clears; no other background task is retained."""
        self.context_clear.shutdown()
        self._probe_memory.clear()

    @filter.on_astrbot_loaded()
    async def notify_restart(self) -> None:
        """Report restart to admins; scheduled clears do not survive restarts."""
        try:
            notified = await self.context_clear.notify_restart_to_admins()
            if notified:
                logger.info(
                    "%s restart notification delivered to %d admin(s).",
                    PLUGIN_NAME,
                    notified,
                )
        except ContextClearError as exc:
            logger.warning("%s restart notification failed: %s", PLUGIN_NAME, exc)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("%s restart notification failed: %s", PLUGIN_NAME, exc)

    # ------------------------------------------------------------------
    # Context clearing commands (admin only, silent for members)
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("清空上文", alias={"清空对话"}, priority=2)
    async def clear_context_command(
        self, event: AstrMessageEvent, action: str = ""
    ) -> AsyncGenerator[Any, None]:
        """清空 LLM 对话上下文；支持 all 与定时（6s/6秒/5min/5分钟/1h/1小时）。"""
        argument = (action or "").strip()
        umo = event.unified_msg_origin

        try:
            if not argument:
                await self.context_clear.clear_session(umo)
                yield event.plain_result("已清空当前会话上下文。")
                return
            if argument.lower() == "all":
                count = await self.context_clear.clear_all_sessions()
                yield event.plain_result(f"已清空 {count} 个会话的上下文。")
                return
            duration = parse_duration(argument)
            if duration is None:
                yield event.plain_result(_CLEAR_USAGE)
                return
            replaced = self.context_clear.schedule_clear(umo, duration)
            prefix = "已覆盖原定时，" if replaced else ""
            yield event.plain_result(
                f"{prefix}将在 {format_duration(duration)} 后清空当前会话上下文。"
            )
        except ContextClearError as exc:
            yield event.plain_result(f"清空上下文失败：{exc}")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.exception("clear_context_command failed")
            yield event.plain_result(f"清空上下文时发生异常：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("查看清空上文定时任务", priority=2)
    async def list_scheduled_clears_command(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """列出当前所有上下文定时清空任务，按触发时间排序。"""
        entries = self.context_clear.sorted_entries()
        if not entries:
            yield event.plain_result("当前没有上下文定时清空任务。")
            return
        now = datetime.now()
        lines = ["当前定时清空任务："]
        for index, entry in enumerate(entries, 1):
            remaining = format_duration(entry.fire_at - now)
            lines.append(
                f"{index}. {session_label(entry.umo)} — "
                f"{entry.fire_at.strftime('%H:%M:%S')}（还有 {remaining}）"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("取消清空上文定时任务", priority=2)
    async def cancel_scheduled_clears_command(
        self, event: AstrMessageEvent, action: str = ""
    ) -> AsyncGenerator[Any, None]:
        """取消定时清空任务；参数为序号、qq号/群号或 all。"""
        argument = (action or "").strip()
        if not argument:
            yield event.plain_result(
                "用法：/取消清空上文定时任务 序号|qq号|群号|all\n"
                "序号以 /查看清空上文定时任务 的列表为准。"
            )
            return

        if argument.lower() == "all":
            removed = self.context_clear.cancel_all()
            if not removed:
                yield event.plain_result("当前没有可取消的定时任务。")
                return
            yield event.plain_result(self._format_cancelled(removed))
            return

        removed: list[Any] = []
        if argument.isdigit() and 1 <= int(argument) <= len(
            self.context_clear.sorted_entries()
        ):
            # Small numbers that exist in the listed indexes are treated as
            # indexes first; qq/group numbers never collide in practice.
            entry = self.context_clear.cancel_by_index(int(argument))
            removed = [entry] if entry else []
        else:
            removed = self.context_clear.cancel_for_session_id(argument)

        if removed:
            yield event.plain_result(self._format_cancelled(removed))
            return
        yield event.plain_result(
            "没有匹配的定时任务。用 /查看清空上文定时任务 查看当前序号和会话。"
        )

    @staticmethod
    def _format_cancelled(removed: list[Any]) -> str:
        labels = "、".join(session_label(entry.umo) for entry in removed)
        return f"已取消 {len(removed)} 个定时任务：{labels}。"

    @filter.llm_tool(name="list_available_tools")
    async def list_available_tools(self, event: AstrMessageEvent) -> str:
        """列出当前 AstrBot 中可供自然语言调用的外部工具。

        当用户询问“有哪些工具”“当前能做什么”或需要选择某个插件工具时调用。
        不要把本插件内部的工具列出为外部能力。
        """
        del event
        if not self._as_bool("enable_tool_listing", True):
            return "工具列表查询已被管理员关闭。"
        try:
            tools = self.tool_bridge.list_tools()
        except ToolBridgeError as exc:
            return f"工具列表暂时不可用：{exc}"
        if not tools:
            return "当前没有发现可调用的外部 LLM 工具。"

        lines = ["当前可调用工具："]
        for item in tools[:40]:
            description = " ".join(item.description.split())
            if len(description) > 180:
                description = description[:177] + "..."
            parameter_names = list(item.parameters.get("properties", {}).keys())
            suffix = f"；参数：{', '.join(parameter_names)}" if parameter_names else ""
            lines.append(f"- {item.name}: {description or '未提供描述'}{suffix}")
        if len(tools) > 40:
            lines.append(f"（还有 {len(tools) - 40} 个工具未展开。）")
        return "\n".join(lines)

    @filter.llm_tool(name="call_plugin_tool")
    async def call_plugin_tool(
        self,
        event: AstrMessageEvent,
        tool_name: str,
        arguments: str = "{}",
    ) -> str:
        """调用当前 AstrBot 已注册的任意外部 LLM 工具。

        适用于用户明确要求调用某个插件工具、搜索工具、资源工具或未来新安装插件的工具。
        搜索/信息查询类工具若已注册为全局工具，可直接调用，无需经过本入口；
        只有需要显式指定工具名，或主模型无法直接选择时才使用本入口。
        本工具不能调用自身的内部工具，也不能调用只有 /指令 而未注册为 LLM 工具的插件。
        重要：当用户意图是“下载图片/视频发给我”时，无论通过哪条途径搜索，
        拿到候选链接后必须调用 fetch_media 完成下载与发送；
        链接里提取不到媒体时如实告知用户，不要改用生图工具冒充下载结果。
        Args:
            tool_name(string): 要调用的外部工具名称，例如 anysearch_batch_search、search_magnet、preview_magnet 或未来插件注册的工具名。
            arguments(string): JSON 对象形式的工具参数，例如 {"queries":"[\\"AstrBot\\"]"}。
        """
        try:
            parsed = self.tool_bridge.parse_json_arguments(arguments)
            result_text, response = await self.tool_bridge.invoke_result(
                event,
                tool_name,
                parsed,
                prompt_prefix=(
                    "你是 AstrBot 的工具桥。这个请求来自用户，必须只执行指定工具一次，"
                    "不要改调用目标，也不要把工具返回内容编造成不存在的事实。"
                ),
            )
        except ToolBridgeError as exc:
            return f"无法调用工具 `{tool_name}`：{exc}"
        except (
            AttributeError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:  # pragma: no cover - adapter/provider-specific
            logger.exception("call_plugin_tool failed for %s", tool_name)
            return f"调用工具 `{tool_name}` 时发生异常：{exc}"

        media_note = await self._deliver_tool_media(event, response, result_text)
        if media_note:
            return f"{result_text}\n\n{media_note}"
        return result_text

    @filter.llm_tool(name="fetch_media")
    async def fetch_media(
        self, event: AstrMessageEvent, urls: list[str], user_intent: str = ""
    ) -> str:
        """把图片/视频直链或含媒体的网页链接下载后，以 base64 图片/视频消息发给用户。

        当用户要求“下载后发给我”“把这张图/视频发给我”“把搜到的图发我”，
        或需要把搜索结果、前文提到的链接变成真正的媒体消息时调用。
        urls 可填图片/视频直链（如 https://.../xxx.jpg），也可填 B 站视频页等
        网页链接（自动提取页面中的封面图与插图，并剔除图标/头像等干扰图，
        再由 AI 按用户诉求审阅挑选）。
        一次最多发送 media_max_count 个媒体；临时文件发送后立即删除。

        行为约定：如果所有链接都提取不到可下载的内容图片，就如实告诉用户
        “没找到可直接下载的图片/视频”，并把原始链接发给用户自行查看；
        禁止改用 generate_image 等生图工具，把生成的图冒充下载的图。
        生图工具只用于用户明确要求“画一张/生成一张”的场景。
        Args:
            urls(array[string]): 图片/视频直链或含媒体的网页链接列表，例如 ["https://www.bilibili.com/video/BV1H8411r735/"]。
            user_intent(string): 用户对图片的原始诉求要点，如“Q版企鹅二创壁纸”；AI 审阅时会用它匹配内容，可留空。
        """
        if not self._as_bool("fetch_media_enabled", True):
            return "管理员已关闭媒体下载功能（fetch_media_enabled）。"
        normalized = self._normalize_urls(urls)
        if not normalized:
            return "没有收到任何链接。请把图片/视频直链或网页链接放进 urls 参数。"
        fresh = self._filter_fresh_urls(event, normalized)
        if not fresh:
            return "这些链接的图片刚刚已经发送过了，没有新的媒体需要下载。"
        try:
            async def _review(prepared, intent, page_title):
                return await self._llm_review_media(event, prepared, intent, page_title)

            result = await self.media_delivery.fetch_media_result(
                event,
                fresh,
                page_extract=self._as_bool("fetch_media_page_extract", True),
                filter_noise=self._as_bool("fetch_media_filter_noise", True),
                review=_review,
                intent=str(user_intent or "").strip(),
            )
        except Exception:  # pragma: no cover - network/platform specific
            logger.exception("fetch_media failed")
            return "下载媒体时发生异常，请稍后再试，或把原始链接直接发给用户查看。"
        self._mark_delivered_urls(event, fresh)
        return result

    async def _llm_review_media(
        self,
        event: AstrMessageEvent,
        prepared: list[tuple[str, str, str]],
        intent: str,
        page_title: str,
    ) -> list[int] | None:
        """AI 精筛（第二级清洗）：让当前聊天模型看图剔除干扰图并按意图挑选。

        返回应保留的 prepared 下标列表；返回 None 表示审阅不可用（模型不
        支持视觉、超时、解析失败），调用方按约定 fail-open 全部发送。
        视频与超大图片不送审，直接保留。
        """
        if not self._as_bool("llm_filter_enabled", True):
            return None
        reviewable: list[tuple[int, str]] = []
        auto_keep: set[int] = set()
        for index, (url, kind, payload) in enumerate(prepared):
            if kind == "image" and len(payload) <= _REVIEW_MAX_PAYLOAD_CHARS:
                reviewable.append((index, payload))
            else:
                auto_keep.add(index)
        if not reviewable:
            return sorted(auto_keep)

        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            getter = getattr(self.context, "get_current_chat_provider_id", None)
            provider_id = str(await getter(umo)) if callable(getter) else ""
            getter = getattr(self.context, "get_provider_by_id", None)
            provider = await getter(provider_id) if callable(getter) else None
        except Exception:  # pragma: no cover - provider manager differences
            provider = None
        if not provider_id or provider is None:
            self._debug("llm review skipped: no chat provider available")
            return None

        count = len(reviewable)
        prompt = (
            "你是发往聊天软件的图片审阅员。下面按顺序给出"
            f"{count} 张候选图片（编号 0 到 {count - 1}），它们来自同一个网页。"
            "请剔除其中的网站图标、站标、用户头像、表情符号、装饰按钮、二维码、"
            "跟踪像素等非内容图片；保留真正的内容图片（壁纸、照片、插画、"
            "截图、封面等）。\n"
            f"页面标题：{page_title or '未知'}\n"
            f"用户想要的图片：{intent or '未特别说明，按常规内容图片标准判断'}\n"
            "只输出一个 JSON 数组，包含应保留的候选编号，例如 [0,2]；"
            "如果全部都是干扰图，输出 []。不要输出任何其他文字。"
        )
        try:
            timeout = self._int_config("llm_filter_timeout", 20, 120)
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    image_urls=[f"base64://{payload}" for _, payload in reviewable],
                ),
                timeout=timeout,
            )
        except Exception as exc:  # pragma: no cover - provider/network specific
            self._debug("llm review failed: %s", exc)
            return None
        kept = parse_kept_indexes(ToolBridge.response_text(response), count)
        if kept is None:
            self._debug("llm review output unparseable; fail-open")
            return None
        keep = auto_keep | {reviewable[index][0] for index in kept}
        return sorted(keep)

    @filter.on_llm_tool_respond()
    async def auto_deliver_tool_media(
        self,
        event: AstrMessageEvent,
        tool: Any = None,
        tool_args: dict | None = None,
        tool_result: Any = None,
    ) -> None:
        """主 Agent 直接调用任意工具返回后，把结果中的媒体转成 base64 发出。

        覆盖模型绕过 call_plugin_tool 直接调用 anysearch 等全局工具的路径。
        call_plugin_tool / fetch_media 自带交付，生图工具自行发图，均跳过；
        内层 tool_loop_agent 默认不挂本钩子，因此不会与 call_plugin_tool 重复。
        钩子内异常由 AstrBot 捕获记录，不影响消息主流程。
        """
        if not self._as_bool("auto_deliver_tool_media", True):
            return
        if not self.media_delivery.enabled():
            return
        try:
            tool_name = str(getattr(tool, "name", "") or "")
            if tool_name in _AUTO_DELIVER_SKIP_TOOLS:
                return
            urls = self._extract_hook_media_urls(tool_result)
            if not urls:
                return
            fresh = self._filter_fresh_urls(event, urls)
            if not fresh:
                return
            report = await self.media_delivery.deliver(event, fresh)
            self._mark_delivered_urls(event, fresh)
        except Exception:  # pragma: no cover - defensive hook boundary
            logger.exception("auto media delivery failed")
            return
        if report.sent_images or report.sent_videos:
            logger.info(
                "%s auto-delivered %d image(s) and %d video(s) from tool `%s`.",
                PLUGIN_NAME,
                report.sent_images,
                report.sent_videos,
                tool_name or "unknown",
            )

    @staticmethod
    def _extract_hook_media_urls(tool_result: Any) -> list[str]:
        """Pull media URLs from a hook's tool_result across result shapes.

        AstrBot v4.x wraps local tool text in CallToolResult(content=[TextContent]);
        older or third-party payloads may pass the raw string directly.
        """
        urls: list[str] = []
        if tool_result is None:
            return urls
        if isinstance(tool_result, str):
            return extract_media_urls_from_text(tool_result)
        content = getattr(tool_result, "content", None)
        if isinstance(content, list):
            for item in content:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    for url in extract_media_urls_from_text(text):
                        if url not in urls:
                            urls.append(url)
            return urls
        for attr in ("text", "completion_text", "result"):
            value = getattr(tool_result, attr, None)
            if isinstance(value, str):
                for url in extract_media_urls_from_text(value):
                    if url not in urls:
                        urls.append(url)
                break
        return urls

    @staticmethod
    def _delivered_url_store(event: AstrMessageEvent) -> set[str]:
        """Per-event set of media URLs already handled this conversation round."""
        store = getattr(event, "_auto_tool_all_delivered_urls", None)
        if not isinstance(store, set):
            store = set()
            # Read-only event objects reject attribute writes; dedupe then
            # degrades to best effort within each handler call.
            with contextlib.suppress(Exception):
                event._auto_tool_all_delivered_urls = store
        return store

    def _filter_fresh_urls(
        self, event: AstrMessageEvent, urls: list[str]
    ) -> list[str]:
        store = self._delivered_url_store(event)
        fresh: list[str] = []
        for url in urls:
            if url and url not in store and url not in fresh:
                fresh.append(url)
        return fresh

    def _mark_delivered_urls(self, event: AstrMessageEvent, urls: list[str]) -> None:
        self._delivered_url_store(event).update(urls)

    async def _deliver_tool_media(
        self, event: AstrMessageEvent, response: Any, result_text: str
    ) -> str:
        """把工具结果里的媒体 URL 下载后以 base64 图片/视频消息发出。

        媒体来源有两处：内层响应消息链中的 Image/Video 组件，
        以及工具文本结果里的媒体直链。发送后临时文件立即清理。
        与钩子层共用同一份已发送记录，避免同一链接双发。
        """
        if not self.media_delivery.enabled():
            return ""
        urls = self.tool_bridge.extract_media_urls(response)
        for url in extract_media_urls_from_text(result_text):
            if url not in urls:
                urls.append(url)
        if not urls:
            return ""
        fresh = self._filter_fresh_urls(event, urls)
        if not fresh:
            return ""
        try:
            report = await self.media_delivery.deliver(event, fresh)
        except Exception:
            logger.exception("media delivery failed")
            return ""
        self._mark_delivered_urls(event, fresh)
        if report.sent_images or report.sent_videos or report.fallback_urls:
            return report.summary()
        return ""

    # ------------------------------------------------------------------
    # OpenAI 兼容接口：模型查看与测速（自然语言 + url/key 记忆触发）
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def openai_probe_listener(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """监听“看看里面有什么模型 / 帮我测试一下里面的模型”类话语。

        url 和 key 支持四种给法：写在同一句、回复引用、上一条消息、
        分多条消息先后发送（内存记忆 30 分钟）。命中即拦截事件，
        直接调 HTTP 接口，测试结果按总回复时间升序汇总推送。
        """
        try:
            prepared = self._prepare_probe(event)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            logger.exception("openai probe preparation failed")
            return
        if prepared is None:
            return

        mode, url, key, prefix, missing = prepared
        event.stop_event()
        if missing:
            yield event.plain_result(missing)
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            models = await self.probe.list_models(url, key, prefix)
        except ProbeError as exc:
            yield event.plain_result(f"获取模型列表失败：{exc}")
            return
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.exception("openai probe list_models failed")
            yield event.plain_result(f"获取模型列表时发生异常：{exc}")
            return

        if mode == "list":
            lines = [f"该接口共有 {len(models)} 个模型："]
            lines.extend(f"{i}. {name}" for i, name in enumerate(models, 1))
            for chunk in _chunk_lines(lines):
                yield event.plain_result(chunk)
            self._forget_probe_key(umo)
            return

        concurrency = self.probe.concurrency()
        timeout = self.probe.timeout_seconds()
        if len(models) >= _TEST_ACK_THRESHOLD:
            yield event.plain_result(
                f"开始测试，共 {len(models)} 个模型"
                f"（{concurrency} 个并发，每个 {timeout} 秒超时），测完自动汇报。"
            )
        try:
            probe_result = await self.probe.probe_models(
                url, key, models, api_prefix=prefix
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.exception("openai probe_models failed")
            yield event.plain_result(f"测试过程发生异常：{exc}")
            return
        for chunk in _chunk_lines(probe_result.summary().splitlines()):
            yield event.plain_result(chunk)
        self._forget_probe_key(umo)

    def _prepare_probe(
        self, event: AstrMessageEvent
    ) -> tuple[str, str, str, str, str] | None:
        """解析触发语并配对 url/key；返回 (mode, url, key, prefix, missing)。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        quoted = self._quoted_text(event)
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        mode = detect_probe_mode(text)
        if not mode:
            self._harvest_probe_inputs(umo, quoted, text)
            return None
        if not self._as_bool("probe_enabled", True):
            return None

        url = (
            first_api_url(text)
            or first_api_url(quoted)
            or self._probe_memory_value(umo, "url")
        )
        key = (
            extract_api_key(text)
            or extract_api_key(quoted)
            or self._probe_memory_value(umo, "key")
        )
        prefix = extract_explicit_prefix(text) or self._probe_memory_value(
            umo, "prefix"
        )
        self._harvest_probe_inputs(umo, quoted, text)

        if url and key:
            return mode, url, key, prefix, ""
        # 提到“里面/全部/所有”说明用户明确指向之前发过的服务，给引导而不是沉默。
        if re.search(r"里面|全部|所有", text):
            return mode, "", "", "", _PROBE_GUIDANCE
        return None

    def _harvest_probe_inputs(self, umo: str, *sources: str) -> None:
        """把消息里的 url/key/前缀记入内存；TTL 过期即丢，绝不写日志。"""
        if not umo:
            return
        now = time.time()
        ttl = self._probe_ttl_seconds()
        entry = self._probe_memory.setdefault(umo, {})
        for field_name in list(entry.keys()):
            _, stamp = entry[field_name]
            if now - stamp > ttl:
                entry.pop(field_name, None)
        for source in sources:
            url = first_api_url(source)
            if url:
                entry["url"] = (url, now)
                prefix = extract_explicit_prefix(source)
                if prefix:
                    entry["prefix"] = (prefix, now)
            key = extract_api_key(source)
            if key:
                entry["key"] = (key, now)
        if not entry:
            self._probe_memory.pop(umo, None)

    def _probe_memory_value(self, umo: str, field_name: str) -> str:
        entry = self._probe_memory.get(umo)
        if not entry:
            return ""
        item = entry.get(field_name)
        if not item:
            return ""
        value, stamp = item
        if time.time() - stamp > self._probe_ttl_seconds():
            entry.pop(field_name, None)
            if not entry:
                self._probe_memory.pop(umo, None)
            return ""
        return value

    def _forget_probe_key(self, umo: str) -> None:
        """用完即弃：一次成功的列表/测试之后立刻丢弃 key。"""
        entry = self._probe_memory.get(umo)
        if not entry:
            return
        entry.pop("key", None)
        if not entry:
            self._probe_memory.pop(umo, None)

    def _probe_ttl_seconds(self) -> int:
        try:
            minutes = int(self.config.get("probe_memory_ttl_minutes", 30))
        except (TypeError, ValueError):
            minutes = 30
        return max(1, min(minutes, 1440)) * 60

    @staticmethod
    def _quoted_text(event: AstrMessageEvent) -> str:
        """取出回复/引用消息的纯文本，供 url/key 配对。"""
        message_obj = getattr(event, "message_obj", None)
        components = getattr(message_obj, "message", None)
        if not isinstance(components, list):
            return ""
        parts: list[str] = []
        for component in components:
            if type(component).__name__ != "Reply":
                continue
            text = str(getattr(component, "message_str", "") or "").strip()
            if text:
                parts.append(text)
            chain = getattr(component, "chain", None)
            if isinstance(chain, list):
                for item in chain:
                    piece = getattr(item, "text", None)
                    if isinstance(piece, str) and piece.strip():
                        parts.append(piece.strip())
        return "\n".join(parts)

    @filter.llm_tool(name="avatar_draw")
    async def avatar_draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        identity: str = "bot",
        count: int = 1,
        reference_image_urls: list[str] | None = None,
        ack_message: str = "",
    ) -> str:
        """以 QQ 头像为身份参考，调用其它生图工具完成文生图或图生图。

        当用户想用“你的头像/我的头像/TA 的头像”画画或修改图片时调用。
        典型触发包括“看看你”（用机器人自己的 QQ 头像画自己）、“看看我”、
        “@某人 看看他”、“把图里的人物换成你”，以及“先搜索一张图，再把图里的人换成你”。
        identity 必须按人物归属严格传参：用户说“我/我的/我自己”必须传 sender，
        说“你/机器人/你自己”必须传 bot，消息中明确 @ 了其他人时传 at。
        不要省略 identity，也不要传列表之外的值。
        当前消息直接附带图片、回复/引用带图片的消息，都会自动作为图生图输入。
        只有已注册为 LLM 工具的生图能力可被调用；普通 /指令 不在本工具范围内。
        Args:
            prompt(string): 用户想要的画面或修改要求，保留动作、场景、风格，以及头像人物和原图之间的关系。
            identity(string): 头像归属，只能使用 bot（机器人）、sender（发送者）、at（被@用户）。“看看我”必须传 sender。
            count(number): 生成张数，默认 1。
            reference_image_urls(array[string]): 上一步搜索或其它工具返回的外部图片 URL，可为空数组。
            ack_message(string): 可选的中文进度短句，10 到 40 字。
        """
        if not self._is_aiocqhttp(event):
            return "头像功能目前只支持 aiocqhttp/OneBot v11 QQ 事件。"

        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return "缺少绘图要求，请说明想画什么或怎样修改图片。"

        external_urls = self._normalize_urls(reference_image_urls)
        source_images = extract_image_sources(event)
        if (
            self._requires_source_image(prompt_text)
            and not source_images
            and not external_urls
        ):
            return (
                "这个请求需要原图，请直接发送图片，或回复/引用一条带图片的消息后再说。"
            )

        try:
            normalized_identity = self.avatar_service.normalize_identity(identity)
            self._debug(
                "avatar_draw: identity %r -> %s", identity, normalized_identity
            )
            target = await self.avatar_service.resolve_for_event(
                event, normalized_identity
            )
            self._debug("avatar_draw: resolved qq=%s url=%s", target.qq, target.url)
            max_refs = self._int_config("max_reference_images", 4, minimum=1, maximum=8)
            external_paths = await self.avatar_service.download_external_images(
                external_urls,
                max_count=max(0, max_refs - 1),
            )
            image_tool_name = self._choose_image_tool(normalized_identity)
            if not image_tool_name:
                return (
                    "没有找到可用的生图 LLM 工具。"
                    "画“我/TA”这类身份需要支持参考图传入的工具（如 generate_image）；"
                    "generate_selfie 只能以机器人自己的形象出镜，已被自动跳过。"
                    "请确认 astrbot_plugin_selfie_image 已加载并在其 Web 面板开启 "
                    "image_enable_llm_tool，或在本插件配置 image_tool_name 指定其它生图工具。"
                )
            self._debug(
                "avatar_draw: tool=%s event_images=%d external=%d",
                image_tool_name,
                len(source_images),
                len(external_paths),
            )

            if normalized_identity != "bot":
                prompt_text = self._sanitize_bot_lookalike_phrases(prompt_text)
            composed_prompt = self._compose_avatar_prompt(
                prompt_text,
                normalized_identity,
                has_source=bool(source_images or external_paths),
            )
            reference_paths = [target.path, *external_paths]
            args = self._image_tool_arguments(
                image_tool_name,
                composed_prompt,
                count=count,
                ack_message=ack_message,
                reference_paths=reference_paths,
                event_sources=[item.source for item in source_images],
            )
            with self._inject_reference_images(
                event, reference_paths, max_refs=max_refs
            ):
                result = await self.tool_bridge.invoke(
                    event,
                    image_tool_name,
                    args,
                    prompt_prefix=(
                        "你正在执行头像生图。只能调用指定的生图工具一次。"
                        "事件中已有图片是用户消息/引用图片；临时追加的本地图片是头像或跨工具参考图。"
                        "请把它们按工具自身的参考图能力传递给生图后端，不要把本地路径写进给用户的回复。"
                    ),
                )
            identity_label = {"bot": "机器人", "sender": "发送者", "at": "被@用户"}.get(
                normalized_identity, "目标"
            )
            return f"已使用{identity_label}的 QQ 头像调用 `{image_tool_name}`。工具结果：{result}"
        except AvatarError as exc:
            return f"头像获取失败：{exc}"
        except ToolBridgeError as exc:
            return f"头像生图工具调用失败：{exc}"
        except (
            AttributeError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:  # pragma: no cover - provider/adapter-specific
            logger.exception("avatar_draw failed")
            return f"头像生图时发生异常：{exc}"

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.api.star import StarTools

            getter = getattr(StarTools, "get_data_dir", None)
            if callable(getter):
                try:
                    path = getter(PLUGIN_NAME)
                except TypeError:
                    path = getter()
                return Path(path)
        except (ImportError, AttributeError, OSError, RuntimeError, TypeError) as exc:
            logger.debug("StarTools data directory unavailable: %s", exc)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except (ImportError, AttributeError, OSError, RuntimeError, TypeError) as exc:
            logger.debug("AstrBot data directory unavailable: %s", exc)
            path = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _as_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)

    def _debug_enabled(self) -> bool:
        return self._as_bool("debug_logging", False)

    def _debug(self, message: str, *args: Any) -> None:
        if not self._debug_enabled():
            return
        method = getattr(logger, "debug", None)
        if not callable(method):
            return
        try:
            method(f"{PLUGIN_NAME} {message}", *args)
        except (AttributeError, TypeError, ValueError):
            pass

    def _int_config(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (AttributeError, TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _choose_image_tool(self, identity: str = "bot") -> str:
        # selfie-only tools always render the bot's own persona; when drawing
        # someone else's avatar they would silently ignore the injected refs.
        selfie_only = {"generate_selfie"}
        configured: list[str] = []
        preferred = str(
            self.config.get("image_tool_name", "generate_image") or ""
        ).strip()
        if preferred:
            configured.append(preferred)
        candidates = self.config.get(
            "image_tool_candidates", ["generate_image", "generate_selfie"]
        )
        if isinstance(candidates, str):
            candidates = candidates.split(",")
        if isinstance(candidates, (list, tuple, set)):
            configured.extend(
                str(item).strip() for item in candidates if str(item).strip()
            )
        seen: set[str] = set()
        for name in configured:
            if name in seen or name in _INTERNAL_TOOL_NAMES:
                continue
            seen.add(name)
            if identity != "bot" and name.lower() in selfie_only:
                if self._debug_enabled():
                    logger.debug(
                        "%s skipped tool %s: it can only draw the bot itself",
                        PLUGIN_NAME,
                        name,
                    )
                continue
            try:
                self.tool_bridge.get_target_tool(name)
            except ToolBridgeError:
                continue
            return name
        return ""

    def _image_tool_arguments(
        self,
        tool_name: str,
        prompt: str,
        *,
        count: int,
        ack_message: str,
        reference_paths: list[Path],
        event_sources: list[str],
    ) -> dict[str, Any]:
        tool = self.tool_bridge.get_target_tool(tool_name)
        properties = getattr(tool, "parameters", {})
        properties = (
            properties.get("properties", {}) if isinstance(properties, dict) else {}
        )
        args: dict[str, Any] = {}
        if "prompt" in properties:
            args["prompt"] = prompt
        elif "action" in properties:
            args["action"] = prompt
        elif "instruction" in properties:
            args["instruction"] = prompt
        elif properties:
            # Let schema validation provide a clear missing-parameter error later.
            first = next(iter(properties))
            args[first] = prompt
        if "count" in properties:
            args["count"] = self._int_config_value(count, 1, 1, 8)
        if "ack_message" in properties and ack_message:
            args["ack_message"] = str(ack_message).strip()[:80]
        if "image_urls" in properties:
            args["image_urls"] = [
                *event_sources,
                *(str(path) for path in reference_paths),
            ]
        elif "reference_images" in properties:
            args["reference_images"] = [str(path) for path in reference_paths]
        elif "references" in properties:
            args["references"] = [str(path) for path in reference_paths]
        return args

    @staticmethod
    def _int_config_value(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _normalize_urls(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _is_aiocqhttp(event: Any) -> bool:
        getter = getattr(event, "get_platform_name", None)
        names = []
        if callable(getter):
            try:
                names.append(str(getter()).lower())
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            try:
                names.append(str(getter()).lower())
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
        names.extend(
            str(getattr(event, key, "") or "").lower()
            for key in ("platform_name", "platform_id")
        )
        if any("aiocqhttp" in name or "onebot" in name for name in names if name):
            return True
        return getattr(event, "bot", None) is not None and bool(
            getattr(getattr(event, "message_obj", None), "self_id", "")
        )

    @staticmethod
    def _requires_source_image(prompt: str) -> bool:
        keywords = (
            "图里",
            "图片里",
            "原图",
            "这张图",
            "该图",
            "替换",
            "换成",
            "图生图",
            "改图",
            "修改图片",
        )
        return any(keyword in prompt for keyword in keywords)

    @staticmethod
    def _sanitize_bot_lookalike_phrases(prompt: str) -> str:
        """Rewrite selfie-trigger words so selfie_image stays on the draw path."""
        text = str(prompt or "")
        for phrase, replacement in _LOOKALIKE_REWRITES_ZH:
            text = text.replace(phrase, replacement)
        lowered = text.lower()
        for phrase, replacement in _LOOKALIKE_REWRITES_EN:
            if phrase in lowered:
                pattern = re.compile(re.escape(phrase), re.IGNORECASE)
                text = pattern.sub(replacement, text)
                lowered = text.lower()
        return text

    @staticmethod
    def _compose_avatar_prompt(prompt: str, identity: str, *, has_source: bool) -> str:
        labels = {
            "bot": "机器人自己的 QQ 头像",
            "sender": "发送者的 QQ 头像",
            "at": "被@用户的 QQ 头像",
        }
        identity_label = labels.get(identity, "目标用户的 QQ 头像")
        if has_source:
            relationship = (
                f"{prompt}\n\n"
                f"参考图关系：临时追加的参考图中包含{identity_label}，请将其中的人物身份/脸部特征"
                "作为需要保留的身份参考；当前消息或引用消息中的图片是用户提供的原图，按上面的要求进行图生图。"
                "不要把本地文件路径或参考图编号写进结果。"
            )
        else:
            relationship = (
                f"{prompt}\n\n"
                f"身份参考：使用临时追加的{identity_label}作为人物身份参考，生成一张完整图片。"
                "保持人物身份特征清晰，不要输出本地文件路径。"
            )
        if identity != "bot":
            relationship += (
                "\n\n重要：本次画面的主角不是机器人/AI 助手本人。"
                "必须以上述 QQ 头像参考图中的人物为唯一身份来源，"
                "忽略任何内置的 AI 自身形象参考图，禁止用机器人形象替换或融合主角长相。"
            )
        return relationship

    @contextmanager
    def _inject_reference_images(
        self,
        event: Any,
        paths: list[Path],
        *,
        max_refs: int,
    ) -> Iterator[None]:
        message_obj = getattr(event, "message_obj", None)
        message_list = getattr(message_obj, "message", None)
        if not isinstance(message_list, list):
            message_list = getattr(event, "message", None)
        if not isinstance(message_list, list):
            raise ToolBridgeError("当前事件没有可注入的消息链。")

        try:
            import astrbot.api.message_components as Comp
        except ImportError as exc:  # pragma: no cover - outside AstrBot
            raise ToolBridgeError("无法导入 AstrBot 图片消息组件。") from exc

        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved in seen or not Path(resolved).is_file():
                continue
            seen.add(resolved)
            unique_paths.append(Path(resolved))
            if len(unique_paths) >= max_refs:
                break
        injected: list[Any] = []
        try:
            for path in unique_paths:
                component = Comp.Image.fromFileSystem(str(path))
                message_list.append(component)
                injected.append(component)
            yield
        finally:
            # Remove by object identity, preserving any changes made by the target tool.
            for component in injected:
                for index in range(len(message_list) - 1, -1, -1):
                    if message_list[index] is component:
                        message_list.pop(index)
                        break
