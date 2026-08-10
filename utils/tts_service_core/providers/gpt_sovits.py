"""gpt-sovits v2 API provider."""

import httpx

from .types import JsonValue, JsonObject
from .http_utils import map_request_error, ensure_audio_response


class GptSovitsProvider:
    file_extension = ".wav"

    def __init__(
        self,
        api_url: str,
        *,
        timeout: float = 15.0,
        text_lang: str = "zh",
        default_speaker: str = "",
        extra_params: JsonObject | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url
        self.text_lang = text_lang
        self.default_speaker = default_speaker
        self.extra_params = extra_params or {}
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def synthesize(self, text: str, **params: JsonValue) -> bytes:
        if not text.strip():
            return b""
        payload: JsonObject = {
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
        except httpx.HTTPError as exc:
            raise map_request_error(exc) from exc
        return ensure_audio_response(resp)

    async def close(self) -> None:
        await self._client.aclose()
