"""Refuses requests whose Host header names a host the server doesn't serve.

With ``MCP_ALLOWED_HOSTS`` set, a request for any other host gets 421 before
the Access check or any tool runs. This blocks DNS-rebinding attacks and
traffic that reaches the container some other way, such as a peer on the
Docker network connecting to it directly.

Loopback names are allowed only on /health, for the container's own health
check. Elsewhere they'd let that same peer in just by sending
``Host: localhost``, so the check goes by the Host header alone and never by
the connection's addresses.
"""

import logging
from ipaddress import ip_address

from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from hevy_mcp.config import Settings

logger = logging.getLogger(__name__)

HEALTH_PATH = "/health"


def hostname(host: str) -> str:
    """The Host header's hostname: lowercased, without the port or IPv6 brackets."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.partition(":")[0]


def is_loopback(name: str) -> bool:
    if name == "localhost":
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


class AllowedHostsMiddleware:
    """Answers 421 to requests for hosts outside ``allowed_hosts``."""

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        name = hostname(Headers(scope=scope).get("host", ""))
        if name in self.allowed_hosts or (scope["path"] == HEALTH_PATH and is_loopback(name)):
            await self.app(scope, receive, send)
            return

        logger.warning("Refused %s for unknown host %r", scope["path"], name)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"error": "Unknown host."}, status_code=421)
        await response(scope, receive, send)


def allowed_hosts_middleware(settings: Settings) -> list[Middleware]:
    """The HTTP middleware that enforces ``MCP_ALLOWED_HOSTS``, or none when it's unset."""
    if not settings.mcp_allowed_hosts:
        return []
    return [Middleware(AllowedHostsMiddleware, allowed_hosts=settings.mcp_allowed_hosts)]
