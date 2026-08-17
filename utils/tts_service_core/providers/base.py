"""Provider protocol and error types for TTS backends."""

from typing import Protocol

from ..voice_catalog import TTSVoiceCatalog, TTSSynthesisRequest


class TTSError(Exception):
    """Base error for TTS synthesis failures."""


class TTSSynthesisError(TTSError):
    """Raised when the provider fails to synthesize audio (timeout, bad response)."""


class TTSProvider(Protocol):
    @property
    def file_extension(self) -> str:
        """Preferred local file suffix for returned audio bytes, including the leading dot."""
        ...

    async def get_voice_catalog(self, *, refresh: bool = False) -> TTSVoiceCatalog:
        """Return the current provider voice catalog."""
        ...

    async def synthesize(self, request: TTSSynthesisRequest) -> bytes:
        """Synthesize one request. Raises TTSSynthesisError on failure."""
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP clients etc.)."""
        ...
