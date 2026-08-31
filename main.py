"""AstrBot plugin: dynamically orchestrate global tools with QQ-avatar references."""

from __future__ import annotations

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
from .tool_bridge import ToolBridge, ToolBridgeError

PLUGIN_NAME = "astrbot_plugin_auto_tool_all"
_INTERNAL_TOOL_NAMES = ToolBridge.INTERNAL_TOOLS

_CLEAR_USAGE = (
    "清空上下文指令用法：\n"
    "/清空上文 —— 立即清空当前会话上下文\n"
    "/清空上文 all —— 立即清空所有会话上下文\n"
    "/清空上文 6s|6秒|5min|5分钟|1h|1小时 —— 定时清空当前会话上下文\n"
    "/查看清空上文定时任务 —— 查看当前定时任务\n"
    "/取消清空上文定时任务 序号|qq号|群号|all —— 取消定时任务"
)


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
        优先直接调用工具；只有需要显式指定工具名，或主模型无法直接选择时才使用本入口。
        本工具不能调用自身的内部工具，也不能调用只有 /指令 而未注册为 LLM 工具的插件。
        Args:
            tool_name(string): 要调用的外部工具名称，例如 anysearch_batch_search、search_magnet、preview_magnet 或未来插件注册的工具名。
            arguments(string): JSON 对象形式的工具参数，例如 {"queries":"[\\"AstrBot\\"]"}。
        """
        try:
            parsed = self.tool_bridge.parse_json_arguments(arguments)
            result = await self.tool_bridge.invoke(
                event,
                tool_name,
                parsed,
                prompt_prefix=(
                    "你是 AstrBot 的工具桥。这个请求来自用户，必须只执行指定工具一次，"
                    "不要改调用目标，也不要把工具返回内容编造成不存在的事实。"
                ),
            )
            return result
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
        用户说“你/机器人”时 identity 传 bot；说“我/我的”时传 sender；
        说“他/她/TA”或消息中明确 @ 某人时传 at。
        当前消息直接附带图片、回复/引用带图片的消息，都会自动作为图生图输入。
        只有已注册为 LLM 工具的生图能力可被调用；普通 /指令 不在本工具范围内。
        Args:
            prompt(string): 用户想要的画面或修改要求，保留动作、场景、风格，以及头像人物和原图之间的关系。
            identity(string): 头像归属，只能使用 bot（机器人）、sender（发送者）、at（被@用户）。
            count(number): 生成张数，默认 1。
            reference_image_urls(array[string]): 上一步搜索或其它工具返回的外部图片 URL，可为空数组。
            ack_message(string): 可选的中文进度短句，10 到 40 字。
        """
        if not self._is_aiocqhttp(event):
            return "头像功能目前只支持 aiocqhttp/OneBot v11 QQ 事件。"

        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return "缺少绘图要求，请说明想画什么或怎样修改图片。"

        normalized_identity = self.avatar_service.normalize_identity(identity)
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
            target = await self.avatar_service.resolve_for_event(
                event, normalized_identity
            )
            max_refs = self._int_config("max_reference_images", 4, minimum=1, maximum=8)
            external_paths = await self.avatar_service.download_external_images(
                external_urls,
                max_count=max(0, max_refs - 1),
            )
            image_tool_name = self._choose_image_tool()
            if not image_tool_name:
                return (
                    "没有找到可用的生图 LLM 工具。请确认 astrbot_plugin_selfie_image 已加载，"
                    "并在其 Web 面板开启 image_enable_llm_tool；也可以在本插件配置中指定其它生图工具名。"
                )

            composed_prompt = self._compose_avatar_prompt(
                prompt_text,
                normalized_identity,
                has_source=bool(source_images or external_paths),
            )
            args = self._image_tool_arguments(
                image_tool_name,
                composed_prompt,
                count=count,
                ack_message=ack_message,
                reference_paths=[target.path, *external_paths],
                event_sources=[item.source for item in source_images],
            )
            with self._inject_reference_images(
                event, [target.path, *external_paths], max_refs=max_refs
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

    def _int_config(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (AttributeError, TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _choose_image_tool(self) -> str:
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
    def _compose_avatar_prompt(prompt: str, identity: str, *, has_source: bool) -> str:
        labels = {
            "bot": "机器人自己的 QQ 头像",
            "sender": "发送者的 QQ 头像",
            "at": "被@用户的 QQ 头像",
        }
        identity_label = labels.get(identity, "目标用户的 QQ 头像")
        if has_source:
            return (
                f"{prompt}\n\n"
                f"参考图关系：临时追加的参考图中包含{identity_label}，请将其中的人物身份/脸部特征"
                "作为需要保留的身份参考；当前消息或引用消息中的图片是用户提供的原图，按上面的要求进行图生图。"
                "不要把本地文件路径或参考图编号写进结果。"
            )
        return (
            f"{prompt}\n\n"
            f"身份参考：使用临时追加的{identity_label}作为人物身份参考，生成一张完整图片。"
            "保持人物身份特征清晰，不要输出本地文件路径。"
        )

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
