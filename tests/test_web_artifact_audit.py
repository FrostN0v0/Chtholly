"""Artifact source and capability privacy across durable tool event transitions."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from plugins.llm_chat.core.media import is_internal_media_record, sanitize_assistant_history
from plugins.llm_chat.core.tool_trace import ToolTraceRecorder
from plugins.llm_chat.core.tool_trace_policy import DeliverySnapshot


def test_artifact_audit_excludes_sources_binary_payloads_and_capability_links() -> None:
    source = '<html><body>private-project-source<script>window.project = "secret-body";</script></body></html>'
    recorder = ToolTraceRecorder()
    call = recorder.start(
        "publish_web_preview",
        {
            "title": "Dashboard",
            "files": [
                {"path": "index.html", "content": source},
                {"path": "logo.png", "encoding": "base64", "content": "PRIVATE_BINARY_PAYLOAD"},
            ],
        },
    )
    recorder.finish_success(
        call,
        {
            "artifact_ref": "artifact_0123456789abcdef0123456789abcdef",
            "title": "Dashboard",
            "version": 2,
            "preview_url": "https://preview.example/p/PRIVATE_PREVIEW_CAPABILITY/",
            "download_url": "https://preview.example/p/PRIVATE_PREVIEW_CAPABILITY/source.zip",
            "capture_token": "PRIVATE_CAPTURE_CREDENTIAL",
            "files": [{"content": source}],
        },
        before=DeliverySnapshot(active=True),
        after=DeliverySnapshot(active=True),
    )
    event = recorder.events[0]
    durable = json.dumps(asdict(event), default=str)
    assert all(
        private not in durable
        for private in (
            "private-project-source",
            "secret-body",
            "PRIVATE_BINARY_PAYLOAD",
            "PRIVATE_PREVIEW_CAPABILITY",
            "PRIVATE_CAPTURE_CREDENTIAL",
        )
    )
    assert (event.status, event.effect) == ("succeeded", "confirmed")


def test_source_read_audit_excludes_source_body() -> None:
    recorder = ToolTraceRecorder()
    call = recorder.start("read_web_artifact", {"artifact_ref": "artifact_example", "path": "index.html"})
    recorder.finish_success(
        call,
        {"artifact_ref": "artifact_example", "content": "PRIVATE_SOURCE_BODY", "offset": 0, "next_offset": 19},
        before=DeliverySnapshot(active=True),
        after=DeliverySnapshot(active=True),
    )
    event = recorder.events[0]
    assert "PRIVATE_SOURCE_BODY" not in json.dumps(asdict(event), default=str)
    assert event.effect == "observed"


@pytest.mark.parametrize("cancelled", [False, True])
def test_committed_publication_survives_delivery_failure_as_partial_audit(cancelled: bool) -> None:
    recorder = ToolTraceRecorder()
    call = recorder.start("publish_web_preview", {"title": "Dashboard", "files": []})
    recorder.record_evidence(
        call.execution_ref,
        {"artifact_effect": "published", "artifact": {"artifact_ref": "artifact_example", "version": 1}},
    )
    if cancelled:
        recorder.finish_cancelled(call, before=DeliverySnapshot(active=True), after=DeliverySnapshot(active=True))
    else:
        recorder.finish_error(
            call,
            RuntimeError("transport failed"),
            before=DeliverySnapshot(active=True),
            after=DeliverySnapshot(active=True, attempts=1),
        )
    event = recorder.events[0]
    assert event.status == ("cancelled" if cancelled else "failed")
    assert event.effect == "partial"
    assert event.evidence["artifact_effect"] == "published"


def test_source_archive_history_marker_is_not_replayed_as_visible_text() -> None:
    assert is_internal_media_record("[Sent source archive]")
    assert sanitize_assistant_history("[Sent source archive]") is None
    assert sanitize_assistant_history("[Sent source archive] Here is the requested project.") == (
        "Here is the requested project."
    )
