"""Migrate the retired forwarded-image budget before a controlled restart."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
import argparse
from collections.abc import MutableMapping

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.comments import CommentedMap

OLD_KEY = "merged_forward_max_described_images"
NEW_KEY = "merged_forward_max_images"


def migrate(path: Path, *, apply: bool = False) -> bool:
    original = path.read_bytes()
    yaml = YAML()
    yaml.preserve_quotes = True
    config = yaml.load(original.decode("utf-8"))
    if not isinstance(config, MutableMapping):
        raise ValueError("Configuration must be a mapping")
    body = config.get("entari", config)
    plugins = body.get("plugins") if isinstance(body, MutableMapping) else None
    if not isinstance(plugins, MutableMapping):
        raise ValueError("Plugin configuration must be a mapping")
    changed = False
    for name, values in plugins.items():
        if str(name).lstrip("~?") not in {"llm_chat", "plugins.llm_chat"}:
            continue
        if not isinstance(values, CommentedMap) or OLD_KEY not in values:
            continue
        if NEW_KEY in values:
            raise ValueError("Both old and new forwarded-image budgets exist; resolve the conflict first")
        index = list(values).index(OLD_KEY)
        comment = values.ca.items.pop(OLD_KEY, None)
        value = values.pop(OLD_KEY)
        values.insert(index, NEW_KEY, value)
        if comment is not None:
            values.ca.items[NEW_KEY] = comment
        changed = True
    if not changed or not apply:
        return changed
    output = io.StringIO()
    yaml.dump(config, output)
    replacement = output.getvalue().encode("utf-8")
    backup = path.with_name(path.name + ".pre-image-inputs")
    with path.open("r+b") as stream:
        if stream.read() != original:
            raise ValueError("Configuration changed while preparing migration")
        with backup.open("xb") as saved:
            saved.write(original)
            saved.flush()
            os.fsync(saved.fileno())
        stream.seek(0)
        stream.write(replacement)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write changes after backing up original bytes")
    args = parser.parse_args()
    try:
        changed = migrate(args.path, apply=args.apply)
    except (OSError, ValueError, YAMLError):
        parser.exit(1, "Migration failed; inspect the configuration and preserve its backup.\n")
    status = "migrated" if changed and args.apply else "migration required" if changed else "unchanged"
    sys.stdout.write(status + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
