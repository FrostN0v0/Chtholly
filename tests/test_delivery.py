"""Behavioral contract tests for generation-local llm_chat delivery."""

from __future__ import annotations

import math
import asyncio
from dataclasses import field, dataclass

import pytest

from plugins.llm_chat.core.delivery import (
    DEFAULT_DELIVERY_LIMITS,
    DeliveryError,
    DeliveryState,
    DeliveryLimits,
    wait_for_delivery,
    reserve_final_text,
    reserve_text_message,
    mark_delivery_attempt,
    mark_delivery_success,
    render_delivered_text,
    reserve_media_message,
    reserve_media_messages,
    llm_chat_delivery_scope,
    normalize_delivery_delay,
    reserve_forward_messages,
    current_llm_chat_delivery,
    normalize_delivery_limits,
    require_llm_chat_delivery,
    reserve_final_text_messages,
)
from plugins.llm_chat.core.media_delivery import (
    MEDIA_UNAVAILABLE_MARKER,
    is_media_unavailable_reply,
    latest_user_requests_media,
    strip_media_unavailable_marker,
)


@pytest.mark.parametrize(
    "content",
    [
        "来张图我看看什么样子",
        '{"speaker":"FrostN0v0","content":"你发的图呢？"}',
        "send me a picture",
    ],
)
def test_latest_user_media_request_detection_handles_chat_payloads(content: str) -> None:
    assert latest_user_requests_media([{"role": "user", "content": content}])


@pytest.mark.parametrize(
    "content",
    [
        "不要发图，只用文字描述",
        "这张图里面是什么",
        '{"speaker":"FrostN0v0","content":"普通聊天"}',
    ],
)
def test_latest_user_media_request_detection_rejects_non_delivery_intent(content: str) -> None:
    assert not latest_user_requests_media([{"role": "user", "content": content}])


def test_media_unavailable_marker_requires_visible_text_and_never_reaches_delivery() -> None:
    marked = f"{MEDIA_UNAVAILABLE_MARKER} 这轮没有确认发出图片。"

    assert is_media_unavailable_reply(marked)
    assert not is_media_unavailable_reply(MEDIA_UNAVAILABLE_MARKER)
    assert strip_media_unavailable_marker(marked) == "这轮没有确认发出图片。"


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _state_snapshot(state: DeliveryState) -> tuple[object, ...]:
    return (
        state.mode,
        state.text_messages,
        state.forward_calls,
        state.media_messages,
        state.text_chars,
        state.last_delivery_at,
        state.delivery_attempts,
        state.confirmed_deliveries,
        state.confirmed_media_deliveries,
        tuple(state.delivered_texts),
    )


def _limits(**overrides: float) -> DeliveryLimits:
    values: dict[str, int | float] = {
        "min_interval_seconds": DEFAULT_DELIVERY_LIMITS.min_interval_seconds,
        "default_interval_seconds": DEFAULT_DELIVERY_LIMITS.default_interval_seconds,
        "max_interval_seconds": DEFAULT_DELIVERY_LIMITS.max_interval_seconds,
        "max_text_messages": DEFAULT_DELIVERY_LIMITS.max_text_messages,
        "max_text_chars_per_message": DEFAULT_DELIVERY_LIMITS.max_text_chars_per_message,
        "max_forward_nodes": DEFAULT_DELIVERY_LIMITS.max_forward_nodes,
        "max_forward_chars_per_node": DEFAULT_DELIVERY_LIMITS.max_forward_chars_per_node,
        "max_total_text_chars": DEFAULT_DELIVERY_LIMITS.max_total_text_chars,
        "max_media_messages": DEFAULT_DELIVERY_LIMITS.max_media_messages,
    }
    values.update(overrides)
    return DeliveryLimits(**values)  # type: ignore[arg-type]


def test_normalize_delivery_limits_orders_intervals_and_enforces_hard_ceilings() -> None:
    ordered = normalize_delivery_limits(4.0, 2.0, 3.0, 5, 1000, 20, 2000, 12000, 2)
    assert (
        ordered.min_interval_seconds,
        ordered.default_interval_seconds,
        ordered.max_interval_seconds,
    ) == (4.0, 4.0, 4.0)

    clamped = normalize_delivery_limits(
        math.nan,
        math.inf,
        -math.inf,
        99,
        99999,
        99,
        99999,
        999999,
        99,
    )
    assert clamped == DEFAULT_DELIVERY_LIMITS

    tightened = normalize_delivery_limits(0.0, 9.0, 99.0, -1, 900, 4, 800, 500, -2)
    assert (
        tightened.min_interval_seconds,
        tightened.default_interval_seconds,
        tightened.max_interval_seconds,
    ) == (1.1, 5.0, 5.0)
    assert tightened.max_text_messages == 0
    assert tightened.max_text_chars_per_message == 500
    assert tightened.max_forward_nodes == 4
    assert tightened.max_forward_chars_per_node == 500
    assert tightened.max_total_text_chars == 500
    assert tightened.max_media_messages == 0


def test_delivery_scope_current_require_and_exception_reset() -> None:
    assert current_llm_chat_delivery() is None
    with pytest.raises(DeliveryError, match="^Delivery tools are unavailable outside llm_chat generation$"):
        require_llm_chat_delivery()

    state = DeliveryState()

    def fail_inside_scope() -> None:
        with llm_chat_delivery_scope(state):
            assert current_llm_chat_delivery() is state
            assert require_llm_chat_delivery() is state
            raise RuntimeError("scope exploded")

    with pytest.raises(RuntimeError, match="scope exploded"):
        fail_inside_scope()

    assert current_llm_chat_delivery() is None
    with pytest.raises(DeliveryError, match="outside llm_chat generation"):
        require_llm_chat_delivery()


@pytest.mark.asyncio
async def test_delivery_scopes_are_isolated_between_concurrent_generations() -> None:
    first = DeliveryState()
    second = DeliveryState()
    ready = asyncio.Event()
    observed: list[DeliveryState] = []

    async def worker(state: DeliveryState, text: str) -> None:
        with llm_chat_delivery_scope(state):
            observed.append(require_llm_chat_delivery())
            if len(observed) == 2:
                ready.set()
            await ready.wait()
            reserved, normalized = reserve_text_message(text)
            mark_delivery_success(reserved, [normalized])
            await asyncio.sleep(0)
            assert current_llm_chat_delivery() is state

    await asyncio.gather(worker(first, "first"), worker(second, "second"))

    assert first.delivered_texts == ["first"]
    assert second.delivered_texts == ["second"]
    assert first.delivery_attempts == first.confirmed_deliveries == 1
    assert second.delivery_attempts == second.confirmed_deliveries == 1
    assert current_llm_chat_delivery() is None


@pytest.mark.asyncio
async def test_delivery_delay_clamps_first_send_and_elapsed_time() -> None:
    clock = FakeClock()
    state = DeliveryState(sleep=clock.sleep, clock=clock.monotonic)

    await wait_for_delivery(state, 0.2)
    assert clock.sleeps == []
    mark_delivery_success(state, ["first"])

    await wait_for_delivery(state, 0.2)
    assert clock.sleeps == [1.1]
    mark_delivery_success(state, ["second"])

    await wait_for_delivery(state, 2.0)
    assert clock.sleeps == [1.1, 2.0]
    mark_delivery_success(state, ["third"])

    clock.advance(0.4)
    await wait_for_delivery(state, None)
    assert clock.sleeps[-1] == pytest.approx(0.8)
    assert state.delivery_attempts == state.confirmed_deliveries == 3


@pytest.mark.asyncio
async def test_delivery_delay_uses_tightened_limits_and_unknown_attempt_time() -> None:
    clock = FakeClock()
    limits = _limits(min_interval_seconds=2.0, default_interval_seconds=2.5, max_interval_seconds=3.0)
    state = DeliveryState(limits=limits, sleep=clock.sleep, clock=clock.monotonic)

    mark_delivery_attempt(state)
    clock.advance(0.75)
    await wait_for_delivery(state, 99.0)
    assert clock.sleeps == [pytest.approx(2.25)]
    assert state.delivery_attempts == 1
    assert state.confirmed_deliveries == 0

    assert normalize_delivery_delay(None) is None
    assert normalize_delivery_delay(math.inf) is None
    assert normalize_delivery_delay(1) == 1.0
    with pytest.raises(DeliveryError, match="^delay_seconds must be a number or null$"):
        normalize_delivery_delay(True)
    with pytest.raises(DeliveryError, match="^delay_seconds must be a number or null$"):
        normalize_delivery_delay("fast")


def test_send_text_budget_mode_and_failed_send_are_not_refunded() -> None:
    state = DeliveryState()
    with llm_chat_delivery_scope(state):
        for index in range(5):
            reserved, text = reserve_text_message(f"segment-{index}")
            assert reserved is state
            mark_delivery_success(state, [text])

        before = _state_snapshot(state)
        with pytest.raises(DeliveryError, match="^send_text budget exhausted; finish with one final reply$"):
            reserve_text_message("sixth")
        assert _state_snapshot(state) == before

    failed_state = DeliveryState(limits=_limits(max_text_messages=1))
    with llm_chat_delivery_scope(failed_state):
        reserve_text_message("reserved but failed")
        mark_delivery_attempt(failed_state)
        with pytest.raises(DeliveryError, match="budget exhausted"):
            reserve_text_message("retry")
    assert failed_state.text_messages == 1
    assert failed_state.delivered_texts == []


def test_text_and_forward_modes_are_mutually_exclusive_without_partial_mutation() -> None:
    segmented = DeliveryState()
    with llm_chat_delivery_scope(segmented):
        reserve_text_message("segment")
        before = _state_snapshot(segmented)
        with pytest.raises(
            DeliveryError,
            match="^Do not mix send_text and send_merged_forward in one generation$",
        ):
            reserve_forward_messages(["node"])
        assert _state_snapshot(segmented) == before

    forwarded = DeliveryState()
    with llm_chat_delivery_scope(forwarded):
        reserve_forward_messages(["node"])
        before = _state_snapshot(forwarded)
        with pytest.raises(
            DeliveryError,
            match="^Do not mix send_text and send_merged_forward in one generation$",
        ):
            reserve_text_message("segment")
        assert _state_snapshot(forwarded) == before


def test_final_supplement_uses_remaining_total_budget_only_after_tool_delivery() -> None:
    limits = _limits(max_text_chars_per_message=4, max_total_text_chars=5)
    state = DeliveryState(limits=limits)
    with llm_chat_delivery_scope(state):
        reserve_text_message("abc")
    assert reserve_final_text(state, "de") == "de"
    assert state.text_chars == 5

    before = _state_snapshot(state)
    with pytest.raises(
        DeliveryError,
        match="^Final supplement exceeds the configured delivery text budget$",
    ):
        reserve_final_text(state, "f")
    assert _state_snapshot(state) == before

    ordinary = DeliveryState(limits=_limits(max_text_chars_per_message=0, max_total_text_chars=0))
    assert (
        reserve_final_text(ordinary, "ordinary final reply remains unlimited")
        == "ordinary final reply remains unlimited"
    )
    assert ordinary.text_chars == 0


def test_multiline_final_text_reserves_independent_messages_without_splitting_structured_content() -> None:
    state = DeliveryState(limits=_limits(max_text_messages=3, max_text_chars_per_message=20, max_total_text_chars=40))

    assert reserve_final_text_messages(state, "first beat\nsecond beat") == ("first beat", "second beat")
    assert state.mode == "segments"
    assert state.text_messages == 2
    assert state.text_chars == 21

    structured = DeliveryState()
    markdown = "Steps:\n- first\n- second"
    assert reserve_final_text_messages(structured, markdown) == (markdown,)
    assert structured.mode is None
    assert structured.text_messages == 0


def test_multiline_final_text_stays_atomic_when_segment_budget_is_insufficient() -> None:
    state = DeliveryState(limits=_limits(max_text_messages=1, max_text_chars_per_message=20, max_total_text_chars=40))

    assert reserve_final_text_messages(state, "first beat\nsecond beat") == ("first beat\nsecond beat",)
    assert state.mode is None
    assert state.text_messages == 0


def test_forward_validation_limits_and_atomic_rejection() -> None:
    state = DeliveryState(limits=_limits(max_forward_nodes=2, max_forward_chars_per_node=4, max_total_text_chars=7))
    invalid_values: tuple[object, ...] = ("abc", b"abc", {"one": "two"}, ["ok", 7])
    with llm_chat_delivery_scope(state):
        for value in invalid_values:
            before = _state_snapshot(state)
            with pytest.raises(DeliveryError, match="^messages must be a list of strings$"):
                reserve_forward_messages(value)
            assert _state_snapshot(state) == before

        before = _state_snapshot(state)
        with pytest.raises(DeliveryError, match="at least one"):
            reserve_forward_messages([])
        assert _state_snapshot(state) == before

        for value, pattern in (
            (["one", "two", "three"], "node limit"),
            (["12345"], r"messages\[0\].*node character limit"),
            (["1234", "5678"], "total character limit"),
        ):
            before = _state_snapshot(state)
            with pytest.raises(DeliveryError, match=pattern):
                reserve_forward_messages(value)
            assert _state_snapshot(state) == before

        reserved, messages = reserve_forward_messages(("one", "two"))
        assert reserved is state
        assert messages == ("one", "two")
        assert state.forward_calls == 1
        assert state.text_chars == 6


def test_media_budget_must_precede_text_and_rejections_do_not_mutate() -> None:
    state = DeliveryState()
    with llm_chat_delivery_scope(state):
        assert reserve_media_messages(2) is state
        assert state.media_messages == 2
        before = _state_snapshot(state)
        with pytest.raises(DeliveryError, match="^Media delivery budget exhausted$"):
            reserve_media_message()
        assert _state_snapshot(state) == before

    invalid_count = DeliveryState()
    with llm_chat_delivery_scope(invalid_count):
        before = _state_snapshot(invalid_count)
        with pytest.raises(DeliveryError, match="^Media delivery count must be a positive integer$"):
            reserve_media_messages(0)
        assert _state_snapshot(invalid_count) == before

    after_text = DeliveryState()
    with llm_chat_delivery_scope(after_text):
        reserve_text_message("text")
        before = _state_snapshot(after_text)
        with pytest.raises(DeliveryError, match="^Media must be sent before text delivery$"):
            reserve_media_messages(2)
        assert _state_snapshot(after_text) == before


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("visible reply\n[END_OF_RESPONSE]", "visible reply", id="newline-suffix"),
        pytest.param("visible reply[END_OF_RESPONSE]", "visible reply", id="attached-suffix"),
        pytest.param(
            "visible reply\n[END_OF_RESPONSE]\n[END_OF_RESPONSE]",
            "visible reply",
            id="repeated-suffix",
        ),
    ],
)
def test_trailing_end_marker_is_removed_from_visible_delivery_text(value: str, expected: str) -> None:
    state = DeliveryState()
    with llm_chat_delivery_scope(state):
        reserved, normalized = reserve_text_message(value)

    assert reserved is state
    assert normalized == expected


def test_media_records_and_internal_sentinel_are_removed_or_rejected_atomically() -> None:
    state = DeliveryState()
    with llm_chat_delivery_scope(state):
        reserved, normalized = reserve_text_message("  before [发送了表情包: hidden] after  ")
        assert normalized == "before  after"
        mark_delivery_success(reserved, [normalized])

        for value in ("[发送了表情包: hidden]", " [END_OF_RESPONSE] "):
            before = _state_snapshot(state)
            with pytest.raises(
                DeliveryError,
                match="^Delivery text is empty or reserved for internal control$",
            ):
                reserve_text_message(value)
            assert _state_snapshot(state) == before

    forward_state = DeliveryState()
    with llm_chat_delivery_scope(forward_state):
        for messages in (
            ["ok", "[发送了语音: hidden]"],
            ["ok", "[END_OF_RESPONSE]"],
        ):
            before = _state_snapshot(forward_state)
            with pytest.raises(
                DeliveryError,
                match="^Delivery text is empty or reserved for internal control$",
            ):
                reserve_forward_messages(messages)
            assert _state_snapshot(forward_state) == before

        reserved, normalized_nodes = reserve_forward_messages(["first [发送了表情包: hidden]", "second"])
        assert normalized_nodes == ("first", "second")
        mark_delivery_success(reserved, normalized_nodes)

    assert state.delivered_texts == ["before  after"]
    assert render_delivered_text(forward_state) == "first\n\nsecond"


def test_non_string_text_reports_only_the_field_name() -> None:
    state = DeliveryState()
    with llm_chat_delivery_scope(state):
        before = _state_snapshot(state)
        with pytest.raises(DeliveryError, match="^text must be a string$") as captured:
            reserve_text_message(7)
        assert "7" not in str(captured.value)
        assert _state_snapshot(state) == before
