"""Real Entari/Launart acceptance lifecycle, confined by the Docker parent."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
import importlib

from creart import it
from launart import Launart, Service
from arclet.entari import Entari, ConfigReload, AiohttpClientService
from arclet.alconna import command_manager
from arclet.letoderea import publish
from worker_rendering import COMMAND_TIMEOUT, renderer_config, verify_renderer, prepare_renderer_environment
from worker_transport import CaptureTransport
from arclet.entari.config import EntariConfig
from arclet.entari.plugin import (
    find_plugin,
    get_plugins,
    load_plugin,
    reload_plugin,
    get_all_subscribers,
    get_plugin_commands,
    unload_plugin_async,
)
from arclet.letoderea.utils import set_event_loop
from arclet.entari.localdata import local_data
from arclet.entari.plugin.service import plugin_service
from graia.amnesia.builtins.memcache import MemcacheService

STAGES = (
    "render_capability",
    "syntax",
    "import",
    "commands",
    "execution",
    "reload",
    "failed_reload",
    "cancellation",
    "services",
    "unload",
)


class Acceptance:
    def __init__(self, payload: dict, root: Path):
        self.payload = payload
        self.root = root
        self.module = "workshop_" + payload["plugin_name"]
        self.package = root / "plugins" / self.module
        self.manifest = payload["manifest"]
        self.results: list[dict] = []
        self.stage = "render_capability"
        self.transport: CaptureTransport | None = None
        self.manager = it(Launart)
        self.baseline_commands: dict[str, int] = {}
        self.baseline_subscribers: set[int] = set()
        self.baseline_services: dict[str, object] = {}
        self.baseline_tasks: set[asyncio.Task] = set()
        self.first_effects: dict[int, tuple[str, ...]] = {}
        self.subscriber_count = 0

    def passed(self, detail: str):
        self.results.append({"name": self.stage, "passed": True, "detail": detail})

    def publish_source(self, *, fail: bool = False):
        self.package.mkdir(parents=True, exist_ok=True)
        for name, source in self.payload["files"].items():
            destination = self.package / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith(".py"):
                compile(source, name, "exec", dont_inherit=True)
            destination.write_bytes(source.encode("utf-8"))
        if fail:
            with (self.package / "__init__.py").open("ab") as stream:
                stream.write(
                    b"\nfrom pathlib import Path as _WorkshopPath\n"
                    b'_WorkshopPath("/workspace/failed-candidate-reached").write_text("reached")\n'
                    b'raise RuntimeError("workshop intentional staged candidate failure")\n'
                )
        importlib.invalidate_caches()

    def inventory(self):
        declared = set(self.manifest["commands"])
        occupied = set(self.payload["occupied_commands"])
        actual: set[str] = set()
        for plug in get_plugins(subplugged=True):
            if plug.id == self.module or plug.id.startswith(self.module + "."):
                for prefixes, head in get_plugin_commands(plug):
                    for prefix in prefixes or [""]:
                        actual.add((str(prefix) + str(head)).split()[0])
        if actual != declared:
            raise AssertionError(
                f"Declared command heads {sorted(declared)!r} differ from native heads {sorted(actual)!r}"
            )
        collisions = actual & (occupied | {path.rsplit("::", 1)[-1].split()[0] for path in self.baseline_commands})
        if collisions:
            raise AssertionError(f"Command collision: {sorted(collisions)!r}")
        catalog = command_manager.get_commands()
        catalog_ids = {command.path: id(command) for command in catalog}
        if any(catalog_ids.get(key) != value for key, value in self.baseline_commands.items()):
            raise AssertionError("Candidate replaced or removed an existing command")
        heads = {str(command.command).split()[0] for command in catalog}
        if not declared <= heads:
            raise AssertionError("Native catalog is missing declared command heads")
        subscribers = get_all_subscribers()
        if not self.baseline_subscribers <= {id(item) for item in subscribers}:
            raise AssertionError("Candidate removed baseline listeners")
        if self.subscriber_count and len(subscribers) != self.subscriber_count:
            raise AssertionError("Replacement leaked or duplicated plugin listeners")
        self.subscriber_count = len(subscribers)

    async def baseline(self):
        assert self.transport is not None
        effects = await self.transport.execute("__workshop_baseline__")
        if effects != ("workshop-baseline-alive",):
            raise AssertionError(f"Baseline command behavior changed: {effects!r}")
        listeners = []
        await publish(ConfigReload("plugin", "workshop-lifecycle-probe", listeners))
        if listeners != ["workshop-baseline-listener"]:
            raise AssertionError(f"Baseline listener effects changed: {listeners!r}")
        for key, value in self.baseline_services.items():
            if self.manager.components.get(key) is not value:
                raise AssertionError(f"Baseline service {key} was changed")

    async def exercise(self, *, first: bool = False):
        assert self.transport is not None
        text_failures: list[str] = []
        for index, check in enumerate(self.manifest["checks"]):
            if not first and not check.get("repeatable", True):
                continue
            effects = await asyncio.wait_for(
                self.transport.execute(check["command"], check.get("operator", False), check["expected_contains"]),
                COMMAND_TIMEOUT,
            )
            images = [effect for effect in effects if "[image mime=" in effect]
            if first and images:
                # Record valid media independently, including when text matching fails.
                self.results.append(
                    {
                        "name": f"{self.stage}_media_{index + 1}",
                        "passed": True,
                        "detail": "\n".join(images)[:4000],
                    }
                )
            if not self.transport.account.protocol.matched_expected:
                text_failures.append(
                    f"Check {index + 1} {check['command']!r}: expected text absent; effects={effects!r}"
                )
            if first:
                self.first_effects[index] = effects
            elif len(effects) != len(self.first_effects[index]):
                raise AssertionError(
                    f"Check {index + 1}: replacement changed visible effect count (possible duplicate handler)"
                )
        await self.baseline()
        if text_failures:
            raise AssertionError("\n".join(text_failures)[:4000])

    async def ready_services(self):
        await asyncio.sleep(0)
        for service in tuple(self.manager.components.values()):
            if service.id in self.baseline_services:
                continue
            if "blocking" in service.stages:
                await asyncio.wait_for(service.status.wait_for("blocking"), 5)
            elif "preparing" in service.stages:
                await asyncio.wait_for(service.status.wait_for("prepared"), 5)

    async def run_candidate(self):
        self.stage = "syntax"
        self.publish_source()
        self.passed("Every Python source compiled without host-side execution")
        self.stage = "import"
        active = load_plugin(self.module, config=json.loads(json.dumps(self.manifest["configuration"])))
        if active is None or find_plugin(self.module) is not active:
            raise AssertionError("Native import/public plugin lookup failed; inspect the bounded worker log")
        await self.ready_services()
        self.passed("Native Entari load and public plugin lookup succeeded")
        self.stage = "commands"
        self.inventory()
        self.passed("Declared heads match native inventory; no host/baseline collision")
        self.stage = "execution"
        await self.exercise(first=True)
        self.passed("All declared checks executed through genuine Session and command.execute; setup ran once")
        self.stage = "reload"
        for _ in range(2):
            previous = find_plugin(self.module)
            self.publish_source()
            if not await reload_plugin(self.module, json.loads(json.dumps(self.manifest["configuration"]))):
                raise AssertionError("Native same-source replacement failed")
            if find_plugin(self.module) is previous or find_plugin(self.module) is None:
                raise AssertionError("Native replacement did not publish a new plugin owner")
            await self.ready_services()
            self.inventory()
            await self.exercise()
        self.passed("Two genuine replacements preserved repeatable behavior and listener/effect counts")
        self.stage = "failed_reload"
        previous = find_plugin(self.module)
        old_services = dict(self.manager.components)
        self.publish_source(fail=True)
        try:
            if await reload_plugin(self.module, json.loads(json.dumps(self.manifest["configuration"]))):
                raise AssertionError("Intentionally broken candidate unexpectedly loaded")
            if not (self.root / "failed-candidate-reached").is_file():
                raise AssertionError("Candidate import failed before the intended failure marker")
            if find_plugin(self.module) is not previous:
                raise AssertionError("Failed candidate did not retain the old runtime owner")
            if any(self.manager.components.get(key) is not value for key, value in old_services.items()):
                raise AssertionError("Failed candidate changed running service ownership")
            await asyncio.sleep(0)
            self.inventory()
            await self.exercise()
        finally:
            self.publish_source()
        if not await reload_plugin(self.module, json.loads(json.dumps(self.manifest["configuration"]))):
            raise AssertionError("Restored source could not reload after failed replacement")
        await self.ready_services()
        self.inventory()
        await self.exercise()
        self.passed(
            "Intentional import failure retained old behavior; source restoration and subsequent reload succeeded"
        )
        self.stage = "unload"
        if not await unload_plugin_async(self.module) or find_plugin(self.module) is not None:
            raise AssertionError("Native asynchronous unload/public lookup failed")
        await asyncio.sleep(0.05)
        await self.baseline()
        if {command.path: id(command) for command in command_manager.get_commands()} != self.baseline_commands:
            raise AssertionError("Candidate commands remain after unload")
        if {id(item) for item in get_all_subscribers()} != self.baseline_subscribers:
            raise AssertionError("Candidate listeners remain after unload")
        self.passed("Unload removed candidate commands/listeners and retained baseline behavior")
        self.stage = "services"
        if dict(self.manager.components) != self.baseline_services:
            raise AssertionError("Candidate services remain after unload")
        self.passed("All candidate-added services removed through Entari/Launart disposal")
        self.stage = "cancellation"
        remaining = asyncio.all_tasks() - self.baseline_tasks - {asyncio.current_task()}
        remaining = {task for task in remaining if not task.done()}
        if remaining:
            names = [task.get_name() for task in remaining]
            for task in remaining:
                task.cancel()
            await asyncio.wait(remaining, timeout=1)
            raise AssertionError(f"Candidate left live tasks after native unload: {names!r}")
        self.passed("No candidate-created tasks remain after native disposal")

    async def run(self):
        set_event_loop(asyncio.get_running_loop())
        prepare_renderer_environment()
        config_path = self.root / "entari.json"
        config_path.write_text(
            json.dumps(
                {
                    "basic": {
                        "network": [],
                        "external_dirs": [str(self.root / "plugins")],
                        "schema": False,
                        "superusers": {"workshop": ["workshop-operator"]},
                        "log": {"level": "ERROR", "save": False, "rich_error": False},
                    },
                    "plugins": {
                        "database": {"name": str(self.root / "acceptance.sqlite3")},
                        "htmlrender": renderer_config(self.package),
                    },
                }
            ),
            encoding="utf-8",
        )

        class IsolatedEntari(Entari):
            def ensure_manager(self, manager):
                # Exclude entry-point auto-discovery; keep the real application and
                # plugin lifecycle, no network connectors or community UI/server.
                Service.ensure_manager(self, manager)
                manager.add_component(plugin_service)
                manager.add_component(AiohttpClientService())
                manager.add_component(MemcacheService())
                manager.add_component(local_data)

        app = IsolatedEntari.from_config(EntariConfig.load(config_path))
        self.manager.add_component(app)
        if load_plugin("entari_plugin_database", config={"name": str(self.root / "acceptance.sqlite3")}) is None:
            raise RuntimeError("Locked SQLite database dependency failed to initialize")
        if load_plugin("entari_plugin_htmlrender", config=renderer_config(self.package)) is None:
            raise RuntimeError("Trusted HTMLRender Playwright dependency failed to initialize")
        running = asyncio.create_task(self.manager.launch())
        probe = None
        try:
            await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 10)
            renderer = self.manager.components.get("htmlrender.runtime")
            if renderer is None:
                raise RuntimeError("Trusted HTMLRender service was not registered")
            await asyncio.wait_for(renderer.status.wait_for("blocking"), COMMAND_TIMEOUT)
            evidence = await asyncio.wait_for(verify_renderer(renderer, self.package), COMMAND_TIMEOUT)
            self.passed("Trusted Playwright renderer decoded deterministic HTML/template PNG: " + evidence)
            plugins = self.root / "plugins"
            plugins.mkdir(exist_ok=True)
            (plugins / "_workshop_acceptance_probe.py").write_bytes(
                Path(__file__).with_name("probe_plugin.py").read_bytes(),
            )
            importlib.invalidate_caches()
            probe = load_plugin("_workshop_acceptance_probe", config={})
            if probe is None:
                raise RuntimeError("Trusted lifecycle probe did not load")
            await asyncio.wait_for(probe.module.service.status.wait_for("blocking"), 5)
            self.transport = CaptureTransport()
            await asyncio.sleep(0.05)
            self.baseline_commands = {command.path: id(command) for command in command_manager.get_commands()}
            self.baseline_subscribers = {id(item) for item in get_all_subscribers()}
            self.baseline_services = dict(self.manager.components)
            self.baseline_tasks = asyncio.all_tasks()
            await self.run_candidate()
            self.stage = "services"
            probe_service, probe_task = probe.module.service, probe.module.task
            if not await unload_plugin_async(probe.id):
                raise AssertionError("Trusted service/task probe did not unload")
            if probe_service.id in self.manager.components or not probe_service.closed.is_set():
                raise AssertionError("Launart did not settle the trusted probe service")
            self.stage = "cancellation"
            if not probe_task.cancelled():
                raise AssertionError("Native disposal did not cancel/settle the trusted probe task")
        finally:
            if find_plugin(self.module) is not None:
                await unload_plugin_async(self.module)
            if self.transport is not None:
                await self.transport.aclose()
            self.manager.status.exiting = True
            group = self.manager.task_group
            if group is not None:
                group.stop = True
                if group.blocking_task is not None:
                    group.blocking_task.cancel()
            await asyncio.wait_for(running, 10)

    def fail(self, error: BaseException):
        self.results.append({"name": self.stage, "passed": False, "detail": f"{type(error).__name__}: {error}"[:4000]})
        present = {result["name"] for result in self.results}
        for name in STAGES:
            if name not in present:
                self.results.append(
                    {"name": name, "passed": False, "detail": "Not reached because an earlier stage failed"}
                )
