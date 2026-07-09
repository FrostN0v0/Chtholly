"""Shared HTTP response handling for TTS providers."""

import httpx

from .base import TTSSynthesisError


def response_detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json())
    except ValueError:
        return resp.text[:200]


def ensure_audio_response(resp: httpx.Response) -> bytes:
    if resp.status_code != 200:
        detail = response_detail(resp)
        raise TTSSynthesisError(f"TTS provider returned {resp.status_code}: {detail}")
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        raise TTSSynthesisError(f"TTS provider returned JSON instead of audio: {resp.text[:200]}")
    return resp.content


def map_request_error(exc: httpx.HTTPError) -> TTSSynthesisError:
    if isinstance(exc, httpx.TimeoutException):
        return TTSSynthesisError(f"TTS request timed out: {exc}")
    return TTSSynthesisError(f"TTS request failed: {exc}")
