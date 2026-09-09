"""Repair Entari 0.19's variadic internal API instrumentation in local code."""

from __future__ import annotations

from typing import Any
from inspect import signature
from weakref import WeakSet
from functools import wraps
from collections.abc import Callable

from satori.client import get_accounts
from arclet.letoderea import es
from arclet.entari.session import EntariProtocol
from arclet.entari.event.api import APIRequest, APIResponse


def install_entari_internal_bridge() -> Callable[[], None]:
    """Repair existing registered accounts and future/custom protocols until disposal."""

    previous_init = EntariProtocol.__init__
    acquire = getattr(previous_init, "__llm_chat_internal_acquire__", None)
    if acquire is not None:
        return acquire()

    protocols: WeakSet[EntariProtocol] = WeakSet()
    owners = 0

    def repair(protocol: EntariProtocol) -> None:
        previous = protocol.internal
        if getattr(previous, "__llm_chat_internal_owner__", None) is initialize:
            return
        operation = getattr(previous, "__wrapped__", previous)
        operation_signature = signature(operation)

        @wraps(operation)
        async def internal(*args: Any, **kwargs: Any) -> Any:
            bounds = operation_signature.bind(*args, **kwargs)
            bounds.apply_defaults()
            try:
                override = await es.post(APIRequest(protocol.account, "internal", bounds.arguments))
                if override is not None:
                    result = override.value
                else:
                    # Entari uses **bounds.arguments, nesting the variadic payload
                    # under "kwargs" instead of passing it to the adapter intact.
                    result = await operation(*bounds.args, **bounds.kwargs)
            except Exception as exc:
                await es.publish(APIResponse(protocol.account, "internal", bounds.arguments, False, exc))
                # Returning exceptions makes reaction feedback claim a failed
                # request succeeded. Cancellation is deliberately not caught.
                raise
            await es.publish(APIResponse(protocol.account, "internal", bounds.arguments, True, result))
            return result

        setattr(internal, "__llm_chat_internal_owner__", initialize)
        setattr(internal, "__llm_chat_original__", previous)
        protocol.internal = internal
        protocols.add(protocol)

    @wraps(previous_init)
    def initialize(self: EntariProtocol, *args: Any, **kwargs: Any) -> None:
        previous_init(self, *args, **kwargs)
        repair(self)

    def acquire_owner() -> Callable[[], None]:
        nonlocal owners
        owners += 1
        disposed = False

        def restore() -> None:
            nonlocal owners, disposed
            if disposed:
                return
            disposed = True
            owners -= 1
            if owners:
                return
            if EntariProtocol.__init__ is initialize:
                EntariProtocol.__init__ = previous_init
            for protocol in protocols:
                current = protocol.internal
                if getattr(current, "__llm_chat_internal_owner__", None) is initialize:
                    protocol.internal = getattr(current, "__llm_chat_original__")

        return restore

    setattr(initialize, "__llm_chat_internal_acquire__", acquire_owner)
    EntariProtocol.__init__ = initialize
    try:
        accounts = get_accounts()
    except RuntimeError:
        accounts = {}
    for account in accounts.values():
        if isinstance(account.protocol, EntariProtocol):
            repair(account.protocol)
    return acquire_owner()
