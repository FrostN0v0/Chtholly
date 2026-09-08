"""Restore current-source environment references without positional records."""

from __future__ import annotations

import re
import ast
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Mapping

TEMPLATE = re.compile(r"\$\{\{\s*(?P<expression>[^}]+?)\s*\}\}")
ENV_EXPRESSION = re.compile(
    r"env(?:\.(?P<attribute>[A-Za-z_][A-Za-z0-9_]*)|\[\s*(?P<quote>['\"])(?P<item>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)\s*\])"
)
_IDENTITIES = ("name", "alias", "id", "$path")


@dataclass(frozen=True)
class EnvironmentReference:
    name: str
    fallback: str = ""
    optional: bool = False
    use_get: bool = False
    default: str | int | float | bool | None = None

    def resolve(self, env: Mapping[str, str]) -> str:
        value = env.get(self.name, self.default) if self.use_get else env.get(self.name)
        return str(value or self.fallback)


def environment_reference(expression: str) -> EnvironmentReference | None:
    body, marker, fallback = expression.partition(":-")
    match = ENV_EXPRESSION.fullmatch(body.strip())
    if match:
        return EnvironmentReference(match.group("attribute") or match.group("item"), fallback.strip(), bool(marker))
    try:
        node = ast.parse(body.strip(), mode="eval").body
    except (SyntaxError, ValueError):
        return None
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "env"
        and node.func.attr == "get"
        and not node.keywords
        and 1 <= len(node.args) <= 2
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.args[0].value)
    ):
        return None
    default = None
    if len(node.args) == 2:
        argument = node.args[1]
        if not isinstance(argument, ast.Constant) or not isinstance(
            argument.value, (str, int, float, bool, type(None))
        ):
            return None
        default = argument.value
    return EnvironmentReference(node.args[0].value, fallback.strip(), True, True, default)


def expand_environment_template(value: str, env: Mapping[str, str]) -> str | None:
    """Expand only environment expressions; never evaluate user-supplied code."""
    matches = list(TEMPLATE.finditer(value))
    if not matches:
        return None
    pieces: list[str] = []
    offset = 0
    for match in matches:
        reference = environment_reference(match.group("expression"))
        if reference is None:
            return None
        replacement = reference.resolve(env)
        # An empty interpolation is not evidence that any new string is unchanged.
        if not replacement:
            return None
        pieces.extend((value[offset : match.start()], str(replacement)))
        offset = match.end()
    pieces.append(value[offset:])
    return "".join(pieces)


def _list_sources(candidate: object, source: list[object]) -> list[object]:
    if not isinstance(candidate, Mapping):
        return source
    ranked: list[tuple[int, object]] = []
    for item in source:
        if not isinstance(item, Mapping):
            continue
        score = sum(
            1 for key in _IDENTITIES if candidate.get(key) not in (None, "") and candidate.get(key) == item.get(key)
        )
        if score:
            ranked.append((score, item))
    if ranked:
        best = max(score for score, _ in ranked)
        matched = [item for score, item in ranked if score == best]
        if len(matched) == 1:
            return matched
    # Renaming both name and alias must not turn an unchanged credential into a
    # literal. Only unanimous, exact-value references at the same field qualify.
    return [item for item in source if isinstance(item, Mapping)]


def _restore(candidate: object, sources: list[object], env: Mapping[str, str]) -> object:
    if isinstance(candidate, str):
        if not candidate or TEMPLATE.search(candidate):
            return candidate
        originals = {
            source
            for source in sources
            if isinstance(source, str) and expand_environment_template(source, env) == candidate
        }
        return next(iter(originals)) if len(originals) == 1 else candidate
    if isinstance(candidate, Mapping):
        result: dict[str, object] = {}
        for key, value in candidate.items():
            if not isinstance(key, str):
                raise ValueError("Configuration keys must be strings")
            originals = [source[key] for source in sources if isinstance(source, Mapping) and key in source]
            result[key] = _restore(value, originals, env)
        return result
    if isinstance(candidate, list):
        original_items = [item for source in sources if isinstance(source, list) for item in source]
        return [_restore(item, _list_sources(item, original_items), env) for item in candidate]
    return deepcopy(candidate)


def restore_environment_templates(candidate: object, source: object, env: Mapping[str, str]) -> object:
    """Return a detached candidate, restoring only existing, unchanged values.

    Missing candidate paths are never visited. Mapping-list identity, not old
    list offsets, determines the source; ambiguous references remain unchanged
    and sensitive literals are subsequently rejected by validation.
    """
    return _restore(candidate, [source], env)
