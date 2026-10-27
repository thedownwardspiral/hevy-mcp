"""Settings read from the environment and validated at startup."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Local-development override: when set, Vault is skipped.
    hevy_api_key: SecretStr | None = None
    hevy_base_url: str = "https://api.hevyapp.com"
    hevy_timeout_seconds: float = Field(10.0, gt=0)
    hevy_max_retries: int = Field(3, ge=0)
    hevy_retry_backoff_seconds: float = Field(0.5, ge=0)

    log_level: str = "INFO"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    tz: str = "UTC"

    @field_validator("tz")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)
