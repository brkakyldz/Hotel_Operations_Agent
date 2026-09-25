"""Provider/SDK configuration for the single Hotel Operations Agent.

Provider and SDK settings: ``store=False`` by default, with encrypted reasoning
carried statelessly, parallel tool calls off, an explicit reasoning effort, a
2,048-token per-call output cap, no ``previous_response_id`` and remote tracing
disabled. The OpenAI client's own retry configuration is the only provider
retry owner (at most two retries); the SDK's opt-in runner retry stays unset.
"""

from __future__ import annotations

from typing import Literal, cast

from agents import (
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    set_tracing_disabled,
    set_tracing_export_api_key,
)
from agents.models.interface import Model
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from hotel_operations.config import Settings

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class ProviderNotConfigured(RuntimeError):
    """No OpenAI key is configured; the product never falls back to canned replies."""


def configure_tracing(settings: Settings) -> None:
    """Remote SDK tracing is off unless explicitly enabled.

    The key comes from the settings file, not the process environment, so the SDK's trace
    exporter (which reads only ``os.environ``) must be handed it; otherwise it skips export.
    """
    set_tracing_disabled(not settings.tracing_enabled)
    key = settings.openai_api_key
    if settings.tracing_enabled and key is not None and key.get_secret_value():
        set_tracing_export_api_key(key.get_secret_value())


def build_model_settings(settings: Settings) -> ModelSettings:
    return ModelSettings(
        parallel_tool_calls=False,
        # Off by default; an explicit opt-in stores responses for the trace view only.
        store=settings.responses_store,
        max_tokens=settings.max_output_tokens,
        reasoning=Reasoning(effort=cast(ReasoningEffort, settings.openai_reasoning_effort)),
        response_include=["reasoning.encrypted_content"],
        truncation="disabled",
    )


def build_run_config(settings: Settings) -> RunConfig:
    return RunConfig(
        model_settings=build_model_settings(settings),
        tracing_disabled=not settings.tracing_enabled,
        # Inputs and outputs reach a trace only by a second, explicit opt-in.
        trace_include_sensitive_data=settings.tracing_enabled and settings.tracing_sensitive_data,
        workflow_name="hotel-operations-agent",
    )


def build_openai_client(settings: Settings) -> AsyncOpenAI:
    key = settings.openai_api_key
    if key is None or not key.get_secret_value():
        raise ProviderNotConfigured("OPENAI_API_KEY is not configured")
    return AsyncOpenAI(
        api_key=key.get_secret_value(),
        timeout=settings.provider_timeout_seconds,
        max_retries=settings.provider_max_retries,
    )


def build_openai_model(settings: Settings, client: AsyncOpenAI | None = None) -> Model:
    client = client or build_openai_client(settings)
    return OpenAIResponsesModel(model=settings.openai_model, openai_client=client)
