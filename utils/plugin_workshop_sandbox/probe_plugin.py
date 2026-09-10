"""Trusted lifecycle sentinel, loaded as a real Entari plugin inside the container."""

import asyncio

from launart import Service
from arclet.entari import ConfigReload, listen, command, add_service, collect_disposes
from launart.status import Phase

COMMAND = "__workshop_baseline__"
task = asyncio.create_task(asyncio.Event().wait())


async def cancel_task():
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


collect_disposes(cancel_task)


@command.on(COMMAND)
def baseline():
    return "workshop-baseline-alive"


@listen(ConfigReload)
def observe(event: ConfigReload):
    if event.scope == "plugin" and event.key == "workshop-lifecycle-probe":
        event.value.append("workshop-baseline-listener")


class ProbeService(Service):
    id = "workshop.acceptance.probe"

    @property
    def required(self) -> set[str]:
        return set()

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    def __init__(self):
        super().__init__()
        self.closed = asyncio.Event()

    async def launch(self, manager):
        async with self.stage("preparing"):
            pass
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            self.closed.set()


service = add_service(ProbeService())
