"""Dynamic discovery and safe invocation of AstrBot global LLM tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    parameters: dict[str, Any]
    active: bool
    module: str


class ToolBridgeError(RuntimeError):
    """Raised for user-correctable tool bridge errors."""


class ToolBridge:
    """Keep global tool discovery independent from particular plugins."""

    INTERNAL_TOOLS: ClassVar[frozenset[str]] = frozenset(
        {
            "list_available_tools",
            "call_plugin_tool",
            "fetch_media",
            "avatar_draw",
            "anysearch_search",
            "anysearch_batch_search",
            "anysearch_extract",
            "anysearch_site_search",
            "web_search",
        }
    )

    def __init__(
        self, context: Any, config: Any | None = None, logger: Any | None = None
    ) -> None:
        self.context = context
        self.config = config or {}
        self.logger = logger

    def manager(self) -> Any:
        getter = getattr(self.context, "get_llm_tool_manager", None)
        if not callable(getter):
            raise ToolBridgeError("当前 AstrBot 版本没有可用的全局 LLM 工具管理器。")
        return getter()

    def _configured_names(self, key: str) -> set[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",")]
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    def is_allowed(self, name: str, *, include_internal: bool = False) -> bool:
        if not include_internal and name in self.INTERNAL_TOOLS:
            return False
        blocked = self._configured_names("blocked_tool_names") | self.INTERNAL_TOOLS
        allowed = self._configured_names("allowed_tool_names")
        if name in blocked:
            return False
        return not allowed or name in allowed

    def _raw_tools(self) -> list[Any]:
        manager = self.manager()
        tools = getattr(manager, "func_list", None)
        return list(tools) if isinstance(tools, list) else []

    def _active_raw_by_name(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for tool in self._raw_tools():
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or not getattr(tool, "active", True):
                continue
            # Later registrations follow AstrBot's overwrite convention.
            result[name] = tool
        return result

    def list_tools(self, *, include_internal: bool = False) -> list[ToolInfo]:
        result: list[ToolInfo] = []
        for tool in self._raw_tools():
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or not getattr(tool, "active", True):
                continue
            if not self.is_allowed(name, include_internal=include_internal):
                continue
            parameters = getattr(tool, "parameters", {})
            result.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or "").strip(),
                    parameters=parameters if isinstance(parameters, dict) else {},
                    active=True,
                    module=str(getattr(tool, "handler_module_path", "") or ""),
                )
            )
        # Match manager's effective last-registration-wins behavior.
        deduped: dict[str, ToolInfo] = {}
        for item in result:
            deduped[item.name] = item
        return sorted(deduped.values(), key=lambda item: item.name)

    def get_target_tool(self, name: str, *, include_internal: bool = False) -> Any:
        tool_name = str(name or "").strip()
        if not tool_name:
            raise ToolBridgeError("工具名不能为空。")
        if not self.is_allowed(tool_name, include_internal=include_internal):
            raise ToolBridgeError(f"工具 `{tool_name}` 未被允许调用。")
        raw = self._active_raw_by_name().get(tool_name)
        if raw is None:
            raise ToolBridgeError(
                f"找不到 active 工具 `{tool_name}`。请先确认插件已加载。"
            )

        # get_full_tool_set adds AstrBot's permission guard and MCP adapters.
        manager = self.manager()
        get_full = getattr(manager, "get_full_tool_set", None)
        if callable(get_full):
            full_set = get_full()
            wrapped = full_set.get_tool(tool_name)
            if wrapped is not None:
                return wrapped
        return raw

    @staticmethod
    def _schema_properties(tool: Any) -> tuple[dict[str, Any], set[str]]:
        schema = getattr(tool, "parameters", {})
        if not isinstance(schema, dict):
            return {}, set()
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        required = schema.get("required", [])
        required_set = (
            {str(item) for item in required} if isinstance(required, list) else set()
        )
        return properties, required_set

    def validate_arguments(
        self, tool: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ToolBridgeError("工具参数必须是 JSON 对象。")
        properties, required = self._schema_properties(tool)
        missing = sorted(required - set(arguments))
        if missing:
            raise ToolBridgeError(f"缺少工具必填参数：{', '.join(missing)}")
        if not properties:
            return arguments
        # The official runner drops unknown fields before executing local tools.
        return {key: value for key, value in arguments.items() if key in properties}

    async def invoke(
        self,
        event: Any,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        prompt_prefix: str = "",
        max_steps: int | None = None,
        timeout: int | None = None,
    ) -> str:
        text, _ = await self.invoke_result(
            event,
            tool_name,
            arguments,
            prompt_prefix=prompt_prefix,
            max_steps=max_steps,
            timeout=timeout,
        )
        return text

    async def invoke_result(
        self,
        event: Any,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        prompt_prefix: str = "",
        max_steps: int | None = None,
        timeout: int | None = None,
    ) -> tuple[str, Any]:
        """Run the tool and return (text, raw response) so callers can recover
        media components the text summary would otherwise drop."""
        tool = self.get_target_tool(tool_name)
        safe_args = self.validate_arguments(tool, arguments)
        tool_set = self._single_tool_set(tool)
        provider_getter = getattr(self.context, "get_current_chat_provider_id", None)
        if not callable(provider_getter):
            raise ToolBridgeError("当前 AstrBot 版本无法获取聊天 Provider。")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            raise ToolBridgeError("当前事件没有 unified_msg_origin，无法调用工具。")
        provider_id = await provider_getter(umo)
        if not provider_id:
            raise ToolBridgeError("当前会话没有可用的聊天 Provider。")

        try:
            steps = int(
                max_steps
                if max_steps is not None
                else self.config.get("tool_loop_max_steps", 3)
            )
        except (TypeError, ValueError):
            steps = 3
        try:
            call_timeout = int(
                timeout
                if timeout is not None
                else self.config.get("tool_call_timeout_seconds", 120)
            )
        except (TypeError, ValueError):
            call_timeout = 120
        steps = max(1, min(steps, 10))
        call_timeout = max(10, min(call_timeout, 600))

        args_json = json.dumps(safe_args, ensure_ascii=False)
        prefix = str(prompt_prefix or "").strip()
        prompt = (f"{prefix}\n" if prefix else "") + (
            f"请只调用一次工具 `{tool_name}`，不要调用任何其它工具。"
            f"调用参数必须严格使用以下 JSON：{args_json}。"
            "工具执行完成后，用一句话说明执行结果；不要臆造工具没有返回的内容。"
            "如果工具返回中包含图片或视频链接（http/https 直链），"
            "必须原样完整保留这些链接，不要省略或改写。"
        )
        loop = getattr(self.context, "tool_loop_agent", None)
        if not callable(loop):
            raise ToolBridgeError("当前 AstrBot 版本没有 tool_loop_agent。")
        response = await loop(
            event=event,
            chat_provider_id=provider_id,
            prompt=prompt,
            tools=tool_set,
            max_steps=steps,
            tool_call_timeout=call_timeout,
        )
        return self.response_text(response) or f"工具 `{tool_name}` 已执行。", response

    @staticmethod
    def _single_tool_set(tool: Any) -> Any:
        try:
            from astrbot.core.agent.tool import ToolSet
        except ImportError as exc:  # pragma: no cover - only outside AstrBot
            raise ToolBridgeError("无法导入 AstrBot ToolSet。") from exc
        result = ToolSet()
        result.add_tool(tool)
        return result

    @staticmethod
    def response_text(response: Any) -> str:
        """Extract a concise textual result across AstrBot provider response versions."""
        for attr in ("completion_text", "text", "content", "result"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        chain = getattr(response, "result_chain", None) or getattr(
            response, "chain", None
        )
        if isinstance(chain, (list, tuple)):
            parts: list[str] = []
            for item in chain:
                text = getattr(item, "text", None)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts)
        return ""

    @staticmethod
    def parse_json_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(str(raw or "{}"))
        except json.JSONDecodeError as exc:
            raise ToolBridgeError("arguments 必须是合法 JSON 对象。") from exc
        if not isinstance(value, dict):
            raise ToolBridgeError("arguments 必须解析为 JSON 对象。")
        return value

    @staticmethod
    def extract_media_urls(response: Any) -> list[str]:
        """Recover http(s) media URLs carried by result-chain components."""
        urls: list[str] = []
        chain = getattr(response, "result_chain", None) or getattr(
            response, "chain", None
        )
        items: list[Any] = []
        inner = getattr(chain, "chain", None)
        if isinstance(inner, list):
            items = inner
        elif isinstance(chain, (list, tuple)):
            items = list(chain)
        for item in items:
            if type(item).__name__ not in ("Image", "Video"):
                continue
            for attr in ("url", "file"):
                value = str(getattr(item, attr, "") or "").strip()
                if value.startswith(("http://", "https://")) and value not in urls:
                    urls.append(value)
        return urls
