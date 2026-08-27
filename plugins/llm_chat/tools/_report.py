"""Validation for the fixed Jinja report template data model."""

from __future__ import annotations

from collections.abc import Sequence

from ._rendering import MAX_RENDER_SOURCE_CHARS, DEFAULT_RENDER_FONT_FAMILY
from ..core.delivery import DeliveryError

_MAX_METRICS = 12
_MAX_COLUMNS = 12
_MAX_ROWS = 100
_MAX_NOTES = 20


def _normalize_text(value: object, *, field: str, required: bool = True) -> str:
    if not isinstance(value, str):
        raise DeliveryError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise DeliveryError(f"{field} must not be empty")
    return normalized


def _normalize_string_list(value: object, *, field: str, maximum: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DeliveryError(f"{field} must be a list of strings")
    if len(value) > maximum:
        raise DeliveryError(f"{field} contains too many items (maximum {maximum})")
    return [_normalize_text(item, field=f"{field}[{index}]") for index, item in enumerate(value)]


def _normalize_matrix(value: object, *, field: str, maximum: int) -> list[list[str]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DeliveryError(f"{field} must be a list of string lists")
    if len(value) > maximum:
        raise DeliveryError(f"{field} contains too many rows (maximum {maximum})")
    rows: list[list[str]] = []
    for row_index, row in enumerate(value):
        if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
            raise DeliveryError(f"{field}[{row_index}] must be a list of strings")
        rows.append(
            [
                _normalize_text(cell, field=f"{field}[{row_index}][{column_index}]")
                for column_index, cell in enumerate(row)
            ]
        )
    return rows


def normalize_report_variables(
    *,
    title: object,
    subtitle: object = "",
    metrics: object = None,
    columns: object = None,
    rows: object = None,
    notes: object = None,
    max_chars: int = MAX_RENDER_SOURCE_CHARS,
) -> dict[str, object]:
    """Validate the fixed report template's JSON-compatible data model."""

    normalized_title = _normalize_text(title, field="title")
    normalized_subtitle = _normalize_text(subtitle, field="subtitle", required=False)
    normalized_metrics = _normalize_matrix(metrics, field="metrics", maximum=_MAX_METRICS)
    normalized_columns = _normalize_string_list(columns, field="columns", maximum=_MAX_COLUMNS)
    normalized_rows = _normalize_matrix(rows, field="rows", maximum=_MAX_ROWS)
    normalized_notes = _normalize_string_list(notes, field="notes", maximum=_MAX_NOTES)

    for index, metric in enumerate(normalized_metrics):
        if len(metric) not in {2, 3}:
            raise DeliveryError(f"metrics[{index}] must contain label, value, and optional detail")
    if bool(normalized_columns) != bool(normalized_rows):
        raise DeliveryError("columns and rows must be provided together")
    if normalized_columns:
        expected = len(normalized_columns)
        for index, row in enumerate(normalized_rows):
            if len(row) != expected:
                raise DeliveryError(f"rows[{index}] must contain exactly {expected} cells")
    if not (normalized_subtitle or normalized_metrics or normalized_rows or normalized_notes):
        raise DeliveryError("the report needs subtitle, metrics, table rows, or notes")

    total_chars = sum(
        len(value)
        for value in (
            normalized_title,
            normalized_subtitle,
            *normalized_columns,
            *normalized_notes,
            *(cell for metric in normalized_metrics for cell in metric),
            *(cell for row in normalized_rows for cell in row),
        )
    )
    if total_chars > max_chars:
        raise DeliveryError(f"report content exceeds the configured character limit ({max_chars})")

    return {
        "title": normalized_title,
        "subtitle": normalized_subtitle,
        "metrics": normalized_metrics,
        "columns": normalized_columns,
        "rows": normalized_rows,
        "notes": normalized_notes,
        "font_family": DEFAULT_RENDER_FONT_FAMILY,
    }
