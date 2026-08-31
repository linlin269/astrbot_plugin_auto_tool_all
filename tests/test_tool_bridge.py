from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_auto_tool_all.tool_bridge import ToolBridge, ToolBridgeError


class FakeManager:
    def __init__(self, tools):
        self.func_list = tools

    def get_full_tool_set(self):
        return FakeToolSet(self.func_list)


class FakeToolSet:
    def __init__(self, tools):
        self.tools = list(tools)

    def get_tool(self, name):
        for tool in reversed(self.tools):
            if tool.name == name:
                return tool
        return None

    def add_tool(self, tool):
        self.tools.append(tool)


class FakeContext:
    def __init__(self, tools):
        self.manager = FakeManager(tools)
        self.calls = []

    def get_llm_tool_manager(self):
        return self.manager

    async def get_current_chat_provider_id(self, umo):
        self.calls.append(("provider", umo))
        return "provider-1"

    async def tool_loop_agent(self, **kwargs):
        self.calls.append(("loop", kwargs))
        return SimpleNamespace(completion_text="工具执行成功")


@pytest.fixture
def tools():
    return [
        SimpleNamespace(
            name="future_tool",
            description="未来插件工具",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            active=True,
            handler_module_path="future_plugin.main",
        ),
        SimpleNamespace(
            name="disabled_tool",
            description="停用工具",
            parameters={},
            active=False,
        ),
        SimpleNamespace(
            name="future_tool",
            description="后加载版本",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            active=True,
        ),
    ]


def test_list_tools_is_dynamic_and_filters_inactive(tools):
    bridge = ToolBridge(FakeContext(tools), {})
    result = bridge.list_tools()

    assert [item.name for item in result] == ["future_tool"]
    assert result[0].description == "后加载版本"


def test_argument_parsing_and_schema_filtering(tools):
    bridge = ToolBridge(FakeContext(tools), {})
    tool = bridge.get_target_tool("future_tool")

    assert bridge.validate_arguments(tool, {"query": "hello", "ignored": 1}) == {
        "query": "hello"
    }
    with pytest.raises(ToolBridgeError):
        bridge.parse_json_arguments("[]")
    with pytest.raises(ToolBridgeError):
        bridge.get_target_tool("disabled_tool")


def test_invoke_uses_current_provider_and_single_tool_set(monkeypatch, tools):
    async def scenario():
        context = FakeContext(tools)
        bridge = ToolBridge(context, {})
        fake_set = FakeToolSet([])
        monkeypatch.setattr(bridge, "_single_tool_set", lambda tool: fake_set)
        event = SimpleNamespace(unified_msg_origin="aiocqhttp:group:1")

        result = await bridge.invoke(
            event, "future_tool", {"query": "hello", "ignored": 1}
        )

        assert result == "工具执行成功"
        assert context.calls[0] == ("provider", "aiocqhttp:group:1")
        assert context.calls[1][0] == "loop"
        assert context.calls[1][1]["chat_provider_id"] == "provider-1"
        assert context.calls[1][1]["tools"] is fake_set
        assert "hello" in context.calls[1][1]["prompt"]

    asyncio.run(scenario())
