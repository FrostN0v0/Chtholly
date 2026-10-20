"""Read JSON that a model may wrap in one Markdown code fence."""

from __future__ import annotations

import json


def load_model_json(content: str) -> object:
    """Decode strict JSON, tolerating only a single surrounding ``` or ```json fence."""
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)
