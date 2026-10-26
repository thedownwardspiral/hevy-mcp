"""The FastMCP app: tool registration, error mapping and the health route."""

import logging
import sys
from collections.abc import AsyncIterator
from importlib.metadata import version
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from hevy_mcp.config import Settings
from hevy_mcp.hevy import HevyClient, HevyError
from hevy_mcp.models import UserInfo
from hevy_mcp.secrets import SecretSourceError, resolve_api_key

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Read-only access to the user's Hevy workout and body-measurement data.
Weights are in kilograms and distances in metres, as the field names say;
get_user_info returns the units the user prefers to see.
"""


class HevyErrorMiddleware(Middleware):
    """Turns domain errors into tool errors whose message reaches the client.

    FastMCP masks unexpected exceptions into a generic ToolError, keeping the
    original as the cause; Hevy errors carry a safe, user-facing message, so
    they are re-raised with that message instead.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except HevyError as exc:
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            if isinstance(exc.__cause__, HevyError):
                raise ToolError(str(exc.__cause__)) from exc.__cause__
            raise


class _State:
    hevy: HevyClient


def create_server(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """Build the server. ``transport`` replaces the network for Hevy and Vault."""
    settings = settings or Settings()
    state = _State()

    @lifespan
    async def connect(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            api_key = await resolve_api_key(settings, transport)
        except SecretSourceError as exc:
            logger.error("Startup failed: %s", exc)
            raise
        async with httpx.AsyncClient(
            base_url=settings.hevy_base_url,
            timeout=settings.hevy_timeout_seconds,
            transport=transport,
        ) as http:
            state.hevy = HevyClient(
                http,
                api_key,
                max_retries=settings.hevy_max_retries,
                backoff_seconds=settings.hevy_retry_backoff_seconds,
            )
            yield {}

    mcp = FastMCP(
        "hevy",
        version=version("hevy-mcp"),
        instructions=INSTRUCTIONS,
        lifespan=connect,
        middleware=[HevyErrorMiddleware()],
        mask_error_details=True,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool(annotations=READ_ONLY)
    async def get_user_info() -> UserInfo:
        """Get the Hevy user's profile: name, username, profile URL, Pro status and
        preferred weight_unit and distance_unit. Use the preferred units when
        presenting weights (stored in kg) and distances (stored in metres)."""
        return await state.hevy.user_info()

    return mcp


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        stream=sys.stdout,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server(settings).run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path="/mcp",
        show_banner=False,
    )
