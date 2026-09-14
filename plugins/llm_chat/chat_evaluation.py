"""Managed member-local evaluation of every completed interaction exactly once."""

from __future__ import annotations

import math
import asyncio
from weakref import WeakKeyDictionary
from collections.abc import Callable

from .config import LLMChatConfig
from .core.errors import summarize_exception
from .persona.runner import run_evaluation
from .relationships.types import EvaluationBatch
from .persona.memory_update import prepare_memory_updates
from .persona.memory_context import load_evaluator_profile_facts
from .relationships.evidence import enqueue_relationship_evidence
from .relationships.migration import pending_relationship_owners
from .relationships.evaluation_store import claim_evaluation, commit_evaluation, release_evaluation

WarningSink = Callable[[str], object]
OwnerKey = tuple[str, str]
_PENDING_EVALUATIONS: set[asyncio.Task[None]] = set()
_WORKERS: dict[OwnerKey, asyncio.Task[None]] = {}
_DIRTY_OWNERS: set[OwnerKey] = set()
_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
_STOPPING = False


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(4)
        _GATES[loop] = gate
    return gate


def _track(task: asyncio.Task[None]) -> None:
    _PENDING_EVALUATIONS.add(task)
    task.add_done_callback(_PENDING_EVALUATIONS.discard)


async def cancel_pending_evaluations() -> None:
    """Wait for cancelled workers and their durable claim compensation."""
    global _STOPPING
    _STOPPING = True
    _DIRTY_OWNERS.clear()
    tasks = tuple(task for task in _PENDING_EVALUATIONS if task is not asyncio.current_task())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    remaining = tuple(task for task in _PENDING_EVALUATIONS if task is not asyncio.current_task())
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)


async def _restore_cancelled(batch: EvaluationBatch) -> None:
    restore = asyncio.create_task(release_evaluation(batch, "Evaluation cancelled before commit", cancelled=True))
    _track(restore)
    try:
        await asyncio.shield(restore)
    except asyncio.CancelledError:
        await restore


def _start_worker(config: LLMChatConfig, key: OwnerKey, warn: WarningSink) -> None:
    if _STOPPING:
        return
    _DIRTY_OWNERS.add(key)
    existing = _WORKERS.get(key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_evaluate_pending(config, key, warn), name="llm-chat-relationship-evaluator")
    _WORKERS[key] = task
    _track(task)


async def _evaluate_pending(config: LLMChatConfig, key: OwnerKey, warn: WarningSink) -> None:
    batch: EvaluationBatch | None = None
    try:
        delay = float(config.relationship_eval_debounce_seconds)
        await asyncio.sleep(max(0.0, min(10.0, delay)) if math.isfinite(delay) else 2.0)
        while not _STOPPING:
            _DIRTY_OWNERS.discard(key)
            claim = asyncio.create_task(
                claim_evaluation(
                    *key,
                    batch_size=config.relationship_eval_batch_size,
                    model=config.eval_model or config.model or "default",
                )
            )
            try:
                batch = await asyncio.shield(claim)
            except asyncio.CancelledError:
                batch = await claim
                if batch is not None:
                    await _restore_cancelled(batch)
                    batch = None
                raise
            if batch is None:
                if key in _DIRTY_OWNERS:
                    continue
                return
            try:
                async with _gate():
                    facts = await load_evaluator_profile_facts(config, batch.user_id, batch.channel_id)
                    result = await run_evaluation(
                        config,
                        batch.persona_prompt,
                        batch.before,
                        facts,
                        batch.episodes,
                        batch.channel_id,
                    )
                    if result is None:
                        raise ValueError("Evaluator returned invalid or incomplete relationship evidence")
                    memory = await prepare_memory_updates(config, batch.user_id, batch.channel_id, result)
                await commit_evaluation(batch, result, memory=memory)
                batch = None
            except asyncio.CancelledError:
                await _restore_cancelled(batch)
                batch = None
                raise
            except Exception as exc:
                error = summarize_exception(exc)
                await release_evaluation(batch, error)
                batch = None
                warn(f"relationship evaluation failed: {error}")
                return
    except asyncio.CancelledError:
        if batch is not None:
            await _restore_cancelled(batch)
        raise
    except Exception as exc:
        warn(f"relationship evidence processing failed: {summarize_exception(exc)}")
    finally:
        if _WORKERS.get(key) is asyncio.current_task():
            _WORKERS.pop(key, None)
            if key in _DIRTY_OWNERS and not _STOPPING:
                _start_worker(config, key, warn)


def schedule_relationship_evaluation(
    config: LLMChatConfig,
    *,
    turn_id: int,
    user_id: str,
    channel_id: str,
    persona_prompt: str,
    warn: WarningSink,
) -> None:
    """Queue an immutable terminal interaction without blocking the delivered reply."""
    if _STOPPING:
        return

    async def enqueue() -> None:
        try:
            persistence = asyncio.create_task(
                enqueue_relationship_evidence(
                    turn_id=turn_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    persona_prompt=persona_prompt,
                    model=config.eval_model or config.model or "default",
                )
            )
            try:
                await asyncio.shield(persistence)
            except asyncio.CancelledError:
                await persistence
                raise
            _start_worker(config, (user_id, channel_id), warn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"relationship evidence enqueue failed: {summarize_exception(exc)}")

    task = asyncio.create_task(enqueue(), name="llm-chat-relationship-evidence")
    _track(task)


async def resume_relationship_evaluations(config: LLMChatConfig, warn: WarningSink) -> None:
    """Resume durable pending evidence after a full process restart."""
    global _STOPPING
    _STOPPING = False
    for key in await pending_relationship_owners():
        _start_worker(config, key, warn)
