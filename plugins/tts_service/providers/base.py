"""Provider protocol and error types for TTS backends."""

from typing import Any, Protocol


class TTSError(Exception):
    """Base error for TTS synthesis failures."""


class TTSSynthesisError(TTSError):
    """Raised when the provider fails to synthesize audio (timeout, bad response)."""


class TTSProvider(Protocol):
    async def synthesize(self, text: str, **params: Any) -> bytes:
        """Synthesize `text` into audio bytes. Raises TTSSynthesisError on failure."""
        ...

    async def close(self) -> None:
        """Release underlying resources (HTTP clients etc.)."""
        ...
