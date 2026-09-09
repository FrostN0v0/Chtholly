"""Keep Entari effect cleanup on each test's active asyncio loop."""

from typing import cast
import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio
from arclet.letoderea.utils import _EventSystem, set_event_loop


@pytest_asyncio.fixture(autouse=True)
async def framework_event_loop() -> AsyncIterator[None]:
    previous = _EventSystem.loop
    set_event_loop(asyncio.get_running_loop())
    try:
        yield
    finally:
        set_event_loop(cast(asyncio.AbstractEventLoop, previous))
