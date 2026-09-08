"""Regression contracts for configuration round trips and credential isolation."""

from copy import deepcopy
import json

import pytest

from utils.webui_config_core import ConfigValidationError, prepare_config, validate_candidate

ENV = {"GLOBAL_KEY": "global-fixture-key", "ALPHA_KEY": "alpha-fixture-key", "BRAVO_KEY": "bravo-fixture-key"}


def source_config():
    return {
        "plugins": {
            "llm": {
                "api_key": "${{ env.GLOBAL_KEY }}",
                "base_url": "https://gateway.example/v1",
                "models": [
                    {
                        "name": "alpha",
                        "alias": "a",
                        "api_key": "${{ env.ALPHA_KEY }}",
                        "base_url": "https://alpha.example/v1",
                    },
                    {"name": "bravo", "alias": "b", "api_key": "${{ env.BRAVO_KEY }}"},
                ],
            },
        },
    }


def persisted_config(candidate, source):
    return json.loads(json.dumps(prepare_config(candidate, source, ENV)))


def test_deleted_overrides_remain_absent_across_repeated_reordered_saves():
    source = source_config()
    candidate = deepcopy(source)
    models = candidate["plugins"]["llm"]["models"]
    models[0].pop("api_key")
    models[0].pop("base_url")
    models[1]["api_key"] = ENV["BRAVO_KEY"]
    models.reverse()
    first = persisted_config(candidate, source)
    assert first["plugins"]["llm"]["models"] == [
        {"name": "bravo", "alias": "b", "api_key": "${{ env.BRAVO_KEY }}"},
        {"name": "alpha", "alias": "a"},
    ]
    second_input = deepcopy(first)
    second_input["plugins"]["llm"]["api_key"] = ENV["GLOBAL_KEY"]
    second_input["plugins"]["llm"]["models"][0]["api_key"] = ENV["BRAVO_KEY"]
    second = persisted_config(second_input, first)
    assert second == first
    assert second_input["plugins"]["llm"]["api_key"] == ENV["GLOBAL_KEY"]
    assert source == source_config()


def test_model_rename_keeps_unchanged_credentials_without_positional_records():
    source = source_config()
    candidate = deepcopy(source)
    candidate["plugins"]["llm"]["models"] = [
        {"name": "new-name", "alias": "new-alias", "api_key": ENV["BRAVO_KEY"]},
    ]
    saved = persisted_config(candidate, source)
    assert saved["plugins"]["llm"]["models"] == [
        {"name": "new-name", "alias": "new-alias", "api_key": "${{ env.BRAVO_KEY }}"},
    ]


def test_empty_old_template_does_not_splice_itself_into_new_text():
    source = source_config()
    source["plugins"]["llm"]["prompt"] = "${{ env.OPTIONAL:- }}"
    candidate = deepcopy(source)
    candidate["plugins"]["llm"]["prompt"] = "A new instruction"
    saved = persisted_config(candidate, source)
    assert saved["plugins"]["llm"]["prompt"] == "A new instruction"


def test_scoped_form_placeholders_inherit_without_erasing_explicit_openai_endpoint():
    source = source_config()
    candidate = deepcopy(source)
    candidate["plugins"]["llm"]["models"] = [
        {"name": "inherited", "api_key": None, "base_url": "https://api.openai.com/v1"},
        {"name": "explicit", "base_url": "https://api.openai.com/v1/"},
    ]
    saved = persisted_config(candidate, source)
    assert saved["plugins"]["llm"]["models"] == [
        {"name": "inherited"},
        {"name": "explicit", "base_url": "https://api.openai.com/v1/"},
    ]


def test_literal_credential_rejection_never_contains_the_credential():
    candidate = source_config()
    candidate["plugins"]["llm"]["models"][0]["api_key"] = "must-not-appear-in-error"
    with pytest.raises(ConfigValidationError) as caught:
        validate_candidate(json.dumps(candidate).encode(), ENV)
    assert caught.value.code == "literal_secret"
    assert "must-not-appear-in-error" not in str(caught.value)


def test_alias_name_collision_is_rejected_before_model_resolution():
    candidate = source_config()
    candidate["plugins"]["llm"]["models"][1]["alias"] = "alpha"
    with pytest.raises(ConfigValidationError) as caught:
        validate_candidate(json.dumps(candidate).encode(), ENV)
    assert caught.value.code == "duplicate_model"


def test_missing_secret_and_empty_model_catalog_cannot_be_applied():
    candidate = source_config()
    with pytest.raises(ConfigValidationError) as missing:
        validate_candidate(json.dumps(candidate).encode(), {})
    assert missing.value.code == "missing_environment"
    candidate["plugins"]["llm"]["models"] = []
    with pytest.raises(ConfigValidationError):
        validate_candidate(json.dumps(candidate).encode(), ENV)


def test_non_sensitive_template_defaults_remain_supported():
    candidate = source_config()
    candidate["plugins"]["llm"]["prompt"] = "${{ env.OPTIONAL:-Ready }}"
    assert validate_candidate(json.dumps(candidate).encode(), ENV) == candidate


def test_optional_get_references_survive_form_round_trips():
    source = source_config()
    source["plugins"]["llm"]["api_key"] = "${{ env.get('GLOBAL_KEY', '') }}"
    source["plugins"]["llm_chat"] = {"exa_api_key": "${{ env.get('OPTIONAL', '') }}"}
    candidate = deepcopy(source)
    candidate["plugins"]["llm"]["api_key"] = ENV["GLOBAL_KEY"]
    assert persisted_config(candidate, source) == source


def test_get_default_does_not_override_an_existing_empty_environment_value():
    source = source_config()
    source["plugins"]["llm"]["prompt"] = "${{ env.get('OPTIONAL', 'fallback') }}"
    candidate = deepcopy(source)
    candidate["plugins"]["llm"]["prompt"] = "fallback"
    result = json.loads(json.dumps(prepare_config(candidate, source, {**ENV, "OPTIONAL": ""})))
    assert result["plugins"]["llm"]["prompt"] == "fallback"


def test_optional_lookup_cannot_hide_a_literal_credential_default():
    candidate = source_config()
    candidate["plugins"]["llm_chat"] = {"exa_api_key": "${{ env.get('OPTIONAL', 'hidden-credential') }}"}
    with pytest.raises(ConfigValidationError) as caught:
        validate_candidate(json.dumps(candidate).encode(), ENV)
    assert caught.value.code == "literal_secret"
    assert "hidden-credential" not in str(caught.value)
