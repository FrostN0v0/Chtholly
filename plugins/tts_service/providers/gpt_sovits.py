"""gpt-sovits v2 API provider.

Expected endpoint behavior (api_v2.py of GPT-SoVITS):
POST {api_url} with JSON body -> 200 with raw audio bytes (wav/ogg/aac),
or 400 with a JSON error message.
"""

from typing import Any

import httpx

from .base import TTSSynthesisError


class GptSovitsProvider:
    def __init__(
        self,
        api_url: str,
        *,
        timeout: float = 15.0,
        text_lang: str = "zh",
        default_speaker: str = "",
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.api_url = api_url
        self.text_lang = text_lang
        self.default_speaker = default_speaker
        self.extra_params = extra_params or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def synthesize(self, text: str, **params: Any) -> bytes:
        if not text.strip():
            return b""
        payload: dict[str, Any] = {
            "text": text,
            "text_lang": self.text_lang,
            **self.extra_params,
        }
        speaker = params.pop("speaker", None) or self.default_speaker
        if speaker:
            payload["speaker"] = speaker
        payload.update(params)

        try:
            resp = await self._client.post(self.api_url, json=payload)
        except httpx.TimeoutException as e:
            raise TTSSynthesisError(f"TTS request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise TTSSynthesisError(f"TTS request failed: {e}") from e

        if resp.status_code != 200:
            detail: str
            try:
                detail = str(resp.json())
            except ValueError:
                detail = resp.text[:200]
            raise TTSSynthesisError(f"TTS provider returned {resp.status_code}: {detail}")

        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            raise TTSSynthesisError(f"TTS provider returned JSON instead of audio: {resp.text[:200]}")
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()
