from __future__ import annotations

from dataclasses import dataclass

from astrbot_plugin_auto_tool_all.event_images import (
    extract_at_ids,
    extract_image_sources,
    has_image,
)


@dataclass
class Image:
    url: str = ""
    file: str = ""
    path: str = ""


@dataclass
class At:
    qq: str


@dataclass
class Reply:
    chain: list


@dataclass
class MessageObject:
    message: list
    self_id: str = "12345678"


@dataclass
class Event:
    message_obj: MessageObject
    message_str: str = ""
    raw_message: object = None

    def get_messages(self):
        return self.message_obj.message

    def get_sender_id(self):
        return "87654321"


def test_extracts_direct_and_quoted_images(tmp_path):
    local = tmp_path / "quoted.png"
    local.write_bytes(b"png")
    event = Event(
        MessageObject(
            [
                Image(url="https://example.com/direct.png"),
                Reply([Image(path=str(local))]),
            ]
        )
    )

    items = extract_image_sources(event)

    assert ("https://example.com/direct.png", "message") in {
        (item.source, item.role) for item in items
    }
    assert (str(local), "quote") in {(item.source, item.role) for item in items}
    assert has_image(event)


def test_extracts_at_ids_from_components_and_raw_message():
    event = Event(
        MessageObject([At("11112222"), At("all")]),
        raw_message={
            "message": [
                {"type": "at", "data": {"qq": "33334444"}},
            ]
        },
    )

    assert extract_at_ids(event) == ["11112222", "33334444"]
