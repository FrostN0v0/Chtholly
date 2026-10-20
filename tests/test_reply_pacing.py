"""A waiting member gets a reply before long lookups; replies and delivery tools are never throttled."""

from __future__ import annotations

from plugins.llm_chat.core.reply_pacing import (
    HARD_LOOKUPS,
    HARD_SECONDS,
    SOFT_LOOKUPS,
    SOFT_SECONDS,
    ReplyPacing,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_lookups_pause_until_something_is_sent() -> None:
    pacing = ReplyPacing(clock=_Clock())
    for _ in range(HARD_LOOKUPS):
        assert pacing.admit("web_search", replied=False) is None
    refusal = pacing.admit("read_web_page", replied=False)
    assert refusal is not None
    assert refusal.startswith("Reply first")
    assert pacing.admit("send_msg", replied=False) is None
    assert pacing.admit("prepare_image", replied=False) is None
    assert pacing.admit("web_search", replied=True) is None


def test_elapsed_time_alone_pauses_lookups_and_nudges_once() -> None:
    clock = _Clock()
    pacing = ReplyPacing(clock=clock)
    assert pacing.notice(replied=False) is None
    clock.now += SOFT_SECONDS
    assert pacing.notice(replied=False) is not None
    assert pacing.notice(replied=False) is None
    clock.now += HARD_SECONDS
    assert pacing.admit("web_search", replied=False) is not None
    assert pacing.admit("web_search", replied=True) is None


def test_notice_follows_soft_lookup_count_but_not_after_a_reply() -> None:
    pacing = ReplyPacing(clock=_Clock())
    for _ in range(SOFT_LOOKUPS - 1):
        pacing.admit("inspect_image", replied=False)
    assert pacing.notice(replied=False) is None
    pacing.admit("inspect_image", replied=False)
    assert pacing.notice(replied=True) is None
    assert pacing.notice(replied=False) is not None
