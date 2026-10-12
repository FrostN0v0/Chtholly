"""Attribution, capability privacy and truncation contracts for preloaded channel context."""

import json

from plugins.llm_chat.core.channel_context import MAX_CHANNEL_CONTEXT_CHARS, build_channel_context


def _message(cursor: str, text: str, *, reply: str = "", is_bot: bool = False) -> dict[str, object]:
    return {
        "cursor": cursor,
        "participant_ref": "bot" if is_bot else "participant_alice",
        "display_name": "bot" if is_bot else "Alice",
        "content": text,
        "image_count": 1,
        "created_at": "2026-10-12T01:00:00Z",
        "minutes_ago": 1,
        "is_bot": is_bot,
        "directed_to_bot": not is_bot,
        "mentions": None,
        "reply_to_cursor": reply,
        "reply_to_status": "available" if reply else "none",
        "protocol_url": "https://private.invalid/image",
        "message_id": "raw-platform-message-id",
    }


def test_channel_context_preserves_speakers_unknown_mentions_and_relative_replies_without_capabilities() -> None:
    snapshot = build_channel_context(
        [
            _message("private-1", "Alice's own words"),
            _message("private-2", "Bot's own words", reply="private-1", is_bot=True),
        ],
        requested_messages=2,
    )
    first, second = snapshot.model_payload["messages"]
    assert (first["participant_ref"], first["is_bot"], first["mentions"]) == ("participant_alice", False, None)
    assert (second["participant_ref"], second["is_bot"], second["reply_to_index"]) == ("bot", True, 1)
    encoded = json.dumps(snapshot.model_payload)
    for private in ("private-1", "private-2", "raw-platform-message-id", "private.invalid", "image_ref"):
        assert private not in encoded


def test_channel_context_keeps_newest_contiguous_records_and_marks_a_reply_to_dropped_content() -> None:
    messages = [
        _message("1", "Old dropped words " + "a" * 4400),
        _message("2", "Recent words " + "b" * 4400),
        _message("3", "Newest words " + "c" * 4400, reply="1"),
    ]
    snapshot = build_channel_context(messages, requested_messages=3)
    selected = snapshot.model_payload["messages"]
    assert [message["content"].split(" ")[0] for message in selected] == ["Recent", "Newest"]
    assert selected[-1]["reply_to_status"] == "outside_context"
    assert "reply_to_index" not in selected[-1]
    assert snapshot.model_payload["truncated"] is True
    assert (
        len(json.dumps(snapshot.model_payload, ensure_ascii=False, separators=(",", ":"))) <= MAX_CHANNEL_CONTEXT_CHARS
    )
