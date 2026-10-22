"""Exercise real adapter construction and WebUI/full-file schema validation offline."""

from __future__ import annotations

import sys
import copy
import json
import asyncio
from pathlib import Path

import httpx
from creart import it
from launart import Launart
import jsonschema
from arclet.entari import Entari, AiohttpClientService
from arclet.entari.plugin import find_plugin
from arclet.letoderea.scope import Scope, _scopes
from arclet.letoderea.utils import set_event_loop
from arclet.entari.plugin.service import plugin_service


def validate_schemas(schema: dict, value: dict, server_key: str, *, full: bool) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    validator.check_schema(schema)
    document = {"basic": {}, "plugins": {server_key: value}} if full else value
    validator.validate(document)
    for index, changes in (
        (0, {"intent": {"c2c_group_at_messages": "not-a-boolean"}}),
        (1, {"secrets": {"isolated-app": 42}}),
        (1, {"certfile": 42}),
        (2, {"path": 42}),
        (0, {"$path": "@unknown.adapter"}),
    ):
        invalid = copy.deepcopy(document)
        target = invalid["plugins"][server_key] if full else invalid
        target["adapters"][index].update(changes)
        try:
            validator.validate(invalid)
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError(f"Invalid adapter accepted: {index}, {changes}")


async def main() -> None:
    set_event_loop(asyncio.get_running_loop())
    root_scope = Scope.root()
    manager = it(Launart)
    server_key = sys.argv[1]
    try:
        app = Entari.load("entari.json")
        manager.add_component(AiohttpClientService())
        manager.add_component(app)
        server = find_plugin("entari_plugin_server")
        assert server is not None
        assert len(server.module.adapters) == 3
        assert server.module.adapters[0].intent.c2c_group_at_messages is True
        transport = httpx.ASGITransport(app=server.module.get_asgi())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.get(f"/api/config/plugins:{server_key}/schema")
            assert response.status_code == 200, response.text
            standalone = response.json()["schema"]
        configuration = json.loads(Path("entari.json").read_text(encoding="utf-8"))
        value = configuration["plugins"][server_key]
        value["adapters"] += configuration["adapters"]
        validate_schemas(standalone, value, server_key, full=False)
        from arclet.entari.config import EntariConfig

        EntariConfig.instance.generate_schema([server])
        full_schema = json.loads(Path("entari.schema.json").read_text(encoding="utf-8"))
        validate_schemas(full_schema, value, server_key, full=True)
        assert server.config_schema() == standalone
        sys.stdout.write(
            json.dumps({"server_key": server_key, "standalone_validated": True, "full_file_validated": True}) + "\n"
        )
    finally:
        roots = [key for key in plugin_service.plugins if key not in plugin_service._subplugined]
        for key in reversed(roots):
            if (plug := plugin_service.plugins.get(key)) and (tasks := plug.dispose(is_cleanup=True)):
                await asyncio.gather(*tasks)
        if _scopes.get("$global") is root_scope:
            await root_scope.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
