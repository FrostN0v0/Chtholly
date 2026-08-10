"""Provider protocol and error types for TTS backends."""

from typing import Protocol

from .types import JsonValue


class TTSError(Exception):
    """Base error for TTS synthesis failures."""


class TTSSynthesisError(TTSError):
    """Raised when the provider fails to synthesize audio (timeout, bad response)."""


class TTSProvider(Protocol):
    @property
    def file_extension(self) -> str:
        """Preferred local file suffix for returned audio bytes, including the leading dot."""
        ...

    async def synthesize(self, text: str, **params: JsonValue) -> bytes:
        """Synthesize `text` into audio bytes. Raises TTSSynthesisError on failure."""
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP clients etc.)."""
        ...
