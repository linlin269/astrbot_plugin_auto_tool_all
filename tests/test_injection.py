from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _install_astrbot_import_stubs():
    # Do not shadow a real AstrBot installation when tests run inside its repo.
    if "astrbot" in sys.modules:
        return
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")

    class Filter:
        PermissionType = SimpleNamespace(ADMIN="admin", MEMBER="member")

        @staticmethod
        def llm_tool(name=None):
            return lambda function: function

        @staticmethod
        def command(name=None, alias=None, priority=0):
            return lambda function: function

        @staticmethod
        def permission_type(permission_type, raise_error=False):
            return lambda function: function

        @staticmethod
        def on_astrbot_loaded(**kwargs):
            return lambda function: function

    class Star:
        def __init__(self, context):
            self.context = context

    api.AstrBotConfig = dict
    api.logger = SimpleNamespace()
    event.AstrMessageEvent = object
    event.filter = Filter()
    star.Context = object
    star.Star = Star
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


_install_astrbot_import_stubs()
from astrbot_plugin_auto_tool_all.main import AutoToolAll


def test_reference_injection_is_restored_on_failure(tmp_path, monkeypatch):
    class FakeImage:
        @staticmethod
        def fromFileSystem(path):
            return SimpleNamespace(path=path)

    fake_components = ModuleType("astrbot.api.message_components")
    fake_components.Image = FakeImage
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", fake_components)

    original = ["original"]
    message_obj = SimpleNamespace(message=original)
    event = SimpleNamespace(message_obj=message_obj)
    reference = tmp_path / "avatar.jpg"
    reference.write_bytes(b"avatar")

    plugin = object.__new__(AutoToolAll)
    try:
        with plugin._inject_reference_images(event, [Path(reference)], max_refs=2):
            assert len(original) == 2
            raise RuntimeError("target failed")
    except RuntimeError:
        pass

    assert original == ["original"]
