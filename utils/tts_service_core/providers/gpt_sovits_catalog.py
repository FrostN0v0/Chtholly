"""Pure GPT-SoVITS voice catalog parsing and selection."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping, Sequence

from .base import TTSSynthesisError
from ..voice_catalog import (
    TTSVoiceOption,
    TTSVoiceCatalog,
    TTSReferenceOption,
    TTSSynthesisRequest,
    TTSSynthesisSelection,
)

GPT_SOVITS_TEXT_LANGUAGES = (
    "\u4e2d\u6587",
    "\u82f1\u8bed",
    "\u65e5\u8bed",
    "\u7ca4\u8bed",
    "\u97e9\u8bed",
    "\u4e2d\u82f1\u6df7\u5408",
    "\u65e5\u82f1\u6df7\u5408",
    "\u7ca4\u82f1\u6df7\u5408",
    "\u97e9\u82f1\u6df7\u5408",
    "\u591a\u8bed\u79cd\u6df7\u5408",
    "\u591a\u8bed\u79cd\u6df7\u5408(\u7ca4\u8bed)",
)
GPT_SOVITS_AUDIO_FORMATS = ("wav", "ogg", "mp3", "aac")
DEFAULT_TEXT_LANGUAGE = "\u4e2d\u6587"
DEFAULT_EMOTION = "\u9ed8\u8ba4"
DEFAULT_SPLIT_METHOD = "\u6309\u6807\u70b9\u7b26\u53f7\u5207"
SPEED_MIN = 0.5
SPEED_MAX = 2.0


def string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def parse_voices(version: str, value: object) -> tuple[TTSVoiceOption, ...]:
    if not isinstance(value, Mapping):
        return ()
    voices: list[TTSVoiceOption] = []
    for model_name, raw_references in value.items():
        if not isinstance(model_name, str) or not isinstance(raw_references, Mapping):
            continue
        references: list[TTSReferenceOption] = []
        for language, raw_emotions in raw_references.items():
            if not isinstance(language, str):
                continue
            emotions = string_items(raw_emotions)
            if emotions:
                references.append(TTSReferenceOption(language=language, emotions=emotions))
        if references:
            voices.append(TTSVoiceOption(version=version, model_name=model_name, references=tuple(references)))
    return tuple(voices)


def _selection_error(field: str, value: str, options: Sequence[str]) -> TTSSynthesisError:
    available = ", ".join(options[:20]) or "none"
    if len(options) > 20:
        available = f"{available}, ..."
    return TTSSynthesisError(f"Unsupported {field} {value!r}; available values: {available}")


def resolve_selection(
    request: TTSSynthesisRequest,
    catalog: TTSVoiceCatalog,
    *,
    default_version: str = "",
    default_model: str = "",
    default_reference_language: str = "",
    default_emotion: str = "",
    default_text_language: str = DEFAULT_TEXT_LANGUAGE,
    default_speed: float = 1.0,
) -> TTSSynthesisSelection:
    if not catalog.voices:
        raise TTSSynthesisError("GPT-SoVITS has no configured voice models")

    versions = tuple(dict.fromkeys(voice.version for voice in catalog.voices))
    requested_version = (request.version or default_version).strip()
    if requested_version and requested_version not in versions:
        raise _selection_error("version", requested_version, versions)
    version = requested_version or versions[0]

    version_voices = tuple(voice for voice in catalog.voices if voice.version == version)
    model_names = tuple(voice.model_name for voice in version_voices)
    requested_model = (request.model_name or default_model).strip()
    if requested_model and requested_model not in model_names:
        raise _selection_error("model", requested_model, model_names)
    model_name = requested_model or model_names[0]
    voice = next(item for item in version_voices if item.model_name == model_name)

    reference_languages = tuple(reference.language for reference in voice.references)
    requested_reference = (request.reference_language or default_reference_language).strip()
    if requested_reference and requested_reference not in reference_languages:
        raise _selection_error("reference language", requested_reference, reference_languages)
    reference_language = requested_reference or reference_languages[0]
    reference = next(item for item in voice.references if item.language == reference_language)

    requested_emotion = (request.emotion or default_emotion).strip()
    if requested_emotion and requested_emotion not in reference.emotions:
        raise _selection_error("emotion", requested_emotion, reference.emotions)
    emotion = requested_emotion or (DEFAULT_EMOTION if DEFAULT_EMOTION in reference.emotions else reference.emotions[0])

    text_language = (request.text_language or default_text_language).strip()
    if text_language not in catalog.text_languages:
        raise _selection_error("text language", text_language, catalog.text_languages)

    speed_value = default_speed if request.speed is None else request.speed
    if isinstance(speed_value, bool) or not isinstance(speed_value, (int, float)):
        raise TTSSynthesisError("GPT-SoVITS speed must be numeric")
    speed = float(speed_value)
    if not catalog.speed_min <= speed <= catalog.speed_max:
        raise TTSSynthesisError(f"GPT-SoVITS speed must be between {catalog.speed_min} and {catalog.speed_max}")
    return TTSSynthesisSelection(
        version=version,
        model_name=model_name,
        reference_language=reference_language,
        emotion=emotion,
        text_language=text_language,
        speed=speed,
    )


def build_catalog(
    voices: Sequence[TTSVoiceOption],
    *,
    default_version: str = "",
    default_model: str = "",
    default_reference_language: str = "",
    default_emotion: str = "",
    default_text_language: str = DEFAULT_TEXT_LANGUAGE,
    default_speed: float = 1.0,
) -> TTSVoiceCatalog:
    catalog = TTSVoiceCatalog(
        provider="gpt-sovits",
        voices=tuple(voices),
        text_languages=GPT_SOVITS_TEXT_LANGUAGES,
        audio_formats=GPT_SOVITS_AUDIO_FORMATS,
        default_selection=None,
        supports_inline_style_tags=False,
        speed_min=SPEED_MIN,
        speed_max=SPEED_MAX,
        speed_default=default_speed,
    )
    if not voices:
        return catalog
    selection = resolve_selection(
        TTSSynthesisRequest(text=""),
        catalog,
        default_version=default_version,
        default_model=default_model,
        default_reference_language=default_reference_language,
        default_emotion=default_emotion,
        default_text_language=default_text_language,
        default_speed=default_speed,
    )
    return replace(catalog, default_selection=selection)
