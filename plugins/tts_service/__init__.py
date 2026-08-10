"""TTS service plugin.

Exposes a launart Service (`tts.service`) that any plugin can inject to
synthesize speech. Currently backed by the gpt-sovits v2 HTTP API.
"""

from launart import Launart, Service
from arclet.entari import metadata, add_service, plugin_config
from launart.status import Phase
from arclet.entari.plugin import PluginRole

from utils.tts_service_core.providers import TTSProvider, TTSSynthesisError, build_provider
from utils.tts_service_core.providers.types import JsonValue

from .config import TTSConfig

metadata(
    name="tts_service",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="TTS synthesis service (gpt-sovits backend) for other plugins",
    role=PluginRole.UTILITY,
    config=TTSConfig,
)

_config = plugin_config(TTSConfig)

__all__ = ["TTSService", "TTSSynthesisError", "tts"]


class TTSService(Service):
    id = "tts.service"

    def __init__(self, config: TTSConfig) -> None:
        super().__init__()
        self.config = config
        self._provider: TTSProvider | None = None

    @property
    def required(self) -> set[str]:
        return set()

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    @property
    def available(self) -> bool:
        return self._provider is not None

    @property
    def file_extension(self) -> str:
        return self._provider.file_extension if self._provider is not None else ".wav"

    async def synthesize(self, text: str, **params: JsonValue) -> bytes:
        """Synthesize `text` into audio bytes.

        Raises:
            TTSSynthesisError: when the service is not ready or the provider fails.
        """
        if self._provider is None:
            raise TTSSynthesisError("TTS service is not ready")
        return await self._provider.synthesize(text, **params)

    async def launch(self, manager: Launart):
        async with self.stage("preparing"):
            self._provider = build_provider(self.config)
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            provider, self._provider = self._provider, None
            if provider is not None:
                await provider.close()


tts = TTSService(_config)
add_service(tts)
