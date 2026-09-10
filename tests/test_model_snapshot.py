"""Defend turn-local model stability across retries and concurrent participants."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from utils.llm_model_core.snapshot import pin_main_model, main_model_scope, current_main_model


@dataclass
class Model:
    name: str
    alias: str | None = None


@pytest.mark.asyncio
async def test_parallel_turns_keep_distinct_models_and_release_their_scopes():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_turn():
        with main_model_scope():
            pin_main_model(Model("old", "chat"))
            entered.set()
            await release.wait()
            retry = current_main_model("chat")
            assert retry is not None
            assert retry.name == "old"
            retry.name = "caller-mutated-copy"
            finalizer = current_main_model(None)
            assert finalizer is not None
            assert finalizer.name == "old"
            assert current_main_model("unrelated-model") is None
        assert current_main_model(None) is None

    old = asyncio.create_task(old_turn())
    await entered.wait()
    try:
        with main_model_scope():
            pin_main_model(Model("new", "chat"))
            selected = current_main_model("chat")
            assert selected is not None
            assert selected.name == "new"
            release.set()
            await old
            selected = current_main_model(None)
            assert selected is not None
            assert selected.name == "new"
    finally:
        release.set()
        await old
    assert current_main_model(None) is None


@pytest.mark.asyncio
async def test_cancelled_turn_cannot_leave_a_model_in_its_calling_context():
    entered = asyncio.Event()

    async def turn():
        try:
            with main_model_scope():
                pin_main_model(Model("cancelled"))
                entered.set()
                await asyncio.Future()
        finally:
            assert current_main_model(None) is None

    task = asyncio.create_task(turn())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_main_model(None) is None
    pin_main_model(Model("outside-turn"))
    assert current_main_model(None) is None
