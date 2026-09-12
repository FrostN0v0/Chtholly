"""Worker media acceptance defends real pixels, not candidate-declared metadata."""

from __future__ import annotations

from io import BytesIO
import zlib
import base64
import struct
import hashlib

import pytest

from utils.plugin_workshop_sandbox.worker_rendering import (
    MAX_IMAGE_BYTES,
    MediaValidationError,
    safe_effect,
    image_evidence,
    inline_image_evidence,
)


@pytest.fixture
def image_module():
    # Pillow is pinned in the worker image, not added to the host application lock.
    return pytest.importorskip("PIL.Image")


def encode_image(image_module, format="PNG"):
    output = BytesIO()
    image_module.new("RGB", (7, 5), (23, 67, 109)).save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize(
    ("format", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")]
)
def test_inline_delivery_preserves_caption_and_reports_decoded_pixels(image_module, format, mime):
    raw = encode_image(image_module, format)
    encoded = base64.b64encode(raw).decode("ascii")
    effect = safe_effect(f'Weather<img src="data:{mime};base64,{encoded}" title="Forecast" width="999"/>Done')
    assert "Weather" in effect
    assert "Forecast" in effect
    assert "Done" in effect
    assert "width=7 height=5" in effect
    assert f"sha256={hashlib.sha256(raw).hexdigest()}" in effect
    assert encoded not in effect


def test_signature_without_decodable_pixels_is_not_an_image(image_module):
    raw = encode_image(image_module)
    with pytest.raises(MediaValidationError):
        image_evidence(raw[:33], "image/png")
    with pytest.raises(MediaValidationError):
        image_evidence(raw, "image/jpeg")


def test_inline_size_and_base64_limits_apply_before_decoding():
    with pytest.raises(MediaValidationError):
        inline_image_evidence("data:image/png;base64,%%%%")
    with pytest.raises(MediaValidationError):
        inline_image_evidence("data:image/png;base64," + "A" * (4 * ((MAX_IMAGE_BYTES + 2) // 3) + 1))
    with pytest.raises(MediaValidationError):
        image_evidence(b"x" * (MAX_IMAGE_BYTES + 1), "image/png")


def test_corrupt_png_crc_and_excessive_dimensions_fail(image_module):
    raw = bytearray(encode_image(image_module))
    raw[29] ^= 1
    with pytest.raises(MediaValidationError):
        image_evidence(bytes(raw), "image/png")
    raw = bytearray(encode_image(image_module))
    raw[16:24] = struct.pack(">II", 8192, 8192)
    raw[29:33] = struct.pack(">I", zlib.crc32(raw[12:29]))
    with pytest.raises(MediaValidationError):
        image_evidence(bytes(raw), "image/png")


def test_every_nested_image_is_validated_before_caption_matching(image_module):
    encoded = base64.b64encode(encode_image(image_module)).decode("ascii")
    with pytest.raises(MediaValidationError):
        safe_effect(
            f'<message>expected caption<img src="data:image/png;base64,{encoded}"/>'
            '<img src="data:image/png;base64,AAAA"/></message>'
        )
    with pytest.raises(MediaValidationError):
        safe_effect('<img src="https://example.com/not-fetched.png"/>')
    with pytest.raises(MediaValidationError):
        safe_effect('<img src="file:///etc/passwd"/>')


def test_animation_budget_is_enforced_over_all_frames(image_module):
    frames = [image_module.new("RGB", (2, 2), (index, 0, 0)) for index in range(129)]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], optimize=False)
    with pytest.raises(MediaValidationError):
        image_evidence(output.getvalue(), "image/gif")


def test_non_image_inline_data_never_enters_report():
    raw = "sensitive-inline-data"
    effect = safe_effect(f'<file src="data:application/octet-stream;base64,{raw}"/>caption')
    assert raw not in effect
    assert "caption" in effect
