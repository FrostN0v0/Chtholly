"""Bounded public-URL and inline image media preparation."""

from __future__ import annotations

from typing import Literal
from dataclasses import dataclass
from urllib.parse import urljoin
from collections.abc import Callable

from aiohttp import TCPConnector, ClientSession, ClientTimeout, DummyCookieJar
from arclet.entari import File, Audio, Image, Video, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..web.policy import WebAccessError, normalize_public_url
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..prepared_media import prepare_media, ensure_media_capacity
from ..core.image_source import IMAGE_FETCH_MAX_BYTES, fetch_image_bytes
from ..web.public_resolver import PublicResolver

WarningSink = Callable[[str], object]
MediaKind = Literal["image", "audio", "video", "file"]
_ALLOWED_INLINE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_MAX_INLINE_SOURCE_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4 + 256
_MAX_MEDIA_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 6


@dataclass
class ExternalImageToolContext:
    """Dependencies for preparing externally sourced media."""

    warn: WarningSink


def _normalize_inline_source(source: str) -> str:
    if len(source) > _MAX_INLINE_SOURCE_CHARS:
        raise DeliveryError("Inline image data is invalid or too large")
    lowered = source.lower()
    if lowered.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator:
            raise DeliveryError("Inline image data is invalid or too large")
        return f"{header.lower()},{''.join(payload.split())}"
    if lowered.startswith("base64://"):
        return f"base64://{''.join(source[9:].split())}"
    if "://" in source:
        raise DeliveryError("Media source must be a public HTTP(S) URL or supported base64 image data")
    return f"base64://{''.join(source.split())}"


async def fetch_public_media(source: str, *, max_bytes: int) -> bytes:
    """Fetch bounded bytes with public-only pinned DNS and validated redirects."""

    try:
        current_url = normalize_public_url(source)
    except WebAccessError:
        raise DeliveryError("A valid public media URL is required") from None
    resolver = PublicResolver()
    connector = TCPConnector(resolver=resolver, use_dns_cache=True)
    try:
        async with ClientSession(
            connector=connector,
            connector_owner=True,
            timeout=ClientTimeout(total=15.0),
            auto_decompress=True,
            cookie_jar=DummyCookieJar(),
            trust_env=False,
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                async with client.get(current_url, allow_redirects=False) as response:
                    if 300 <= response.status < 400:
                        location = response.headers.get("Location")
                        if not location:
                            raise DeliveryError("Public media redirect omitted its target")
                        current_url = normalize_public_url(urljoin(current_url, location))
                        continue
                    if not 200 <= response.status < 300:
                        raise DeliveryError("Public media returned an unsuccessful HTTP status")
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise DeliveryError("Public media exceeded the size limit")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > max_bytes:
                            raise DeliveryError("Public media exceeded the size limit")
                        body.extend(chunk)
                    if not body:
                        raise DeliveryError("Public media returned empty content")
                    return bytes(body)
            raise DeliveryError("Public media exceeded the redirect limit")
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("Public media could not be downloaded safely") from None
    finally:
        await resolver.close()


def _build_media(data: bytes, kind: MediaKind) -> Image | Audio | Video | File:
    if kind == "file":
        return File.of(raw=data, mime="application/octet-stream", name="download")
    element: Image | Audio | Video
    try:
        if kind == "image":
            element = Image.of(raw=data)
        elif kind == "audio":
            element = Audio.of(raw=data)
        else:
            element = Video.of(raw=data)
    except ValueError:
        raise DeliveryError("Media format is not recognized") from None
    mime = element.src[5:].partition(";")[0].lower()
    if not mime.startswith(f"{kind}/"):
        raise DeliveryError("Media bytes do not match the requested kind")
    if kind == "image" and mime not in _ALLOWED_INLINE_MIMES:
        raise DeliveryError("Image format must be JPEG, PNG, WebP, or GIF")
    return element


def register_prepare_external_media(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ExternalImageToolContext,
) -> Subscriber[JSONType]:
    """Register public-URL and inline-base64 media preparation."""

    async def prepare_external_media(
        session: Session,
        source: str,
        kind: MediaKind = "image",
    ) -> dict[str, JSONType]:
        """Prepare one public image, audio, video, or file without sending it.

        The source must be a direct public HTTP(S) URL. Images also accept bounded JPEG, PNG, WebP, or GIF
        base64 data (data:image/...;base64, base64://, or raw base64). Public images are limited to 6 MiB;
        audio, video, and files to 10 MiB. Audio/video bytes must match their requested media kind. Files are
        inert attachments. No local paths, private URLs, credentials, or unchecked protocol URLs are allowed.
        This tool does not search or inspect media. Use prepare_image for registered local reaction images.
        Pass the returned media_ref to send_msg in the desired message position.

        Args:
            source: Direct public media URL or supported inline image data.
            kind: Native media kind: image, audio, video, or file.

        Returns:
            dict: Prepared media reference without the original source.
        """

        if kind not in {"image", "audio", "video", "file"}:
            raise DeliveryError("Unsupported media kind")
        if not isinstance(source, str) or not source.strip():
            raise DeliveryError("Media source is required")
        ensure_media_capacity(1, byte_count=0)
        candidate = source.strip()
        if candidate.lower().startswith(("http://", "https://")):
            data = await fetch_public_media(
                candidate,
                max_bytes=IMAGE_FETCH_MAX_BYTES if kind == "image" else _MAX_MEDIA_BYTES,
            )
        elif kind == "image":
            data = await fetch_image_bytes(session, _normalize_inline_source(candidate))
            if not data:
                raise DeliveryError("Inline image data is invalid or too large")
        else:
            raise DeliveryError("Audio, video, and files require a direct public HTTP(S) URL")
        element = _build_media(data, kind)
        markers = {"image": "[发送了图片]", "audio": "[发送了语音]", "video": "[发送了视频]", "file": "[发送了文件]"}
        return prepare_media(
            session,
            element,
            byte_count=len(data),
            tool_name="prepare_external_media",
            history_marker=markers[kind],
        )

    return register_tool(dispatcher, prepare_external_media)
