"""Explicit, offline-checked resources for the enabled startup features.

Do not import Entari plugins or LiteLLM here: importing them can create stores,
register services, or download tokenizers before the startup boundary runs.
"""

from __future__ import annotations

import os
import ast
import sys
import json
import shutil
import hashlib
from pathlib import Path
import tempfile
import subprocess
from dataclasses import dataclass
from urllib.request import urlopen
from collections.abc import Mapping
from importlib.metadata import distribution

Plugins = Mapping[str, Mapping[str, object]]
_PREPARE = "uv run --locked main.py --prepare (with the same --config, if used)"
_ENCODINGS = ("cl100k_base", "o200k_base")
_BROWSER_CONSUMERS = frozenset({"help_menu", "status", "llm_chat"})


class _ResourceError(ValueError):
    """An actionable message that contains no native exception payload."""


@dataclass(frozen=True)
class _Browser:
    plugin: str
    engine: str
    channel: str | None
    executable: Path | None
    storage: Path | None
    headless: bool
    mirror: str | None
    proxy: str | None


@dataclass(frozen=True)
class _Vocabulary:
    name: str
    url: str
    digest: str

    @property
    def cache_key(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()


def _absolute(path: str | Path, root: Path) -> Path:
    # Only callers whose native implementation expands '~' do so explicitly.
    return (root / path).resolve()


def _local_cache(plugins: Plugins, root: Path) -> Path:
    config = plugins.get(".localdata", {})
    app_name = str(config.get("app_name", "entari")).lower()
    if config.get("use_global", False):
        from nonestorage import user_cache_dir

        return user_cache_dir(app_name.title()).resolve()
    base = config.get("base_dir") or f".{app_name.lstrip('.')}"
    return _absolute(str(base), root) / "cache"


def _needs_tokenizers(plugins: Plugins) -> bool:
    return "llm" in plugins or "llm_chat" in plugins


def _tokenizer_cache(plugins: Plugins, root: Path) -> Path:
    custom = os.environ.get("CUSTOM_TIKTOKEN_CACHE_DIR")
    native = os.environ.get("TIKTOKEN_CACHE_DIR")
    fallback = os.environ.get("DATA_GYM_CACHE_DIR")
    if custom and native is not None and _absolute(custom, root) != _absolute(native, root):
        raise _ResourceError("CUSTOM_TIKTOKEN_CACHE_DIR and TIKTOKEN_CACHE_DIR disagree; select one cache directory.")
    selected = custom if custom else native if native is not None else fallback
    if selected == "":
        raise _ResourceError(
            "Tokenizer caching is disabled; set a nonempty TIKTOKEN_CACHE_DIR before running --prepare."
        )
    return _absolute(selected, root) if selected is not None else _local_cache(plugins, root) / "tiktoken"


def configure_resources(plugins: Plugins, root: Path) -> None:
    """Set process-only cache configuration, without importing resource users."""
    if _needs_tokenizers(plugins):
        cache = str(_tokenizer_cache(plugins, root))
        # LiteLLM overwrites TIKTOKEN_CACHE_DIR from CUSTOM_TIKTOKEN_CACHE_DIR.
        # Set both so direct Agno/tiktoken use selects the identical directory.
        os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = cache
        os.environ["TIKTOKEN_CACHE_DIR"] = cache
        # Native LiteLLM otherwise fetches pricing metadata during import.
        # An explicit non-true value opts back into its remote refresh policy.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _browsers(plugins: Plugins, root: Path) -> list[_Browser]:
    browsers: list[_Browser] = []
    if "browser" in plugins or _BROWSER_CONSUMERS.intersection(plugins):
        config = plugins.get("browser", {})
        if config.get("connect_endpoint") is None:
            engine = str(config.get("browser_type", "chromium"))
            executable = config.get("executable_path")
            browsers.append(
                _Browser(
                    "browser",
                    engine,
                    _optional_string(config.get("channel")),
                    _absolute(str(executable), root) if executable else None,
                    None,
                    config.get("headless") is not False,
                    _optional_string(config.get("playwright_download_host")),
                    _optional_string(config.get("playwright_download_proxy")),
                )
            )
    html = plugins.get("htmlrender", {})
    if html.get("provider") == "playwright":
        provider_config = html.get("provider_config", {})
        if not isinstance(provider_config, Mapping):
            raise _ResourceError("htmlrender.provider_config must be a mapping.")
        remote = any(
            isinstance(endpoint, Mapping) and endpoint.get("endpoint")
            for endpoint in (provider_config.get("connect_ws"), provider_config.get("connect_cdp"))
        )
        if not remote:
            storage = provider_config.get("storage_path")
            cache = (
                _absolute(Path(str(storage)).expanduser(), root)
                if storage is not None
                else _local_cache(plugins, root) / "htmlrender" / "playwright"
            )
            executable = provider_config.get("executable_path")
            normalized = str(executable).strip() if executable is not None else ""
            browsers.append(
                _Browser(
                    "htmlrender",
                    str(provider_config.get("engine", "chromium")),
                    _optional_string(provider_config.get("channel")),
                    _absolute(normalized, root) if normalized and normalized != "." else None,
                    cache,
                    True,
                    _optional_string(provider_config.get("install_mirror")),
                    _optional_string(provider_config.get("install_proxy")),
                )
            )
    return browsers


def _driver() -> tuple[str, Path]:
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    return node, Path(cli)


def _browser_env(browser: _Browser) -> dict[str, str]:
    env = os.environ.copy()
    if browser.storage is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser.storage)
    return env


# Ask the installed driver's registry instead of duplicating platform/revision
# tables. This only resolves paths; it never launches or installs a browser.
_REGISTRY_QUERY = """
const path = require('path');
const bundle = require(path.join(process.argv[1], 'lib', 'coreBundle.js'));
const {registry, registryDirectory} = bundle.registry;
const engine = process.argv[2];
const channel = process.argv[3];
const name = channel || (engine === 'chromium' && process.argv[4] === '1'
    ? 'chromium-headless-shell' : engine);
const entry = registry.findExecutable(name);
if (!entry || entry.browserName !== engine) process.exit(2);
console.log(JSON.stringify({cache: registryDirectory, executable: entry.executablePath() || null,
    managed: entry.installType === 'download-by-default' || entry.installType === 'download-on-demand'}));
"""


def _browser_location(browser: _Browser, root: Path) -> tuple[Path | None, Path | None, bool]:
    if browser.executable is not None:
        return browser.executable, None, False
    if browser.engine not in {"chromium", "firefox", "webkit"}:
        raise _ResourceError(f"{browser.plugin}: unsupported browser engine; select chromium, firefox, or webkit.")
    node, cli = _driver()
    result = subprocess.run(
        [
            node,
            "-e",
            _REGISTRY_QUERY,
            str(cli.parent),
            browser.engine,
            browser.channel or "",
            "1" if browser.headless else "0",
        ],
        cwd=root,
        env=_browser_env(browser),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise _ResourceError(f"{browser.plugin}: browser engine/channel cannot be resolved; run uv sync --locked.")
    location = json.loads(result.stdout)
    executable = Path(location["executable"]) if location["executable"] else None
    return executable, Path(location["cache"]), bool(location["managed"])


def _executable_ready(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0 and (os.name == "nt" or os.access(path, os.X_OK))
    except OSError:
        return False


def _vocabularies() -> tuple[_Vocabulary, ...]:
    # Read the pinned package's own definitions without executing constructors:
    # the constructors download when the selected cache is missing or damaged.
    source = Path(str(distribution("tiktoken").locate_file("tiktoken_ext/openai_public.py")))
    module = ast.parse(source.read_text(encoding="utf-8"))
    vocabularies: list[_Vocabulary] = []
    for name in _ENCODINGS:
        definition = next(
            (node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name), None
        )
        if definition is None:
            raise _ResourceError("Installed tiktoken definitions are incomplete; run uv sync --locked.")
        calls = [
            node
            for node in ast.walk(definition)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_tiktoken_bpe"
        ]
        if len(calls) != 1 or not calls[0].args:
            raise _ResourceError("Installed tiktoken vocabulary format is unsupported; run uv sync --locked.")
        call = calls[0]
        url = ast.literal_eval(call.args[0])
        digest = next((ast.literal_eval(item.value) for item in call.keywords if item.arg == "expected_hash"), None)
        if (
            not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise _ResourceError("Installed tiktoken vocabulary lacks a trusted URL/hash; run uv sync --locked.")
        vocabularies.append(_Vocabulary(name, url, digest))
    return tuple(vocabularies)


def _valid_vocabulary(path: Path, vocabulary: _Vocabulary) -> bool:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == vocabulary.digest
    except OSError:
        return False


def _directory_issue(path: Path) -> str | None:
    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return "a cache directory or its parent is a file"
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return "the selected cache directory is not writable"
    return None


def resource_issues(plugins: Plugins, root: Path) -> list[str]:
    """Inspect the selected assets without writes, downloads, or browser launch."""
    issues: list[str] = []
    try:
        for browser in _browsers(plugins, root):
            executable, _, managed = _browser_location(browser, root)
            if not _executable_ready(executable):
                remedy = (
                    f"run {_PREPARE}" if managed else "install the selected browser/channel or correct executable_path"
                )
                issues.append(
                    f"{browser.plugin}: selected browser executable is missing, empty, or not executable; {remedy}."
                )
        if _needs_tokenizers(plugins):
            cache = _tokenizer_cache(plugins, root)
            directory_issue = _directory_issue(cache)
            if directory_issue:
                issues.append(f"Tokenizer cache: {directory_issue}; correct the cache override before --prepare.")
            for vocabulary in _vocabularies():
                if not _valid_vocabulary(cache / vocabulary.cache_key, vocabulary):
                    issues.append(
                        f"Tokenizer {vocabulary.name} is missing or damaged in the selected cache; run {_PREPARE}."
                    )
    except Exception as error:
        # Native exceptions can include authenticated download/proxy URLs.
        if isinstance(error, _ResourceError):
            issues.append(str(error))
        else:
            issues.append(f"Resource inspection failed ({type(error).__name__}); run uv sync --locked and {_PREPARE}.")
    return issues


def _run_installer(arguments: list[str], env: dict[str, str], root: Path) -> None:
    node, cli = _driver()
    result = subprocess.run(
        [node, str(cli), *arguments],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Playwright preparation failed; check network and OS prerequisites, then run {_PREPARE}. "
            "Installer output is withheld to protect credentials."
        )


def _prepare_browsers(browsers: list[_Browser], root: Path, with_browser_deps: bool) -> None:
    plans: dict[tuple[Path, str], _Browser] = {}
    engines: set[str] = set()
    for browser in browsers:
        executable, cache, managed = _browser_location(browser, root)
        engines.add(browser.engine)
        if _executable_ready(executable):
            continue
        if not managed or cache is None:
            raise _ResourceError(f"{browser.plugin}: install the selected external browser or correct executable_path.")
        plans.setdefault((cache.resolve(), browser.engine), browser)
    if with_browser_deps and sys.platform.startswith("linux") and engines:
        if os.geteuid() != 0:
            raise _ResourceError(
                "Install Linux browser libraries as an administrator, then rerun --prepare. "
                "This command never runs sudo automatically."
            )
        _run_installer(["install-deps", *sorted(engines)], os.environ.copy(), root)
    for (cache, engine), browser in plans.items():
        env = _browser_env(browser)
        # A shared cache can still serve another checkout's locked revision.
        env["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
        # Resolve the native default/relative/0 store once; installation and
        # readiness must see precisely the same cache even with INIT_CWD set.
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
        if browser.mirror:
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = browser.mirror
        if browser.proxy:
            env["HTTPS_PROXY"] = browser.proxy
            env["HTTP_PROXY"] = browser.proxy
        _run_installer(["install", engine], env, root)


def _existing_tokenizer_caches() -> list[Path]:
    caches = [Path(tempfile.gettempdir()) / "data-gym-cache"]
    try:
        caches.append(Path(str(distribution("litellm").locate_file("litellm/litellm_core_utils/tokenizers"))))
    except Exception:
        # Absence of the optional source cache does not replace the native URL.
        pass
    return caches


def _prepare_vocabulary(cache: Path, vocabulary: _Vocabulary) -> None:
    destination = cache / vocabulary.cache_key
    if _valid_vocabulary(destination, vocabulary):
        return
    source = next(
        (
            directory / vocabulary.cache_key
            for directory in _existing_tokenizer_caches()
            if _valid_vocabulary(directory / vocabulary.cache_key, vocabulary)
        ),
        None,
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".tokenizer-", delete=False) as output:
            temporary = Path(output.name)
            if source is not None:
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, output)
            else:
                with urlopen(vocabulary.url, timeout=60) as response:
                    shutil.copyfileobj(response, output)
        if not _valid_vocabulary(temporary, vocabulary):
            raise _ResourceError("Downloaded vocabulary failed its native SHA-256 integrity check; rerun --prepare.")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_resources(plugins: Plugins, root: Path, *, with_browser_deps: bool = False) -> None:
    """Explicitly install selected assets; valid local assets are reused."""
    configure_resources(plugins, root)
    try:
        _prepare_browsers(_browsers(plugins, root), root, with_browser_deps)
        if _needs_tokenizers(plugins):
            cache = _tokenizer_cache(plugins, root)
            cache.mkdir(parents=True, exist_ok=True)
            for vocabulary in _vocabularies():
                _prepare_vocabulary(cache, vocabulary)
    except _ResourceError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"Resource preparation failed ({type(error).__name__}); check permissions/network, then run {_PREPARE}."
        ) from None
    issues = resource_issues(plugins, root)
    if issues:
        raise RuntimeError("Resource preparation is incomplete: " + " ".join(issues))
