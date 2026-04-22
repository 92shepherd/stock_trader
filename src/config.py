"""Central configuration loader.

Reads from:
    1. Environment variables (via .env)
    2. config/settings.yaml
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DBSettings(BaseSettings):
    """Database connection settings from .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "stockdata"
    db_user: str = "stock"
    db_password: str = "changeme"

    @property
    def sync_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def psycopg_dsn(self) -> str:
        """Plain psycopg3 DSN (for COPY operations)."""
        return (
            f"host={self.db_host} port={self.db_port} "
            f"dbname={self.db_name} user={self.db_user} "
            f"password={self.db_password}"
        )


class KISSettings(BaseSettings):
    """KIS OpenAPI credentials — filled later after account setup."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    kis_account_product: str = "01"
    kis_mode: str = "paper"  # paper | real


class DailyConfig(BaseModel):
    backfill_days: int = 400
    request_delay: float = 0.3
    max_retries: int = 3
    retry_delay: float = 2.0


class MinuteConfig(BaseModel):
    request_delay: float = 0.15
    max_retries: int = 3
    retry_delay: float = 1.5
    tier1_count: int = 300
    tier2_count: int = 1000


class CollectionConfig(BaseModel):
    daily: DailyConfig = Field(default_factory=DailyConfig)
    minute: MinuteConfig = Field(default_factory=MinuteConfig)


class AppConfig(BaseModel):
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    markets: list[str] = Field(default_factory=lambda: ["KOSPI", "KOSDAQ"])
    exclude_patterns: list[str] = Field(default_factory=list)


@lru_cache
def get_db_settings() -> DBSettings:
    return DBSettings()


@lru_cache
def get_kis_settings() -> KISSettings:
    return KISSettings()


@lru_cache
def get_app_config() -> AppConfig:
    yaml_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not yaml_path.exists():
        return AppConfig()
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig(**data)
