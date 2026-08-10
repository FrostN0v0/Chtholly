"""Generation-local Entari event context for Agno tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from arclet.letoderea import Contexts

_CURRENT_CONTEXT = ContextVar[Contexts | None]("llm_chat_context", default=None)


@contextmanager
def llm_chat_context_scope(context: Contexts | None) -> Iterator[None]:
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


def copy_llm_chat_context() -> Contexts | None:
    context = _CURRENT_CONTEXT.get()
    return Contexts(context.copy()) if context is not None else None
