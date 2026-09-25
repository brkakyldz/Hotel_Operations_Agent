"""Application settings.

The OpenAI key comes only from the process environment or the gitignored root
secrets file (``.env``). This module is the only place that loads it; nothing
prints, logs or persists the value.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

SIMULATION_LABEL = (
    "Simulation: fictional hotel, guests and operations. No real staff are contacted."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-6-luna", alias="OPENAI_MODEL")
    openai_reasoning_effort: str = Field(default="low", alias="OPENAI_REASONING_EFFORT")

    hotel_db_path: Path = Field(default=REPO_ROOT / "data" / "hotel.sqlite", alias="HOTEL_DB_PATH")
    conversations_db_path: Path = Field(
        default=REPO_ROOT / "data" / "conversations.sqlite", alias="CONVERSATIONS_DB_PATH"
    )

    # Local structured telemetry (JSON lines, scalars only). Empty value disables the file.
    telemetry_log_path: Path | None = Field(
        default=REPO_ROOT / "data" / "telemetry.jsonl", alias="HOTEL_TELEMETRY_LOG"
    )

    @field_validator("telemetry_log_path", mode="before")
    @classmethod
    def _empty_disables_telemetry_file(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # Remote SDK tracing is off unless explicitly enabled. Model and
    # tool inputs/outputs are captured only when both switches are on.
    tracing_enabled: bool = Field(default=False, alias="HOTEL_TRACING_ENABLED")
    tracing_sensitive_data: bool = Field(default=False, alias="HOTEL_TRACING_SENSITIVE_DATA")
    # Keep each model response on the provider (30 days) so the trace view can open it.
    # History still comes only from the local SDK session.
    responses_store: bool = Field(default=False, alias="HOTEL_RESPONSES_STORE")

    ui_origin: str = Field(default="http://127.0.0.1:5173", alias="HOTEL_UI_ORIGIN")

    # Workload bounds.
    run_deadline_seconds: float = 90.0
    provider_timeout_seconds: float = 45.0
    provider_max_retries: int = 2
    max_turns: int = 6
    max_tool_proposals: int = 8
    max_output_tokens: int = 2048
    max_user_input_chars: int = 4000
    # Sized for the largest legitimate result: every policy topic in one read, about 4,300
    # characters at fixture v1.
    max_tool_projection_chars: int = 8000
    tool_deadline_seconds: float = 3.0
    sqlite_busy_timeout_ms: int = 500
    max_active_runs_per_session: int = 1
    max_active_runs_global: int = 2
    # A conversation has no turn cap and a session no wall-clock lifetime: the
    # model input is trimmed to the character budget below at user-message boundaries.
    # Conservative estimated model-input ceiling in characters (~4 chars/token estimate).
    max_estimated_input_chars: int = 100_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
