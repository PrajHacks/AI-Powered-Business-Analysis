from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_scratch_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "tmp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AI_BUSINESS_ANALYST_",
        extra="ignore",
    )

    scratch_data_dir: Path = Field(default_factory=_default_scratch_dir)
    max_csv_upload_size_mb: int = Field(default=50, ge=1)
    query_timeout_seconds: int = Field(default=15, ge=1)
    query_max_rows: int = Field(default=1000, ge=1)
    schema_cache_ttl_seconds: int = Field(default=600, ge=1)
    schema_row_count_sample_limit: int = Field(default=1_000, ge=1)
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.2:3b")
    ollama_timeout_seconds: int = Field(default=120, ge=1)
    sql_generation_max_attempts: int = Field(default=2, ge=1)
    conversation_max_turns: int = Field(default=10, ge=1)
    conversation_ttl_minutes: int = Field(default=30, ge=1)
    conversation_prompt_window: int = Field(default=4, ge=1)
    feedback_few_shot_limit: int = Field(default=5, ge=1)

    def ensure_directories(self) -> Path:
        self.scratch_data_dir.mkdir(parents=True, exist_ok=True)
        return self.scratch_data_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
