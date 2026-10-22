"""Defend protected native saves, observable plugin effects, and model rollback."""

from __future__ import annotations

import os
import sys
import json
import asyncio
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("fixtures") / "native_protected_save.py"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native_save(tmp_path: Path):
    """Keep WebUI/Entari's plugin globals and generated credentials process-local."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    environment.update({key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    environment["NATIVE_SAVE_KEY"] = "isolated-save-fixture-not-a-real-credential"

    async def run(scenario: str, value: object = None) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X",
            "utf8",
            str(PROBE),
            str(ROOT),
            scenario,
            json.dumps(value),
            cwd=tmp_path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 75)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        output = (stdout + stderr).decode("utf-8", errors="replace")
        assert process.returncode == 0, output

    return run


async def test_quoted_block_and_numeric_yaml_do_not_force_model_save_to_restart(native_save):
    await native_save("model-save")


async def test_failed_state_repair_restores_exact_yaml_and_keeps_old_runtime(native_save):
    await native_save("state-rollback")


async def test_file_close_failure_cannot_publish_new_model_state(native_save):
    await native_save("close-rollback")


@pytest.mark.parametrize("value", [-1, True, "96"])
async def test_invalid_companion_budget_does_not_change_saved_or_running_config(native_save, value):
    await native_save("invalid-budget", value)


async def test_operator_contact_budget_is_saved_without_clamping(native_save):
    await native_save("operator-budget")


async def test_native_save_requires_current_etag_on_both_native_put_routes(native_save):
    await native_save("native-etag")


async def test_native_save_cannot_hide_invalid_true_as_existing_one(native_save):
    await native_save("native-bool-number")


async def test_native_save_removes_fields_and_omits_new_schema_defaults(native_save):
    await native_save("native-deletion-defaults")


async def test_native_yaml_save_preserves_user_comments_quotes_and_key_order(native_save):
    await native_save("native-yaml-comments")


async def test_native_whole_plugins_save_deletes_section_and_unloads_plugin(native_save):
    await native_save("native-section-deletion")


async def test_native_form_save_retains_original_credential_environment_reference(native_save):
    await native_save("native-credential-reference")


async def test_native_plugin_apply_reports_in_progress_without_blocking_status(native_save):
    await native_save("native-apply")


async def test_native_plugin_failure_preserves_candidate_and_last_running_digest(native_save):
    await native_save("native-failed-candidate")


async def test_native_partial_failure_cannot_claim_original_runtime_was_restored(native_save):
    await native_save("native-partial-candidate")


async def test_native_lifecycle_cannot_bypass_workshop_and_core_save_boundaries(native_save):
    await native_save("native-protected-lifecycle")


async def test_native_mutations_require_same_origin_and_real_authentication(native_save):
    await native_save("native-origin")


async def test_disposing_protected_save_restores_native_mutation_behavior(native_save):
    await native_save("native-dispose")


async def test_stale_disposer_cannot_remove_new_owners_hook_or_origin_guard(native_save):
    await native_save("native-dispose-ownership")
