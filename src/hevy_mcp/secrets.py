"""Resolves the Hevy API key once at startup.

``HEVY_API_KEY`` wins if set (local development). Otherwise the key is read
from Vault: an AppRole login, then a KV v2 read of the ``api_key`` field.
Both calls go through the same injectable transport as Hevy's.
"""

import ssl
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from hevy_mcp.config import Settings

API_KEY_FIELD = "api_key"


class SecretSourceError(Exception):
    """The API key could not be resolved. Messages never contain credentials."""


async def resolve_api_key(
    settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> SecretStr:
    # A blank HEVY_API_KEY (e.g. "HEVY_API_KEY=" left in an env file) counts as unset.
    override = settings.hevy_api_key.get_secret_value().strip() if settings.hevy_api_key else ""
    if override:
        return SecretStr(override)
    if not settings.vault_addr:
        raise SecretSourceError(
            "No Hevy API key: set VAULT_ADDR and the AppRole credentials to read it from "
            "Vault, or HEVY_API_KEY for local development."
        )
    return await _read_from_vault(settings, transport)


def _credential(value: SecretStr | None, file: Path | None, name: str) -> str:
    if value is not None:
        return value.get_secret_value()
    if file is not None:
        try:
            return file.read_text().strip()
        except OSError as exc:
            raise SecretSourceError(
                f"Could not read {name}_FILE ({file}): {exc.strerror}."
            ) from None
    raise SecretSourceError(f"Set {name} or {name}_FILE to log in to Vault with AppRole.")


def _tls(cacert: Path | None) -> ssl.SSLContext:
    """Verify Vault's certificate, against the internal CA bundle if one is given."""
    try:
        return ssl.create_default_context(cafile=cacert)
    except (OSError, ssl.SSLError) as exc:
        raise SecretSourceError(
            f"Could not load the CA bundle in VAULT_CACERT ({cacert}): {exc}."
        ) from None


async def _read_from_vault(
    settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> SecretStr:
    addr = settings.vault_addr
    role_id = _credential(settings.vault_role_id, settings.vault_role_id_file, "VAULT_ROLE_ID")
    secret_id = _credential(
        settings.vault_secret_id, settings.vault_secret_id_file, "VAULT_SECRET_ID"
    )
    secret = f"{settings.vault_kv_mount}/{settings.vault_kv_path}"

    async with httpx.AsyncClient(
        base_url=addr,
        verify=_tls(settings.vault_cacert),
        timeout=settings.vault_timeout_seconds,
        transport=transport,
    ) as vault:
        login = await _call(
            vault,
            "POST",
            f"/v1/auth/{settings.vault_approle_mount}/login",
            addr=addr,
            action="the AppRole login",
            json={"role_id": role_id, "secret_id": secret_id},
        )
        if login.status_code in (400, 401, 403):
            raise SecretSourceError(
                f"Vault at {addr} denied the AppRole login (HTTP {login.status_code}): "
                "check the role ID, the secret ID and its TTL."
            )
        token = _object(_json(login, addr, "the AppRole login"), "auth").get("client_token")
        if not login.is_success or not isinstance(token, str) or not token:
            raise SecretSourceError(
                f"The AppRole login at Vault {addr} failed (HTTP {login.status_code})."
            )

        read = await _call(
            vault,
            "GET",
            f"/v1/{settings.vault_kv_mount}/data/{settings.vault_kv_path}",
            addr=addr,
            action=f"reading {secret}",
            headers={"X-Vault-Token": token},
        )
        if read.status_code == 404:
            raise SecretSourceError(f"There is no KV v2 secret at {secret} in Vault at {addr}.")
        if read.status_code == 403:
            raise SecretSourceError(
                f"Vault at {addr} returned permission denied for {secret}: the AppRole's "
                "policy needs read on that path."
            )
        if not read.is_success:
            raise SecretSourceError(
                f"Reading {secret} from Vault at {addr} failed (HTTP {read.status_code})."
            )
        data = _object(_object(_json(read, addr, f"reading {secret}"), "data"), "data")
        api_key = data.get(API_KEY_FIELD)
        if not isinstance(api_key, str) or not api_key:
            raise SecretSourceError(
                f"The secret {secret} in Vault at {addr} has no {API_KEY_FIELD!r} field."
            )
        return SecretStr(api_key)


async def _call(
    vault: httpx.AsyncClient, method: str, path: str, *, addr: str, action: str, **kwargs: Any
) -> httpx.Response:
    try:
        return await vault.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise SecretSourceError(
            f"Could not reach Vault at {addr} for {action}: {type(exc).__name__}."
        ) from None


def _json(response: httpx.Response, addr: str, action: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SecretSourceError(f"Vault at {addr} sent an unexpected response to {action}.")
    return body


def _object(body: dict[str, Any], key: str) -> dict[str, Any]:
    """``body[key]`` if it is an object, else empty: Vault may send ``null``."""
    value = body.get(key)
    return value if isinstance(value, dict) else {}
