"""Async client for Hevy's public API.

This module owns every HTTP concern: the ``api-key`` header, timeouts, retry
with jittered backoff, and mapping status codes to the domain errors below.
"""

import asyncio
import logging
import random
from typing import Any

import httpx
from pydantic import SecretStr

from hevy_mcp.models import UserInfo

logger = logging.getLogger(__name__)


class HevyError(Exception):
    """Base for errors that tools report to the MCP client as-is."""


class HevyAuthError(HevyError):
    def __init__(self, status: int) -> None:
        super().__init__(
            f"Hevy rejected the API key (HTTP {status}). Hevy's API is only available "
            "with an active Hevy Pro subscription; check the subscription and the key "
            "at https://hevy.com/settings?developer."
        )


class HevyNotFound(HevyError):
    def __init__(self, what: str) -> None:
        super().__init__(f"Not found: {what} does not exist in Hevy.")


class HevyUnavailable(HevyError):
    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(
            f"Hevy is unavailable right now ({reason}, after {attempts} attempt"
            f"{'s' if attempts != 1 else ''}). Try again later."
        )


class HevyRequestError(HevyError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Hevy rejected the request (HTTP {status}): {detail}")


def _is_transient(status: int) -> bool:
    return status == 429 or status >= 500


class HevyClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: SecretStr,
        *,
        max_retries: int,
        backoff_seconds: float,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds

    async def _get(self, path: str, *, what: str, params: dict[str, Any] | None = None) -> Any:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.get(
                    path,
                    params=params,
                    headers={"api-key": self._api_key.get_secret_value()},
                )
            except httpx.TimeoutException:
                reason = "request timed out"
            except httpx.TransportError as exc:
                reason = f"connection failed: {type(exc).__name__}"
            else:
                status = response.status_code
                if response.is_success:
                    return response.json()
                if status in (401, 403):
                    raise HevyAuthError(status)
                if status == 404:
                    raise HevyNotFound(what)
                if not _is_transient(status):
                    raise HevyRequestError(status, _error_detail(response))
                reason = f"HTTP {status}"

            if attempt < attempts:
                # Full jitter: a random delay up to an exponentially growing cap.
                delay = random.uniform(0, self._backoff_seconds * 2 ** (attempt - 1))
                logger.warning(
                    "Hevy GET %s failed (%s); retry %d of %d in %.2fs",
                    path,
                    reason,
                    attempt,
                    self._max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
        raise HevyUnavailable(reason, attempts)

    async def user_info(self) -> UserInfo:
        body = await self._get("/v1/user/info", what="the Hevy user")
        return UserInfo.model_validate(body.get("data", body))


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "no detail"
    detail = body.get("error") if isinstance(body, dict) else None
    return str(detail or body)[:300]
