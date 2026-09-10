"""JSON-safe projections of immutable workshop records."""

from __future__ import annotations

from .models import Manifest, ProjectState, VersionRecord, ValidationReport


def manifest_payload(manifest: Manifest) -> dict[str, object]:
    return {
        "title": manifest.title,
        "description": manifest.description,
        "commands": list(manifest.commands),
        "permissions": list(manifest.permissions),
        "data_description": manifest.data_description,
        "configuration": manifest.configuration,
        "checks": [
            {
                "command": check.command,
                "expected_contains": check.expected_contains,
                "operator": check.operator,
                "repeatable": check.repeatable,
            }
            for check in manifest.checks
        ],
    }


def report_payload(report: ValidationReport) -> dict[str, object]:
    return {
        "status": report.status,
        "source_hash": report.source_hash,
        "checks": [{"name": check.name, "passed": check.passed, "detail": check.detail} for check in report.checks],
        "log": report.log,
        "framework_version": report.framework_version,
        "python_version": report.python_version,
        "image_id": report.image_id,
        "passed": report.passed,
    }


def version_payload(record: VersionRecord) -> dict[str, object]:
    return {
        "plugin_name": record.plugin_name,
        "version": record.version,
        "source_hash": record.source_hash,
        "manifest": manifest_payload(record.manifest),
        "owner_key": record.owner_key,
        "scope_id": record.scope_id,
        "created_at": record.created_at,
        "validation_status": record.validation_status,
        "report": report_payload(record.report) if record.report is not None else None,
        "approved_by": record.approved_by,
        "approved_at": record.approved_at,
        "active": record.active,
        "ever_active": record.ever_active,
    }


def state_payload(state: ProjectState) -> dict[str, object]:
    return {
        "plugin_name": state.plugin_name,
        "active_version": state.active_version,
        "enabled": state.enabled,
        "last_error": state.last_error,
    }
