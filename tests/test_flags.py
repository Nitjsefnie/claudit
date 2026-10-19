from __future__ import annotations

import pickle
import subprocess
from pathlib import Path
from typing import MutableMapping, cast

import pytest

from backend import flags, hide_rate_limits
from backend.flag_registry import (
    COLLECTION_SPECS,
    FEATURE_SPECS,
    CollectionSpec,
    FeatureSpec,
)
from backend.flags import ALL_COLLECTIONS_ON, resolve_config


EXPECTED_COLLECTION_ENVS = {
    key: "CLAUDIT_COLLECT_" + key.upper() for key in (
        "turn_context", "request_metadata", "cache_write_tokens", "context_intake",
        "error_text", "file_extras", "tool_error_kinds", "dispatch_analytics", "generation_speed",
    )
}

EXPECTED_FEATURE_ENVS = {
    key: "CLAUDIT_FEATURE_" + key.upper() for key in (
        "usage_timeseries", "thinking_output", "line_churn", "model_comparison",
        "token_breakdown", "project_comparison", "cache_ttl", "response_sizes",
        "context_growth", "burn_rate", "context_comparison", "tool_usage", "reply_latency",
        "page_performance", "tool_error_rate", "agent_comparison", "activity_heatmap",
        "summary", "rate_limits", "sessions", "cache", "inspector", "project_picker",
        "export_png", "live_updates",
    )
}


def test_defaults_reproduce_all_on():
    config = resolve_config({})

    assert {spec.key: spec.env for spec in COLLECTION_SPECS} == (
        EXPECTED_COLLECTION_ENVS
    )
    assert {spec.key: spec.env for spec in FEATURE_SPECS} == EXPECTED_FEATURE_ENVS
    assert all(config.collection.enabled(key) for key in EXPECTED_COLLECTION_ENVS)
    assert isinstance(config.collection.stamp(), tuple)
    assert not config.collection.stamp()
    assert dict(config.features) == {key: True for key in EXPECTED_FEATURE_ENVS}
    assert isinstance(config.disabled_features(), tuple)
    assert not config.disabled_features()
    assert config.legacy_hide_rate_limits is False
    with pytest.raises(TypeError):
        cast(MutableMapping[str, bool], config.features)["summary"] = False

    collection_disabled = resolve_config({"CLAUDIT_COLLECT_TURN_CONTEXT": "0"})
    assert collection_disabled.collection.stamp() == ("turn_context",)
    assert not collection_disabled.disabled_features()

    feature_disabled = resolve_config({"CLAUDIT_FEATURE_SUMMARY": "0"})
    assert not feature_disabled.collection.stamp()
    assert feature_disabled.disabled_features() == ("summary",)


@pytest.mark.parametrize('value, expected, hide, hidden, visible', [
    pytest.param(None, True, None, False, True, id='unset'),
    pytest.param('', False, '', False, False, id='empty'),
    pytest.param('0', False, '0', False, False, id='zero'),
    pytest.param('false', False, 'false', False, False, id='false'),
    pytest.param('FALSE', False, 'FALSE', False, False, id='uppercase-false'),
    pytest.param('no', False, 'no', False, False, id='no'),
    pytest.param('No', False, 'No', False, False, id='mixed-no'),
    pytest.param('1', True, '1', True, False, id='one'),
    pytest.param('true', True, 'true', True, False, id='true'),
    pytest.param('YES', True, 'YES', True, False, id='yes'),
    pytest.param('off', True, 'off', True, False, id='off'),
    pytest.param('OFF', True, 'OFF', True, False, id='uppercase-off'),
    pytest.param(' ', True, ' ', True, False, id='space'),
    pytest.param('\t', True, '\t', True, False, id='tab'),
    pytest.param(' false', True, ' false', True, False, id='padded-false'),
    pytest.param('1', True, None, False, True, id='positive-without-alias'),
    pytest.param('1', True, 'false', False, True, id='false-alias'),
])
def test_positive_flags_and_legacy_hide_truth_table(value, expected, hide, hidden, visible, monkeypatch):
    env = {} if value is None else {
        "CLAUDIT_COLLECT_TURN_CONTEXT": value,
        "CLAUDIT_COLLECT_GENERATION_SPEED": value,
        "CLAUDIT_FEATURE_RATE_LIMITS": value,
    }
    if hide is not None:
        env["CLAUDIT_HIDE_RATE_LIMITS"] = hide
    config = resolve_config(env)

    assert config.collection.enabled("turn_context") is expected
    assert config.collection.enabled("generation_speed") is expected
    assert config.legacy_hide_rate_limits is hidden
    assert config.features["rate_limits"] is visible
    monkeypatch.setattr(flags, "CONFIG", config)
    assert hide_rate_limits.HIDE_RATE_LIMITS is config.legacy_hide_rate_limits

    positive_only = {} if value is None else {"CLAUDIT_FEATURE_RATE_LIMITS": value}
    assert resolve_config(positive_only).features["rate_limits"] is expected


def test_collection_policy_pickle_and_declarative_extension():
    disabled_policy = resolve_config({
        "CLAUDIT_COLLECT_TURN_CONTEXT": "0",
        "CLAUDIT_COLLECT_ERROR_TEXT": "0",
    }).collection
    restored = pickle.loads(pickle.dumps(disabled_policy))
    assert restored.stamp() == ("error_text", "turn_context")
    assert restored.enabled("turn_context") is False
    assert restored.enabled("error_text") is False
    assert restored.enabled("request_metadata") is True
    assert not ALL_COLLECTIONS_ON.stamp()
    assert ALL_COLLECTIONS_ON.enabled("turn_context") is True

    synthetic = CollectionSpec(
        key="synthetic_speed",
        env="TEST_COLLECT_SYNTHETIC_SPEED",
        record_fields=("speed_tokens",),
        tool_fields=("speed_milliseconds",),
        file_fields=("speed_samples",),
    )
    extended_specs = COLLECTION_SPECS + (synthetic,)
    enabled = resolve_config({}, collection_specs=extended_specs).collection
    assert enabled.enabled("synthetic_speed") is True
    assert enabled.specs[-1] == synthetic

    disabled = resolve_config(
        {"TEST_COLLECT_SYNTHETIC_SPEED": "0"},
        collection_specs=extended_specs,
    ).collection
    assert disabled.enabled("synthetic_speed") is False
    assert disabled.stamp() == ("synthetic_speed",)
    assert disabled.specs[-1].record_fields == ("speed_tokens",)
    assert disabled.specs[-1].tool_fields == ("speed_milliseconds",)
    assert disabled.specs[-1].file_fields == ("speed_samples",)

    with pytest.raises(KeyError, match="Unknown collection key"):
        disabled.enabled("unregistered")


def test_feature_dependencies_are_exclusive():
    speed_feature = FeatureSpec(
        key="speed_only",
        env="TEST_FEATURE_SPEED_ONLY",
        collection_dependencies=("generation_speed",),
    )
    extended_features = FEATURE_SPECS + (speed_feature,)
    speed_on = resolve_config({}, feature_specs=extended_features)
    assert speed_on.collection.enabled("generation_speed") is True
    assert speed_on.features["speed_only"] is True

    speed_off = resolve_config(
        {"CLAUDIT_COLLECT_GENERATION_SPEED": "0"},
        feature_specs=extended_features,
    )
    assert speed_off.collection.enabled("generation_speed") is False
    assert speed_off.features["speed_only"] is False

    independent_groups = (
        ("file_extras", "context_growth", "CLAUDIT_COLLECT_FILE_EXTRAS"),
        ("cache_write_tokens", "cache_ttl", "CLAUDIT_COLLECT_CACHE_WRITE_TOKENS"),
        ("tool_error_kinds", "tool_error_rate", "CLAUDIT_COLLECT_TOOL_ERROR_KINDS"),
        ("dispatch_analytics", "agent_comparison", "CLAUDIT_COLLECT_DISPATCH_ANALYTICS"),
    )
    for collection_key, feature_key, env_name in independent_groups:
        on = resolve_config({})
        assert on.collection.enabled(collection_key) is True
        assert on.features[feature_key] is True

        off = resolve_config({env_name: "0"})
        assert off.collection.enabled(collection_key) is False
        assert off.features[feature_key] is True

    broken_dependency = FeatureSpec(
        key="broken_feature",
        env="TEST_BROKEN_FEATURE",
        collection_dependencies=("unregistered_collection",),
    )
    with pytest.raises(KeyError, match="Unknown collection key"):
        resolve_config(
            {"TEST_BROKEN_FEATURE": "0"},
            feature_specs=(broken_dependency,),
        )


def test_bootstrap_helper_defaults_and_known_false():
    root = Path(__file__).resolve().parents[1]
    helper = root / "src" / "feature-flags.js"
    script = """
global.window = {};
require(process.argv[1]);
if (window.featureOn('missing') !== true) throw new Error('missing FEATURES');
window.FEATURES = {known: false};
if (window.featureOn('missing') !== true) throw new Error('missing key');
if (window.featureOn('known') !== false) throw new Error('known false');
"""
    result = subprocess.run(
        ["node", "-e", script, str(helper)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    index_html = (root / "public" / "index.html").read_text()
    helper_tag = '<script src="/src/feature-flags.js"></script>'
    first_consumer = '<script src="/src/parser-cache-write.js"></script>'
    assert index_html.index(helper_tag) < index_html.index(first_consumer)


def test_all_registry_envs_documented_and_compose_passed_through():
    root = Path(__file__).resolve().parents[1]
    env_example = (root / "backend" / ".env.example").read_text()
    compose = (root / "compose.yaml").read_text()
    readme = (root / "README.md").read_text()
    agents = (root / "AGENTS.md").read_text()
    privacy = (root / "PRIVACY.md").read_text()
    registry_envs = {
        spec.env for spec in (*COLLECTION_SPECS, *FEATURE_SPECS)
    } | {"CLAUDIT_HIDE_RATE_LIMITS"}

    for env_name in registry_envs:
        default = "0" if env_name == "CLAUDIT_HIDE_RATE_LIMITS" else "1"
        assert env_name in env_example
        assert f"{env_name}: ${{{env_name}:-{default}}}" in compose
        assert env_name in readme

    for spec in COLLECTION_SPECS:
        assert spec.env in privacy

    assert "CLAUDIT_R2_ENDPOINT: ${CLAUDIT_R2_ENDPOINT-${R2_ENDPOINT-file" in compose
    assert "[`backend/.env.example`](backend/.env.example)" in readme
    assert "PRIVACY.md#collection-switch-inventory" in readme
    assert "README.md#configuration" in agents
    assert "PRIVACY.md#collection-switch-inventory" in agents
