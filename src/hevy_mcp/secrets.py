"""Resolves the Hevy API key once at startup."""

import httpx
from pydantic import SecretStr

from hevy_mcp.config import Settings


class SecretSourceError(Exception):
    """The API key could not be resolved. Messages never contain credentials."""


async def resolve_api_key(
    settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> SecretStr:
    if settings.hevy_api_key is not None:
        return settings.hevy_api_key
    raise SecretSourceError("HEVY_API_KEY is not set.")
