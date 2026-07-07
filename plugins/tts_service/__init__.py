"""TTS service plugin.

Exposes a launart Service (`tts.service`) that any plugin can inject to
synthesize speech. Currently backed by the gpt-sovits v2 HTTP API.
"""

from typing import Any

from launart import Launart, Service
from arclet.entari import metadata, add_service, plugin_config
from launart.status import Phase
from arclet.entari.plugin import PluginRole

from .config import TTSConfig
from .providers import GptSovitsProvider, TTSSynthesisError

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
        self._provider: GptSovitsProvider | None = None

    @property
    def required(self) -> set[str]:
        return set()

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    @property
    def available(self) -> bool:
        return self._provider is not None

    async def synthesize(self, text: str, **params: Any) -> bytes:
        """Synthesize `text` into audio bytes.

        Raises:
            TTSSynthesisError: when the service is not ready or the provider fails.
        """
        if self._provider is None:
            raise TTSSynthesisError("TTS service is not ready")
        return await self._provider.synthesize(text, **params)

    async def launch(self, manager: Launart):
        async with self.stage("preparing"):
            self._provider = GptSovitsProvider(
                self.config.api_url,
                timeout=self.config.timeout,
                text_lang=self.config.text_lang,
                default_speaker=self.config.default_speaker,
                extra_params=self.config.extra_params,
            )
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            provider, self._provider = self._provider, None
            if provider is not None:
                await provider.close()


tts = TTSService(_config)
add_service(tts)
