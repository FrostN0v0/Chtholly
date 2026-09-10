"""Run one isolated public-API regression: python entari_reload_probe.py SCENARIO.

Use the target environment's interpreter. No project modules/configuration are loaded;
Entari discovers generated plugins through its normal external_dirs configuration.
The service case runs Entari/Launart without accounts, connectors, or entry-point plugins.
"""

import os
import sys
import json
from typing import Protocol, cast
import asyncio
from pathlib import Path
import argparse
import tempfile
import importlib
import traceback

SCENARIOS = ("single", "repeated", "package", "subplugin", "replacement", "service")


def write_plugin(relative: str, source: str) -> None:
    path = Path("plugins") / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    importlib.invalidate_caches()


def handlers(command_name: str, version: str) -> str:
    tag = f"{command_name}:{version}"
    return f"""
from pathlib import Path
from arclet.entari import command, listen, ConfigReload

@command.on({command_name!r})
def answer():
    with Path("commands.log").open("a", encoding="utf-8") as stream:
        stream.write({(tag + chr(10))!r})
    return {tag!r}

@listen(ConfigReload)
def observe(event: ConfigReload):
    if event.scope == "plugin" and event.key == "lifecycle-probe":
        event.value.append({tag!r})
"""


def failure(label: str) -> str:
    # A marker proves we reached the intended import failure, rather than failing
    # earlier because the fixture itself was malformed or a dependency was missing.
    return f"""
from pathlib import Path
Path("attempt.txt").write_text({label!r}, encoding="utf-8")
raise RuntimeError({("intentional candidate failure: " + label)!r})
"""


async def assert_commands(expected: dict[str, str | None]) -> None:
    from arclet.entari import command

    for name, version in expected.items():
        Path("commands.log").write_text("", encoding="utf-8")
        actual = await command.execute(name)
        tag = None if version is None else f"{name}:{version}"
        assert actual == tag, (name, actual, tag)
        calls = Path("commands.log").read_text(encoding="utf-8").splitlines()
        assert calls == ([] if tag is None else [tag]), (name, calls)


async def assert_listeners(expected: dict[str, str | None]) -> None:
    from arclet.entari import ConfigReload
    from arclet.letoderea import publish

    calls = []
    await publish(ConfigReload("plugin", "lifecycle-probe", calls))
    wanted = sorted(f"{name}:{version}" for name, version in expected.items() if version is not None)
    assert sorted(calls) == wanted, (calls, wanted)


async def assert_behavior(expected: dict[str, str | None]) -> None:
    await assert_commands(expected)
    await assert_listeners(expected)


async def reject_candidate(name: str, label: str, *, subplugin: bool = False) -> None:
    from arclet.entari.plugin import reload_plugin, reload_subplugin

    Path("attempt.txt").unlink(missing_ok=True)
    reload = reload_subplugin if subplugin else reload_plugin
    assert await reload(name) is False, name
    assert Path("attempt.txt").read_text(encoding="utf-8") == label


async def unload_tree(root: str, names: list[str], commands: list[str]) -> None:
    from arclet.entari.plugin import find_plugin, unload_plugin_async

    assert await unload_plugin_async(root) is True
    for name in names:
        assert find_plugin(name) is None, name
    await assert_behavior(dict.fromkeys(commands))
    assert await unload_plugin_async(root) is False


async def single(*, repeated: bool = False) -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    name = "reload_single"
    write_plugin(f"{name}.py", handlers("probe", "v1"))
    assert load_plugin(name, config={}) is not None
    await assert_behavior({"probe": "v1"})
    write_plugin(f"{name}.py", handlers("probe", "v2"))
    assert await reload_plugin(name) is True
    active = find_plugin(name)
    assert active is not None
    await assert_behavior({"probe": "v2"})

    attempts = ("early", "registered-1", "registered-2") if repeated else ("early",)
    for label in attempts:
        source = "" if label == "early" else handlers("probe", label) + handlers("candidate", label)
        write_plugin(f"{name}.py", source + failure(label))
        await reject_candidate(name, label)
        # Behavior comes first: orphaned-but-still-working handlers alone are not
        # sufficient; the plugin must also remain manageable through find/reload.
        await assert_behavior({"probe": "v2", "candidate": None})
        assert find_plugin(name) is active

    write_plugin(f"{name}.py", handlers("probe", "v3"))
    assert await reload_plugin(name) is True
    assert find_plugin(name) is not active
    await assert_behavior({"probe": "v3", "candidate": None})
    await unload_tree(name, [name], ["probe", "candidate"])


def package_source(name: str, children: tuple[str, ...], version: str) -> str:
    imports = "from arclet.entari import package\n"
    imports += f"package(*{tuple(f'{name}.{child}' for child in children)!r})\n"
    imports += "".join(f"from . import {child}\n" for child in children)
    return imports + handlers(name.rsplit(".", 1)[-1], version)


def write_tree(version: str, *, grandchild: bool) -> None:
    write_plugin("tree/__init__.py", package_source("tree", ("child", "sibling"), version))
    write_plugin("tree/sibling.py", handlers("sibling", version))
    if grandchild:
        write_plugin("tree/child/__init__.py", package_source("tree.child", ("grandchild",), version))
        write_plugin("tree/child/grandchild.py", handlers("grandchild", version))
    else:
        write_plugin("tree/child.py", handlers("child", version))


async def package_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    write_tree("v1", grandchild=True)
    assert load_plugin("tree", config={}) is not None
    names = ["tree", "tree.child", "tree.child.grandchild", "tree.sibling"]
    commands = ["tree", "child", "grandchild", "sibling", "candidate"]
    active = {name: find_plugin(name) for name in names}
    assert all(plugin is not None for plugin in active.values())
    expected = dict.fromkeys(commands[:-1], "v1") | {"candidate": None}
    await assert_behavior(expected)

    write_tree("bad", grandchild=True)
    write_plugin("tree/candidate.py", handlers("candidate", "bad"))
    write_plugin(
        "tree/__init__.py",
        package_source("tree", ("child", "sibling", "candidate"), "bad") + failure("package"),
    )
    await reject_candidate("tree", "package")
    await assert_behavior(expected)
    for name, plugin in active.items():
        assert find_plugin(name) is plugin, name
    assert find_plugin("tree.candidate") is None

    write_tree("v3", grandchild=True)
    assert await reload_plugin("tree") is True
    await assert_behavior(dict.fromkeys(commands[:-1], "v3") | {"candidate": None})
    await unload_tree("tree", [*names, "tree.candidate"], commands)


async def subplugin_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_subplugin

    write_tree("v1", grandchild=False)
    assert load_plugin("tree", config={}) is not None
    names = ["tree", "tree.child", "tree.sibling"]
    active = {name: find_plugin(name) for name in names}
    assert all(plugin is not None for plugin in active.values())
    await assert_behavior({"tree": "v1", "child": "v1", "sibling": "v1"})
    write_plugin("tree/child.py", handlers("child", "bad") + failure("subplugin"))
    await reject_candidate("tree.child", "subplugin", subplugin=True)
    await assert_behavior({"tree": "v1", "child": "v1", "sibling": "v1"})
    for name, plugin in active.items():
        assert find_plugin(name) is plugin, name

    write_plugin("tree/child.py", handlers("child", "v2"))
    assert await reload_subplugin("tree.child") is True
    assert find_plugin("tree") is active["tree"]
    assert find_plugin("tree.sibling") is active["tree.sibling"]
    assert find_plugin("tree.child") is not active["tree.child"]
    await assert_behavior({"tree": "v1", "child": "v2", "sibling": "v1"})
    await unload_tree("tree", names, ["tree", "child", "sibling"])


async def replacement() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    name = "reload_replacement"
    write_plugin(f"{name}.py", handlers("probe", "v1"))
    old = load_plugin(name, config={})
    assert old is not None
    await assert_behavior({"probe": "v1"})
    write_plugin(f"{name}.py", handlers("probe", "v2"))
    assert await reload_plugin(name) is True
    current = find_plugin(name)
    assert current is not None
    assert current is not old
    # A stale owner may be disposed again after the normal successful swap.
    if tasks := old.dispose():
        await asyncio.gather(*tasks)
    assert find_plugin(name) is current
    await assert_behavior({"probe": "v2"})
    write_plugin(f"{name}.py", handlers("probe", "v3"))
    assert await reload_plugin(name) is True
    await assert_behavior({"probe": "v3"})
    await unload_tree(name, [name], ["probe"])


def service_source(version: str) -> str:
    return (
        handlers("probe", version)
        + f"""
import asyncio
from arclet.entari import add_service
from launart import Service

class EchoService(Service):
    id = "probe.echo"
    required = set()
    stages = {{"preparing", "blocking", "cleanup"}}

    def __init__(self):
        super().__init__()
        self.requests = asyncio.Queue()
        self.closed = asyncio.Event()

    async def ask(self, text):
        response = asyncio.get_running_loop().create_future()
        await self.requests.put((text, response))
        return await response

    async def launch(self, manager):
        async with self.stage("preparing"):
            pass
        async with self.stage("blocking"):
            exit_task = asyncio.create_task(manager.status.wait_for_sigexit())
            request = None
            try:
                while not exit_task.done():
                    request = asyncio.create_task(self.requests.get())
                    done, _ = await asyncio.wait(
                        (exit_task, request), return_when=asyncio.FIRST_COMPLETED
                    )
                    if request in done:
                        text, response = request.result()
                        response.set_result({version!r} + ":" + text)
            finally:
                pending = [task for task in (request, exit_task) if task is not None]
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        async with self.stage("cleanup"):
            self.closed.set()

service = add_service(EchoService())
"""
    )


async def service_reload(app) -> None:
    from creart import it
    from launart import Launart
    from launart.status import ServiceStatus
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin, unload_plugin_async
    from arclet.entari.plugin.service import plugin_service

    class EchoService(Protocol):
        status: ServiceStatus
        closed: asyncio.Event

        async def ask(self, text: str) -> str: ...

    manager = it(Launart)
    manager.add_component(app)
    running = asyncio.create_task(manager.launch())
    try:
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 10)
        write_plugin("reload_service.py", service_source("v1"))
        active = load_plugin("reload_service", config={})
        assert active is not None
        old = cast(EchoService, manager.get_component("probe.echo"))
        await asyncio.wait_for(old.status.wait_for("blocking"), 10)
        assert await asyncio.wait_for(old.ask("before"), 5) == "v1:before"
        await assert_behavior({"probe": "v1"})

        write_plugin("reload_service.py", service_source("bad") + failure("service"))
        await reject_candidate("reload_service", "service")
        # Wait for a candidate disposal task to run as well as checking lookup.
        await asyncio.sleep(0)
        assert manager.get_component("probe.echo") is old
        assert await asyncio.wait_for(old.ask("after"), 5) == "v1:after"
        assert not old.closed.is_set()
        assert find_plugin("reload_service") is active
        await assert_behavior({"probe": "v1"})

        write_plugin("reload_service.py", service_source("v2"))
        assert await reload_plugin("reload_service") is True
        await asyncio.wait_for(old.closed.wait(), 5)
        current = cast(EchoService, manager.get_component("probe.echo"))
        assert current is not old
        await asyncio.wait_for(current.status.wait_for("blocking"), 10)
        assert await asyncio.wait_for(current.ask("recovered"), 5) == "v2:recovered"
        await assert_behavior({"probe": "v2"})
        assert await unload_plugin_async("reload_service") is True
        await asyncio.wait_for(current.closed.wait(), 5)
        try:
            manager.get_component("probe.echo")
        except ValueError:
            pass
        else:
            raise AssertionError("unloaded echo service is still available")
        assert find_plugin("reload_service") is None
        await assert_behavior({"probe": None})
    finally:
        manager.status.exiting = True
        await asyncio.wait_for(running, 15)


async def run(scenario: str, config_path: Path) -> None:
    from launart import Service
    from arclet.entari import Entari, AiohttpClientService
    from arclet.entari.config import EntariConfig
    from arclet.letoderea.utils import set_event_loop
    from arclet.entari.plugin.service import plugin_service

    set_event_loop(asyncio.get_running_loop())

    class ProbeEntari(Entari):
        def ensure_manager(self, manager):
            # Keep real application/service lifecycle, excluding installed plugin
            # entry points, which could open accounts or touch application data.
            Service.ensure_manager(self, manager)
            manager.add_component(plugin_service)
            manager.add_component(AiohttpClientService())

    app = ProbeEntari.from_config(EntariConfig.load(config_path))
    if scenario == "single":
        await single()
    elif scenario == "repeated":
        await single(repeated=True)
    elif scenario == "package":
        await package_reload()
    elif scenario == "subplugin":
        await subplugin_reload()
    elif scenario == "replacement":
        await replacement()
    elif scenario == "service":
        await service_reload(app)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=SCENARIOS)
    args = parser.parse_args()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    os.environ.clear()
    os.environ.update(environment)
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="entari-reload-probe-") as temporary:
        root = Path(temporary)
        try:
            os.chdir(root)
            for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                os.environ[name] = str(root)
            os.environ["PYTHONIOENCODING"] = "utf-8"
            config_path = root / "entari.json"
            config_path.write_text(
                json.dumps(
                    {
                        "basic": {
                            "network": [],
                            "external_dirs": [str(root / "plugins")],
                            "log": {"level": "ERROR", "save": False, "rich_error": False},
                            "schema": False,
                        },
                        "plugins": {},
                    }
                ),
                encoding="utf-8",
            )
            asyncio.run(asyncio.wait_for(run(args.scenario, config_path), 60))
        except Exception:
            traceback.print_exc()
            return 1
        finally:
            # Windows cannot remove the current working directory.
            os.chdir(original_cwd)
    sys.stdout.write(f"PASS {args.scenario}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
