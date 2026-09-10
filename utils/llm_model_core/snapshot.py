"""Keep one main-model selection stable across a chat turn's generation attempts."""

from __future__ import annotations

from copy import copy
from typing import TypeVar, Protocol
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


class NamedModel(Protocol):
    name: str


_Model = TypeVar("_Model", bound=NamedModel)


@dataclass
class _TurnModel:
    value: NamedModel | None = None


_CURRENT: ContextVar[_TurnModel | None] = ContextVar("llm_main_model_snapshot", default=None)


@contextmanager
def main_model_scope() -> Iterator[None]:
    token = _CURRENT.set(_TurnModel())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def pin_main_model(model: _Model) -> _Model:
    state = _CURRENT.get()
    if state is not None:
        state.value = copy(model)
    return model


def current_main_model(name: str | None) -> NamedModel | None:
    state = _CURRENT.get()
    if state is None or state.value is None:
        return None
    model = state.value
    if name is not None and name not in (model.name, getattr(model, "alias", None)):
        return None
    # The upstream resolver returns a fresh model too. Preserve that contract;
    # model hot updates replace, rather than mutate, nested request parameters.
    return copy(model)
