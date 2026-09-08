"""Repair stale entari-plugin-llm model selections during startup."""

from __future__ import annotations

from arclet.entari import plugin, local_data
from arclet.entari.logger import log
from entari_plugin_llm.config import _conf
from arclet.entari.event.lifespan import Ready

from .core.model_state import ConfiguredModel
from .core.model_state_store import repair_model_state_file

_LOGGER = log.wrapper("[llm_chat]")


@plugin.listen(Ready, priority=-1000)
async def repair_persisted_model_selections() -> None:
    models = [ConfiguredModel(model.name, model.alias) for model in _conf.models]
    state_path = local_data.get_data_file("entari_plugin_llm", "state.json")
    try:
        repair = repair_model_state_file(state_path, models)
    except (OSError, ValueError) as error:
        _LOGGER.warning(f"failed to repair persisted LLM model selections: {error}")
        return

    if repair.changed:
        _LOGGER.warning(
            "repaired persisted LLM model selections: "
            f"defaults={repair.default_model_updates}, "
            f"session_models={repair.session_model_updates}, "
            f"fallback={repair.fallback_model}"
        )
