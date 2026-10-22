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
from datetime import UTC
import tempfile
from textwrap import indent
import importlib
import traceback

SCENARIOS = (
    "single",
    "repeated",
    "catalogue",
    "package",
    "subplugin",
    "subtree",
    "replacement",
    "service",
    "fragments",
    "bindings",
    "niche",
    "watcher",
    "readiness",
    "schema",
    "config",
)


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
    result = await reload(name)
    assert not result, result
    assert isinstance(result.error, Exception), result
    assert Path("attempt.txt").read_text(encoding="utf-8") == label


async def unload_tree(root: str, names: list[str], commands: list[str]) -> None:
    from arclet.alconna import command_manager
    from arclet.entari.plugin import find_plugin, unload_plugin_async

    assert await unload_plugin_async(root) is True
    for name in names:
        assert find_plugin(name) is None, name
    await assert_behavior(dict.fromkeys(commands))
    advertised = command_manager.all_command_help()
    for name in commands:
        assert all(key.rsplit("::", 1)[-1] != name for key in advertised), advertised
    assert await unload_plugin_async(root) is False


async def catalogue_reload() -> None:
    from datetime import datetime

    from satori import EventType, ChannelType
    from satori.model import User, Event, Login, Channel, MessageObject
    from arclet.entari import command
    from arclet.alconna import command_manager
    from arclet.entari.plugin import load_plugin, reload_plugin, unload_plugin_async
    from arclet.entari.session import Session, EntariProtocol
    from satori.client.account import Account, ApiInfo
    from arclet.entari.event.base import MessageCreatedEvent

    account = Account(Login(sn=0, platform="probe", user=User("bot")), ApiInfo(), [], EntariProtocol)
    event = Event(
        EventType.MESSAGE_CREATED,
        datetime.now(UTC),
        account.self_info,
        channel=Channel("channel", ChannelType.TEXT),
        user=User("user"),
        message=MessageObject("message", "catalogue_probe"),
    )
    session: Session = Session(account, MessageCreatedEvent(account, event))
    source = "from arclet.entari import command\n@command.on('catalogue_probe')\ndef answer():\n    return 'pong'\n"
    name = "reload_catalogue"
    path = f"{name}/__init__.py"
    baseline = set(command_manager.all_command_help())
    try:
        write_plugin(path, source)
        assert load_plugin(name, config={})
        for _ in range(2):
            assert await command.execute("catalogue_probe", session) == "pong"
            write_plugin(path, source)
            assert await reload_plugin(name)
        write_plugin(path, source + failure("catalogue"))
        await reject_candidate(name, "catalogue")
        assert await command.execute("catalogue_probe", session) == "pong"
        write_plugin(path, source)
        assert await reload_plugin(name)
        assert await command.execute("catalogue_probe", session) == "pong"
        assert await unload_plugin_async(name) is True
        assert await command.execute("catalogue_probe", session) is None
        assert set(command_manager.all_command_help()) == baseline
    finally:
        await unload_plugin_async(name)
        await account.protocol.session.close()


async def single(*, repeated: bool = False) -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    name = "reload_single"
    write_plugin(f"{name}.py", handlers("probe", "v1"))
    assert load_plugin(name, config={})
    await assert_behavior({"probe": "v1"})
    write_plugin(f"{name}.py", handlers("probe", "v2"))
    assert await reload_plugin(name)
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
    assert await reload_plugin(name)
    assert find_plugin(name) is not active
    await assert_behavior({"probe": "v3", "candidate": None})
    await unload_tree(name, [name], ["probe", "candidate"])


def package_source(name: str, children: tuple[str, ...], version: str) -> str:
    imports = "from arclet.entari import package\n"
    imports += f"package(*{tuple(f'{name}.{child}' for child in children)!r})\n"
    imports += "".join(f"from . import {child}\n" for child in children)
    return imports + handlers(name.rsplit(".", 1)[-1], version)


def write_tree(version: str, *, grandchild: bool, added: bool = False) -> None:
    children = ("child", "sibling", "added") if added else ("child", "sibling")
    write_plugin("tree/__init__.py", package_source("tree", children, version))
    write_plugin("tree/sibling.py", handlers("sibling", version))
    if grandchild:
        write_plugin("tree/child/__init__.py", package_source("tree.child", ("grandchild",), version))
        write_plugin("tree/child/grandchild.py", handlers("grandchild", version))
    else:
        write_plugin("tree/child.py", handlers("child", version))
    if added:
        write_plugin("tree/added/__init__.py", package_source("tree.added", ("leaf",), version))
        write_plugin("tree/added/leaf.py", handlers("leaf", version))


async def package_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    write_tree("v1", grandchild=True)
    assert load_plugin("tree", config={})
    names = ["tree", "tree.child", "tree.child.grandchild", "tree.sibling"]
    await assert_behavior(dict.fromkeys(("tree", "child", "grandchild", "sibling"), "v1"))

    # New descendants must stage with their root, never become live before commit.
    write_tree("v2", grandchild=True, added=True)
    assert await reload_plugin("tree")
    names += ["tree.added", "tree.added.leaf"]
    commands = ["tree", "child", "grandchild", "sibling", "added", "leaf", "candidate"]
    active = {name: find_plugin(name) for name in names}
    assert all(plugin is not None for plugin in active.values())
    expected = dict.fromkeys(commands[:-1], "v2") | {"candidate": None}
    await assert_behavior(expected)

    for label in ("package-1", "package-2"):
        write_tree("bad", grandchild=True, added=True)
        write_plugin("tree/candidate.py", handlers("candidate", "bad"))
        write_plugin(
            "tree/__init__.py",
            package_source("tree", ("child", "sibling", "added", "candidate"), "bad") + failure(label),
        )
        await reject_candidate("tree", label)
        await assert_behavior(expected)
        for name, plugin in active.items():
            assert find_plugin(name) is plugin, name
        assert find_plugin("tree.candidate") is None

    write_tree("v3", grandchild=True, added=True)
    assert await reload_plugin("tree")
    await assert_behavior(dict.fromkeys(commands[:-1], "v3") | {"candidate": None})
    await unload_tree("tree", [*names, "tree.candidate"], commands)


async def subplugin_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_subplugin

    write_tree("v1", grandchild=False)
    assert load_plugin("tree", config={})
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
    assert await reload_subplugin("tree.child")
    assert find_plugin("tree") is active["tree"]
    assert find_plugin("tree.sibling") is active["tree.sibling"]
    assert find_plugin("tree.child") is not active["tree.child"]
    await assert_behavior({"tree": "v1", "child": "v2", "sibling": "v1"})
    await unload_tree("tree", names, ["tree", "child", "sibling"])


async def replacement() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    name = "reload_replacement"
    write_plugin(f"{name}.py", handlers("probe", "v1"))
    loaded = load_plugin(name, config={})
    assert loaded, loaded
    old = loaded.plugin
    await assert_behavior({"probe": "v1"})
    write_plugin(f"{name}.py", handlers("probe", "v2"))
    assert await reload_plugin(name)
    current = find_plugin(name)
    assert current is not None
    assert current is not old
    # A stale owner may be disposed again after the normal successful swap.
    if tasks := old.dispose():
        await asyncio.gather(*tasks)
    assert find_plugin(name) is current
    await assert_behavior({"probe": "v2"})
    write_plugin(f"{name}.py", handlers("probe", "v3"))
    assert await reload_plugin(name)
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


async def stop_manager(manager, running: asyncio.Task) -> None:
    """Follow Launart's signal path so sideload-local statuses can exit too."""
    manager.status.exiting = True
    if group := manager.task_group:
        group.stop = True
        if group.blocking_task is not None:
            group.blocking_task.cancel()
    await asyncio.wait_for(running, 15)


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
        loaded = load_plugin("reload_service", config={})
        assert loaded, loaded
        active = loaded.plugin
        old = cast(EchoService, manager.get_component("probe.echo"))
        await asyncio.wait_for(old.status.wait_for("blocking"), 10)
        assert await asyncio.wait_for(old.ask("before"), 5) == "v1:before"
        await assert_behavior({"probe": "v1"})

        for label in ("service-1", "service-2"):
            write_plugin("reload_service.py", service_source("bad") + failure(label))
            await reject_candidate("reload_service", label)
            # A rejected same-id service must not dispose the live manager object.
            await asyncio.sleep(0)
            assert manager.get_component("probe.echo") is old
            assert await asyncio.wait_for(old.ask("after"), 5) == "v1:after"
            assert not old.closed.is_set()
            assert find_plugin("reload_service") is active
            await assert_behavior({"probe": "v1"})

        write_plugin("reload_service.py", service_source("v2"))
        assert await reload_plugin("reload_service")
        await asyncio.wait_for(old.closed.wait(), 5)
        current = cast(EchoService, manager.get_component("probe.echo"))
        assert current is not old
        await asyncio.wait_for(current.status.wait_for("blocking"), 10)
        assert await asyncio.wait_for(current.ask("recovered"), 5) == "v2:recovered"
        await assert_behavior({"probe": "v2"})
        write_plugin("reload_service.py", service_source("v3"))
        assert await reload_plugin("reload_service")
        await asyncio.wait_for(current.closed.wait(), 5)
        previous = current
        current = cast(EchoService, manager.get_component("probe.echo"))
        assert current is not previous
        await asyncio.wait_for(current.status.wait_for("blocking"), 10)
        assert await asyncio.wait_for(current.ask("latest"), 5) == "v3:latest"
        await assert_behavior({"probe": "v3"})
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
        await stop_manager(manager, running)


async def subtree_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_subplugin

    write_tree("v1", grandchild=True)
    assert load_plugin("tree", config={})
    names = ["tree", "tree.child", "tree.child.grandchild", "tree.sibling"]
    active = {name: find_plugin(name) for name in names}
    expected = {"tree": "v1", "child": "v1", "grandchild": "v1", "sibling": "v1", "candidate": None}
    await assert_behavior(expected)
    for label in ("subtree-1", "subtree-2"):
        write_plugin("tree/child/grandchild.py", handlers("grandchild", "bad"))
        write_plugin("tree/child/candidate.py", handlers("candidate", "bad"))
        write_plugin(
            "tree/child/__init__.py",
            package_source("tree.child", ("grandchild", "candidate"), "bad") + failure(label),
        )
        await reject_candidate("tree.child", label, subplugin=True)
        await assert_behavior(expected)
        for name, plugin in active.items():
            assert find_plugin(name) is plugin, name
        assert find_plugin("tree.child.candidate") is None

    names.append("tree.child.added")
    for version in ("v2", "v3"):
        write_plugin("tree/child/__init__.py", package_source("tree.child", ("grandchild", "added"), version))
        write_plugin("tree/child/grandchild.py", handlers("grandchild", version))
        write_plugin("tree/child/added.py", handlers("added", version))
        assert await reload_subplugin("tree.child")
        assert find_plugin("tree") is active["tree"]
        assert find_plugin("tree.sibling") is active["tree.sibling"]
        expected |= {"child": version, "grandchild": version, "added": version}
        await assert_behavior(expected)
        promoted = {key: find_plugin(key) for key in names}
        for child in ("grandchild", "added", "candidate"):
            write_plugin(f"tree/child/{child}.py", handlers(child, "bad"))
        label = f"subtree-promoted-{version}"
        write_plugin(
            "tree/child/__init__.py",
            package_source("tree.child", ("grandchild", "added", "candidate"), "bad") + failure(label),
        )
        await reject_candidate("tree.child", label, subplugin=True)
        await assert_behavior(expected)
        for key, owner in promoted.items():
            assert find_plugin(key) is owner, key
        assert find_plugin("tree.child.candidate") is None
    await unload_tree("tree", [*names, "tree.child.candidate"], list(expected))


def fragment_source(version: str) -> str:
    marker = f"generation_{version}"
    fragment = {"properties": {marker: {"type": "string", "default": version}}}
    return (
        handlers("probe", version)
        + f"""
from arclet.entari.config import BasicConfModel
from arclet.entari.plugin import metadata, plugin_config, register_schema

class Settings(BasicConfModel):
    value: str = "default"

metadata("fragment probe", config=Settings)
settings = plugin_config(Settings, bind=True)
register_schema(fragment={fragment!r})
"""
    )


async def fragment_reload() -> None:
    from arclet.entari import ConfigReload
    from arclet.letoderea import publish
    from arclet.entari.config import EntariConfig
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin
    from arclet.entari.config.schema import has_schema_fragments

    name = "reload_fragments"
    EntariConfig.instance.plugin[name] = {"value": "configured"}
    write_plugin(f"{name}.py", fragment_source("v1"))
    result = load_plugin(name, config={"value": "configured"})
    assert result, result
    current = result.plugin
    for version in ("v1", "v2", "v3"):
        if version != "v1":
            write_plugin(f"{name}.py", fragment_source(version))
            result = await reload_plugin(name)
            assert result, result
            assert result.plugin is find_plugin(name)
            assert result.plugin is not current
            current = result.plugin
        await assert_behavior({"probe": version})
        properties = current.config_schema()["properties"]
        assert {key for key in properties if key.startswith("generation_")} == {f"generation_{version}"}
        assert properties[f"generation_{version}"]["default"] == version
        assert current.module.settings.value == "configured"
        for attempt in (1, 2):
            label = f"fragment-{version}-{attempt}"
            write_plugin(f"{name}.py", fragment_source("rejected") + failure(label))
            await reject_candidate(name, label)
            assert find_plugin(name) is current
            properties = current.config_schema()["properties"]
            assert {key for key in properties if key.startswith("generation_")} == {f"generation_{version}"}
            await publish(ConfigReload("plugin", name, {"value": label}))
            assert current.module.settings.value == label
            await assert_behavior({"probe": version})
    await unload_tree(name, [name], ["probe"])
    assert not has_schema_fragments(name)


def binding_source(version: str, *, rejected: bool = False) -> str:
    target = "sibling" if rejected else "child"
    return (
        "from . import sibling as historical\n"
        + f"from . import {target} as active\nfrom . import {target} as historical\n"
        + f"import binding_tree.{target} as aliased\nimport binding_tree.{target}\n"
        + handlers("bindings", version)
        + """
@command.on("bound")
def report_binding():
    return ":".join((active.VALUE, historical.VALUE, aliased.VALUE, binding_tree.child.VALUE))
"""
    )


async def binding_reload() -> None:
    from arclet.entari import command
    from arclet.entari.plugin import find_plugin, load_plugin, reload_subplugin

    root = "binding_tree"
    write_plugin(f"{root}/__init__.py", binding_source("v1"))
    for child in ("child", "sibling"):
        write_plugin(f"{root}/{child}.py", "VALUE = 'v1'\n" + handlers(child, "v1"))
    loaded = load_plugin(root, config={})
    assert loaded, loaded
    active = loaded.plugin
    assert await command.execute("bound") == "v1:v1:v1:v1"
    for label in ("bindings-1", "bindings-2"):
        write_plugin(f"{root}/__init__.py", binding_source("bad", rejected=True) + failure(label))
        await reject_candidate(root, label)
        assert find_plugin(root) is active
        assert await command.execute("bound") == "v1:v1:v1:v1"
        await assert_behavior({"bindings": "v1", "child": "v1", "sibling": "v1"})
    for version in ("v2", "v3"):
        write_plugin(f"{root}/child.py", f"VALUE = {version!r}\n" + handlers("child", version))
        result = await reload_subplugin(f"{root}.child")
        assert result, result
        assert result.plugin is find_plugin(f"{root}.child")
        assert find_plugin(root) is active
        assert await command.execute("bound") == ":".join([version] * 4)
        await assert_behavior({"bindings": "v1", "child": version, "sibling": "v1"})
    await unload_tree(root, [root, f"{root}.child", f"{root}.sibling"], ["bindings", "child", "sibling"])
    assert await command.execute("bound") is None


def niche_source(version: str) -> str:
    grandchild = handlers("niche_grandchild", version)
    child = (
        handlers("niche_child", version)
        + "from arclet.entari.plugin import get_plugin\n"
        + "@get_plugin().niche('grandchild')\ndef grandchild(plugin):\n"
        + indent(grandchild, "    ")
        + "\ngrandchild.exec_apply()\n"
    )
    return (
        handlers("niche_root", version)
        + "from arclet.entari.plugin import get_plugin\n"
        + "@get_plugin().niche('child')\ndef child(plugin):\n"
        + indent(child, "    ")
        + "\nchild.exec_apply()\n"
        + "\ndef add_late_niche():\n"
        + "    @get_plugin().niche('late')\n    def late(plugin):\n"
        + indent(handlers("niche_late", version), "        ")
        + "\n    late.exec_apply()\n"
    )


async def niche_reload() -> None:
    from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin

    name = "reload_niche"
    names = [name, f"{name}.child", f"{name}.child.grandchild"]
    commands = ["niche_root", "niche_child", "niche_grandchild"]
    write_plugin(f"{name}.py", niche_source("v1"))
    assert load_plugin(name, config={})
    await assert_behavior(dict.fromkeys(commands, "v1"))
    for version in ("v2", "v3"):
        write_plugin(f"{name}.py", niche_source(version))
        assert await reload_plugin(name)
        active = {key: find_plugin(key) for key in names}
        assert all(owner is not None for owner in active.values())
        await assert_behavior(dict.fromkeys(commands, version))
        active[name].module.add_late_niche()
        active[f"{name}.late"] = find_plugin(f"{name}.late")
        assert active[f"{name}.late"] is not None
        expected = dict.fromkeys([*commands, "niche_late"], version)
        await assert_behavior(expected)
        for attempt in (1, 2):
            label = f"niche-{version}-{attempt}"
            write_plugin(f"{name}.py", niche_source("bad") + failure(label))
            await reject_candidate(name, label)
            for key, owner in active.items():
                assert find_plugin(key) is owner, key
            await assert_behavior(expected)
    await unload_tree(name, [*names, f"{name}.late"], [*commands, "niche_late"])


async def watcher_reload(app) -> None:
    from creart import it
    from launart import Launart
    from arclet.entari import command
    from arclet.entari.plugin import find_plugin, load_plugin
    from arclet.entari.plugin.service import plugin_service

    name = "reload_watched"
    write_plugin(f"{name}.py", handlers("watched", "v1"))
    loaded = load_plugin(name, config={})
    assert loaded, loaded
    first = loaded.plugin
    assert load_plugin(
        "::auto_reload",
        config={"watch_dirs": [str(Path("plugins").resolve())], "watch_config": False, "debounce": 80, "step": 20},
    )

    async def wait_for_command(version: str) -> None:
        deadline = asyncio.get_running_loop().time() + 10
        actual = None
        while asyncio.get_running_loop().time() < deadline:
            actual = await command.execute("watched")
            if actual == f"watched:{version}":
                return
            await asyncio.sleep(0.05)
        raise AssertionError(("watcher did not update command", version, actual))

    manager = it(Launart)
    manager.add_component(app)
    running = asyncio.create_task(manager.launch())
    try:
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 10)
        watcher = manager.get_component("entari.plugin.auto_reload/watcher")
        await asyncio.wait_for(watcher.status.wait_for("blocking"), 10)
        # Service blocking precedes awatch entering its native filesystem loop.
        await asyncio.sleep(0.25)
        await assert_behavior({"watched": "v1"})

        # Function-body-only updates keep their native owner and registrations.
        write_plugin(f"{name}.py", handlers("watched", "v2"))
        await wait_for_command("v2")
        assert find_plugin(name) is first
        await assert_behavior({"watched": "v2"})

        # Module-level changes require an actual owner replacement.
        write_plugin(f"{name}.py", "GENERATION = 'v3'\n" + handlers("watched", "v3"))
        await wait_for_command("v3")
        current = find_plugin(name)
        assert current is not None
        assert current is not first
        await assert_behavior({"watched": "v3"})

        label = "watcher-rejected"
        write_plugin(
            f"{name}.py",
            "GENERATION = 'bad'\n" + handlers("watched", "bad") + handlers("candidate", "bad") + failure(label),
        )
        deadline = asyncio.get_running_loop().time() + 10
        while not Path("attempt.txt").exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert Path("attempt.txt").read_text(encoding="utf-8") == label
        assert find_plugin(name) is current
        await assert_behavior({"watched": "v3", "candidate": None})

        write_plugin(f"{name}.py", "GENERATION = 'v4'\n" + handlers("watched", "v4"))
        await wait_for_command("v4")
        assert find_plugin(name) is not current
        await assert_behavior({"watched": "v4", "candidate": None})
        await unload_tree(name, [name], ["watched", "candidate"])
    finally:
        await stop_manager(manager, running)


async def readiness(app) -> None:
    from creart import it
    from launart import Launart
    from arclet.entari.plugin import load_plugin, unload_plugin_async
    from arclet.entari.plugin.service import plugin_service

    source = """from typing import Annotated
import asyncio
from sqlalchemy import text
from arclet.entari import Ready, plugin, add_service
from arclet.entari.core import on_stage
from graia.amnesia.builtins.sqla import SqlalchemyService

entered = asyncio.Event()
initializing = asyncio.Event()
release = asyncio.Event()
finished = asyncio.Event()
observations = []

class DelayedDatabase(SqlalchemyService):
    async def initialize(self):
        initializing.set()
        await release.wait()
        binds = await super().initialize()
        async with self.engines[""].begin() as connection:
            await connection.execute(text("CREATE TABLE readiness (value INTEGER)"))
            await connection.execute(text("INSERT INTO readiness VALUES (7)"))
        return binds

def attach_database():
    return add_service(DelayedDatabase("sqlite+aiosqlite:///readiness.sqlite3", {"echo": False}))

@plugin.listen(Ready, priority=-110)
async def before():
    entered.set()

@plugin.listen(Ready, priority=-100)
async def migrate(database: Annotated[SqlalchemyService, on_stage("blocking")]):
    result = await database.execute(text("SELECT value FROM readiness"))
    observations.append(("database", database.status.blocking, result.scalar_one()))

@plugin.listen(Ready, priority=-90)
async def following():
    observations.append(("following",))
    finished.set()
"""
    manager = it(Launart)
    manager.add_component(app)
    running = asyncio.create_task(manager.launch())
    try:
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 10)
        write_plugin("readiness_probe.py", source)
        result = load_plugin("readiness_probe", config={})
        assert result, result
        module = result.plugin.module
        await asyncio.wait_for(module.entered.wait(), 5)
        assert module.observations == []
        database = module.attach_database()
        await asyncio.wait_for(module.initializing.wait(), 5)
        assert module.observations == []
        module.release.set()
        await asyncio.wait_for(module.finished.wait(), 5)
        assert module.observations == [("database", True, 7), ("following",)]
        assert await unload_plugin_async("readiness_probe")
        try:
            manager.get_component(database.id)
        except ValueError:
            pass
        else:
            raise AssertionError("Unloaded database remains available for injection")
    finally:
        await stop_manager(manager, running)


async def generated_schema() -> None:
    from jsonschema import ValidationError, Draft202012Validator
    from arclet.entari.config import EntariConfig
    from arclet.entari.plugin import load_plugin, unload_plugin_async

    source = """from arclet.entari import BasicConfModel, metadata

class Profile(BasicConfModel):
    name: str
    prompt: str

class Config(BasicConfModel):
    profiles: dict[str, Profile]

metadata(name="Nested configuration", config=Config)
"""
    write_plugin("schema_probe.py", source)
    result = load_plugin("schema_probe", config={})
    assert result, result
    try:
        EntariConfig.instance.generate_schema([result.plugin])
        schema = json.loads(Path("entari.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        validator.validate(
            {"basic": {}, "plugins": {"schema_probe": {"profiles": {"main": {"name": "Main", "prompt": "Valid"}}}}}
        )
        for invalid in ({"name": "Main"}, {"name": 42, "prompt": "Invalid"}):
            try:
                validator.validate({"basic": {}, "plugins": {"schema_probe": {"profiles": {"main": invalid}}}})
            except ValidationError:
                pass
            else:
                raise AssertionError("Generated full-file schema accepted an invalid nested profile")
    finally:
        assert await unload_plugin_async("schema_probe")


async def configuration_apply(config_path: Path) -> None:
    from arclet.letoderea import post
    from arclet.entari.config import EntariConfig
    from arclet.entari.plugin import find_plugin
    from arclet.entari.config.model import ApplyReport
    from arclet.entari.event.config import ConfigModify
    from arclet.entari.plugin.apply import applier

    name = "reload_configured"
    cfg = EntariConfig.instance
    data = json.loads(config_path.read_text(encoding="utf-8"))

    def source(version: str) -> str:
        return (
            handlers("configured", version)
            + f"""
from arclet.entari import BasicConfModel, metadata, plugin_config
from arclet.entari.plugin import get_plugin

class Settings(BasicConfModel):
    value: str = "default"

metadata("Config apply probe", config=Settings)
settings = plugin_config(Settings)

@get_plugin().collect
def disposed():
    with Path("disposed.log").open("a", encoding="utf-8") as stream:
        stream.write({(version + chr(10))!r})
"""
        )

    def persist(value: str | None) -> None:
        data["plugins"] = {} if value is None else {name: {"value": value}}
        config_path.write_text(json.dumps(data), encoding="utf-8")

    async def modify() -> ApplyReport:
        result = await post(ConfigModify(config_path, source="lifecycle-probe"))
        assert result is not None
        assert isinstance(result.value, ApplyReport)
        return result.value

    def disposals() -> list[str]:
        path = Path("disposed.log")
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    write_plugin(f"{name}.py", source("v1"))
    persist("v1")
    # ConfigModify consumes an already persisted file; it is not a save request.
    assert name not in cfg.plugin
    assert find_plugin(name) is None
    report = await modify()
    assert report.ok
    assert report.applied
    assert report.loaded == [name]
    assert not report.errors
    current = find_plugin(name)
    assert current is not None
    assert current.module.settings.value == "v1"
    await assert_behavior({"configured": "v1"})

    report = await modify()
    assert report.ok
    assert not report.applied
    assert find_plugin(name) is current
    assert disposals() == []

    write_plugin(f"{name}.py", source("v2"))
    persist("v2")
    report = await modify()
    assert report.ok
    assert report.reloaded == [name]
    previous = current
    current = find_plugin(name)
    assert current is not None
    assert current is not previous
    assert current.module.settings.value == "v2"
    assert disposals() == ["v1"]
    await assert_behavior({"configured": "v2"})

    config_path.write_text("{", encoding="utf-8")
    report = await modify()
    assert not report.ok
    assert report.errors
    assert cfg.plugin[name]["value"] == "v2"
    assert find_plugin(name) is current
    assert config_path.read_text(encoding="utf-8") == "{"
    assert disposals() == ["v1"]

    label = "config-rejected"
    write_plugin(f"{name}.py", source("rejected") + failure(label))
    persist("v3")
    report = await modify()
    assert not report.ok
    assert any(error.plugin == name for error in report.errors)
    # Native apply retains desired disk/config state while the old runtime lives.
    assert cfg.plugin[name]["value"] == "v3"
    assert json.loads(config_path.read_text(encoding="utf-8"))["plugins"][name]["value"] == "v3"
    assert find_plugin(name) is current
    assert current.module.settings.value == "v2"
    assert disposals() == ["v1", "rejected"]
    await assert_behavior({"configured": "v2"})

    file_path = str(current.module.__file__)
    write_plugin(f"{name}.py", source("v3"))
    assert await applier.retry(file_path)
    assert not await applier.retry(file_path)
    previous = current
    current = find_plugin(name)
    assert current is not None
    assert current is not previous
    assert current.module.settings.value == "v3"
    assert disposals() == ["v1", "rejected", "v2"]
    await assert_behavior({"configured": "v3"})

    persist(None)
    report = await modify()
    assert report.ok
    assert report.unloaded == [name]
    assert find_plugin(name) is None
    assert disposals() == ["v1", "rejected", "v2", "v3"]
    await assert_behavior({"configured": None})


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
    elif scenario == "catalogue":
        await catalogue_reload()
    elif scenario == "package":
        await package_reload()
    elif scenario == "subplugin":
        await subplugin_reload()
    elif scenario == "subtree":
        await subtree_reload()
    elif scenario == "replacement":
        await replacement()
    elif scenario == "service":
        await service_reload(app)
    elif scenario == "fragments":
        await fragment_reload()
    elif scenario == "bindings":
        await binding_reload()
    elif scenario == "niche":
        await niche_reload()
    elif scenario == "watcher":
        await watcher_reload(app)
    elif scenario == "readiness":
        await readiness(app)
    elif scenario == "schema":
        await generated_schema()
    elif scenario == "config":
        await configuration_apply(config_path)


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
