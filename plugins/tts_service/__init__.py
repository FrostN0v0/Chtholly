"""TTS service plugin.

Exposes a launart Service (`tts.service`) that any plugin can inject for
GPT-SoVITS GSVI or Fish Audio speech synthesis.
"""

from launart import Launart, Service
from arclet.entari import metadata, add_service, plugin_config
from launart.status import Phase
from arclet.entari.plugin import PluginRole

from utils.tts_service_core.providers import TTSProvider, TTSSynthesisError, build_provider
from utils.tts_service_core.voice_catalog import TTSVoiceCatalog, TTSSynthesisRequest

from .config import TTSConfig

metadata(
    name="tts_service",
    author=[{"name": "FrostN0v0"}],
    version="0.2.0",
    description="TTS synthesis service with dynamic GPT-SoVITS voices and Fish Audio",
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

    async def get_voice_catalog(self, *, refresh: bool = False) -> TTSVoiceCatalog:
        """Return the active provider's current voice catalog."""
        if self._provider is None:
            raise TTSSynthesisError("TTS service is not ready")
        return await self._provider.get_voice_catalog(refresh=refresh)

    async def synthesize(
        self,
        text: str,
        *,
        version: str = "",
        model_name: str = "",
        reference_language: str = "",
        emotion: str = "",
        text_language: str = "",
        speed: float | None = None,
    ) -> bytes:
        """Synthesize text with an optional provider-specific voice selection."""
        if self._provider is None:
            raise TTSSynthesisError("TTS service is not ready")
        request = TTSSynthesisRequest(
            text=text,
            version=version.strip() or None,
            model_name=model_name.strip() or None,
            reference_language=reference_language.strip() or None,
            emotion=emotion.strip() or None,
            text_language=text_language.strip() or None,
            speed=speed,
        )
        return await self._provider.synthesize(request)

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
