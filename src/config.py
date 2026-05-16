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


class FnguideSettings(BaseSettings):
    """FnGuide 스크래퍼 설정 — 개인용 비공개 한정.

    consent_ack 가 '1' 이 아니면 src/collectors/consensus_fnguide.py 의
    _consent_check() 가 RuntimeError 로 거부한다. 자세한 정책은 해당
    모듈의 docstring 참고.

    이 설정을 raw `os.environ.get()` 이 아니라 pydantic-settings 로
    로딩하는 이유: 다른 설정들과 동일한 경로로 읽혀서, load_dotenv()
    호출 순서나 세션 환경변수 잔재에 구애받지 않음. .env 의 keys 적재
        FNGUIDE_CONSENT_ACK=1
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fnguide_consent_ack: str = ""

    @property
    def consent_ack(self) -> bool:
        return self.fnguide_consent_ack.strip() == "1"


class DartSettings(BaseSettings):
    """DART OpenAPI credentials & policy.

    api_key comes from .env (DART_API_KEY). Get one at:
        https://opendart.fss.or.kr/  →  인증키 신청/관리

    Personal-use limit: 10,000 calls/day. Plenty for this project's
    pattern (1 corp_codes refresh + ~10 disclosure-list calls per day).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dart_api_key: str = ""
    # Refresh dart_corp_codes if older than this many days. Stock_codes
    # rarely change, but new IPOs need a fresh pull to be discoverable.
    dart_corp_codes_stale_after_days: int = 7

    # Compatibility aliases so callers can write `cfg.dart.api_key`
    # rather than the env-var-style `cfg.dart.dart_api_key`.
    @property
    def api_key(self) -> str:
        return self.dart_api_key

    @property
    def corp_codes_stale_after_days(self) -> int:
        return self.dart_corp_codes_stale_after_days


class HankyungSettings(BaseSettings):
    """한경 컨센서스 수집기 활성화 설정.

    HANKYUNG_ENABLED=true (또는 1/yes) 로 설정해야 수집기가 동작.
    docker-compose.gcp-db.yml 에서만 활성화됨.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hankyung_enabled: str = ""

    @property
    def enabled(self) -> bool:
        return self.hankyung_enabled.strip().lower() in ("1", "true", "yes", "y", "on")


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
    # DART settings live under cfg.dart for namespacing alongside cfg.collection.
    # Loaded from .env (not settings.yaml) because it contains the API key.
    dart: DartSettings = Field(default_factory=DartSettings)


@lru_cache
def get_db_settings() -> DBSettings:
    return DBSettings()


@lru_cache
def get_kis_settings() -> KISSettings:
    return KISSettings()


@lru_cache
def get_fnguide_settings() -> FnguideSettings:
    return FnguideSettings()


@lru_cache
def get_hankyung_settings() -> HankyungSettings:
    return HankyungSettings()


@lru_cache
def get_app_config() -> AppConfig:
    yaml_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not yaml_path.exists():
        return AppConfig()
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # DartSettings reads from .env, not settings.yaml. AppConfig's default
    # factory will pick up env vars, but if the YAML supplies a 'dart' key
    # we still want env to win for the API key (security). Drop any 'dart'
    # block from YAML and let DartSettings load from .env.
    data.pop("dart", None)
    return AppConfig(**data)
