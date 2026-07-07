"""Test bootstrap: import pure plugin modules without executing plugin __init__.

Plugin package __init__ files run Entari runtime code (metadata(), services),
which requires a live EntariConfig. We register a synthetic package whose
__path__ points at the plugin directory so submodules (config/media/persona/*)
import cleanly while __init__.py is never executed.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _register(alias: str, path: Path) -> None:
    if alias in sys.modules:
        return
    pkg = types.ModuleType(alias)
    pkg.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[alias] = pkg


_register("llm_chat_src", ROOT / "plugins" / "llm_chat")
_register("llm_chat_src.persona", ROOT / "plugins" / "llm_chat" / "persona")
