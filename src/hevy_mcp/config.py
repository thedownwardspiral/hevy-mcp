"""Settings read from the environment and validated at startup."""

from pathlib import Path
from typing import Annotated, Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # Hidden inputs: a validation error would otherwise print every setting,
    # credentials included.
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    # Local-development override: when set, Vault is skipped.
    hevy_api_key: SecretStr | None = None
    hevy_base_url: str = "https://api.hevyapp.com"
    hevy_timeout_seconds: float = Field(10.0, gt=0)
    hevy_max_retries: int = Field(3, ge=0)
    hevy_retry_backoff_seconds: float = Field(0.5, ge=0)
    # Most pages a range query walks before it stops and reports truncation.
    hevy_range_max_pages: int = Field(50, ge=1)
    hevy_template_cache_seconds: float = Field(600, ge=0)

    # Vault, used when HEVY_API_KEY is not set. VAULT_ADDR has no default so
    # that no real hostname is committed. Each AppRole credential can instead
    # be read from a file (the _FILE variant), for Docker secrets.
    vault_addr: str | None = None
    vault_role_id: SecretStr | None = None
    vault_role_id_file: Path | None = None
    vault_secret_id: SecretStr | None = None
    vault_secret_id_file: Path | None = None
    vault_cacert: Path | None = None
    vault_approle_mount: str = "approle"
    vault_kv_mount: str = "secret"
    vault_kv_path: str = "hevy-mcp"
    vault_timeout_seconds: float = Field(10.0, gt=0)

    # Cloudflare Access. With these set, every request except /health needs a
    # valid Access JWT; without them the server is unauthenticated. They go
    # together: setting only some of them stops startup.
    cf_access_team_domain: str | None = None
    cf_access_aud: str | None = None
    cf_access_allowed_emails: Annotated[frozenset[str], NoDecode] = frozenset()

    log_level: str = "INFO"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    # Hostnames the server answers for (comma-separated). When set, requests
    # with any other Host header are refused; loopback is always allowed, for
    # the container's health check. Unset accepts any host.
    mcp_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
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

    @field_validator("cf_access_team_domain", "cf_access_aud", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("cf_access_team_domain")
    @classmethod
    def _team_hostname(cls, value: str | None) -> str | None:
        """Accept ``team.cloudflareaccess.com`` or its ``https://`` URL."""
        if value is None:
            return None
        host = value.lower().removeprefix("https://").rstrip("/")
        if not host or "/" in host or ":" in host:
            raise ValueError(f"expected a hostname such as <team>.cloudflareaccess.com: {value!r}")
        return host

    @field_validator("cf_access_allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: Any) -> Any:
        """A comma-separated list, compared without regard to case."""
        if isinstance(value, str):
            value = value.split(",")
        return frozenset(email.strip().lower() for email in value if email.strip())

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        return tuple(host.strip().lower() for host in value if host.strip())

    @field_validator("mcp_allowed_hosts")
    @classmethod
    def _hostnames_only(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        for host in hosts:
            if "/" in host or host == "*":
                raise ValueError(
                    f"MCP_ALLOWED_HOSTS takes hostnames such as hevy-mcp.example.com, "
                    f"not URLs or a bare '*': {host!r}"
                )
        return hosts

    @model_validator(mode="after")
    def _access_settings_together(self) -> Self:
        given = {
            "CF_ACCESS_TEAM_DOMAIN": self.cf_access_team_domain is not None,
            "CF_ACCESS_AUD": self.cf_access_aud is not None,
            "CF_ACCESS_ALLOWED_EMAILS": bool(self.cf_access_allowed_emails),
        }
        missing = [name for name, present in given.items() if not present]
        if missing and len(missing) < len(given):
            raise ValueError(
                "Cloudflare Access needs CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD and "
                f"CF_ACCESS_ALLOWED_EMAILS together; missing: {', '.join(missing)}."
            )
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)
