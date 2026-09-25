"""Tool catalog, strict schemas and provider/SDK settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.tracing import get_trace_provider
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from hotel_operations.agent.runtime import (
    ProviderNotConfigured,
    build_model_settings,
    build_openai_model,
    build_run_config,
    configure_tracing,
)
from hotel_operations.app import create_app
from hotel_operations.config import Settings
from hotel_operations.tools.gateway import _sanitize_args
from hotel_operations.tools.registry import active_tool_specs
from hotel_operations.tools.reservation import NoArguments
from tests.conftest import make_settings


def _settings(**kw: object) -> Settings:
    values: dict[str, object] = {"OPENAI_API_KEY": None, **kw}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_the_catalog_has_no_approval_generic_update_or_lookup_tool() -> None:
    specs = active_tool_specs()
    names = {s.name for s in specs}
    assert "get_my_reservation" in names
    forbidden = {
        "update_checkout_time",
        "approve_request",
        "reject_request",
        "check_next_reservation",
        "get_guest_profile",
        "run_sql",
    }
    assert not names & forbidden


def test_empty_input_schema_is_strict() -> None:
    spec = next(s for s in active_tool_specs() if s.name == "get_my_reservation")
    schema = spec.json_schema()
    assert schema["type"] == "object"
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    assert schema["required"] == []


def test_audited_arguments_keep_short_lists_but_never_free_text() -> None:
    topics = json.dumps({"topics": ["breakfast", "parking"]})
    assert _sanitize_args(topics) == {"topics": ["breakfast", "parking"]}
    # Free text stays redacted inside a list, and nothing deeper or longer is kept.
    assert _sanitize_args(json.dumps({"notes": ["call me"]})) == {
        "notes": [{"redacted_text_length": 7}]
    }
    assert _sanitize_args(json.dumps({"topics": [["x"]]})) == {"topics": [{"non_scalar": "list"}]}
    assert _sanitize_args(json.dumps({"topics": ["x"] * 13})) == {"topics": {"non_scalar": "list"}}


def test_item_count_bounds_stay_out_of_the_provider_schema() -> None:
    spec = next(s for s in active_tool_specs() if s.name == "get_hotel_policy")
    topics = spec.json_schema()["properties"]["topics"]
    assert "minItems" not in topics and "maxItems" not in topics  # enforced at runtime


@pytest.mark.parametrize(
    "raw",
    ['{"reservation_id": "rsv_1088"}', '{"guest_id": "G-002"}', '{"reference": "R-1088"}'],
)
def test_arbitrary_target_keys_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        NoArguments.model_validate_json(raw)


def test_model_settings_match_provider_contract() -> None:
    ms = build_model_settings(_settings())
    assert ms.parallel_tool_calls is False
    assert ms.store is False
    assert ms.max_tokens == 2048
    assert ms.reasoning is not None and ms.reasoning.effort == "low"
    assert ms.response_include == ["reasoning.encrypted_content"]
    assert ms.retry is None  # the OpenAI client is the only retry owner


def test_tracing_is_disabled_by_default() -> None:
    settings = _settings()
    configure_tracing(settings)
    assert get_trace_provider()._disabled is True  # noqa: SLF001
    rc = build_run_config(settings)
    assert rc.tracing_disabled is True
    assert rc.trace_include_sensitive_data is False


def test_opt_in_tracing_keeps_sensitive_capture_off() -> None:
    settings = _settings(HOTEL_TRACING_ENABLED=True)
    rc = build_run_config(settings)
    assert rc.tracing_disabled is False
    assert rc.trace_include_sensitive_data is False
    configure_tracing(_settings())  # restore default


def test_opt_in_tracing_hands_the_settings_key_to_the_trace_exporter() -> None:
    """The key lives in the settings file, not os.environ, where the SDK exporter looks."""
    from agents.tracing.processors import default_exporter

    exporter = default_exporter()
    before = exporter._api_key  # noqa: SLF001
    try:
        configure_tracing(_settings(HOTEL_TRACING_ENABLED=True, OPENAI_API_KEY="sk-test-fake"))
        assert exporter._api_key == "sk-test-fake"  # noqa: SLF001
    finally:
        exporter._api_key = before  # noqa: SLF001
        configure_tracing(_settings())  # restore default


def test_storing_responses_is_an_explicit_opt_in_that_keeps_history_local() -> None:
    stored = build_model_settings(_settings(HOTEL_RESPONSES_STORE=True))
    assert stored.store is True
    # Still no provider-side history: the SDK session stays the only history.
    rc = build_run_config(_settings(HOTEL_RESPONSES_STORE=True))
    assert rc.model_settings is not None and rc.model_settings.store is True
    assert build_model_settings(_settings()).store is False


def test_sensitive_trace_capture_needs_both_opt_ins() -> None:
    both = _settings(HOTEL_TRACING_ENABLED=True, HOTEL_TRACING_SENSITIVE_DATA=True)
    assert build_run_config(both).trace_include_sensitive_data is True
    # Without tracing there is no trace to put the data in.
    alone = _settings(HOTEL_TRACING_SENSITIVE_DATA=True)
    assert build_run_config(alone).tracing_disabled is True
    assert build_run_config(alone).trace_include_sensitive_data is False


def test_missing_key_is_explicit_not_a_fallback() -> None:
    with pytest.raises(ProviderNotConfigured):
        build_openai_model(_settings())


def test_openai_client_retry_and_timeout_bounds() -> None:
    settings = _settings(OPENAI_API_KEY="sk-test-not-real")
    model = build_openai_model(settings)
    client = model._client  # type: ignore[attr-defined]  # noqa: SLF001
    assert client.max_retries == 2
    assert client.timeout == 45.0


def test_the_app_closes_its_openai_client_on_shutdown(tmp_path: Path, db_path: Path) -> None:
    """Closed while the app's event loop still runs, not garbage-collected after it ended."""
    settings = make_settings(tmp_path, db_path, openai_api_key=SecretStr("sk-test-not-real"))
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1"):
        factory = app.state.services.model_factory
        assert factory() is factory()  # one model and client per app; nothing is sent
        client = factory.client
        assert client is not None and not client.is_closed()
    assert client.is_closed()
    assert factory.client is None
