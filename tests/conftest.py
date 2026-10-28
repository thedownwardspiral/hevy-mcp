"""Test harness: the real server behind FastMCP's in-memory client, with Hevy
and Vault HTTP served by a fake transport."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hevy_mcp.config import Settings
from hevy_mcp.server import create_server, http_options

FIXTURES = Path(__file__).parent / "fixtures"
HEVY = "https://api.hevyapp.com"
TEST_API_KEY = "00000000-0000-4000-8000-000000000000"
# Where HTTP tests address the server, as Traefik or the tunnel would.
HTTP_BASE = "https://hevy-mcp.example"

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


def load(name: str) -> Any:
    """Load a JSON fixture, e.g. ``load("hevy/user_info.json")``."""
    return json.loads((FIXTURES / name).read_text())


def page(key: str, items: list[Any], page: int = 1, page_count: int = 1) -> dict[str, Any]:
    """A Hevy paginated response body."""
    return {"page": page, "page_count": page_count, key: items}


def workout_at(workout_id: str, start_time: str, end_time: str | None = None) -> dict[str, Any]:
    """The workout fixture with a different id and time."""
    workout = load("hevy/workout.json")
    workout |= {"id": workout_id, "start_time": start_time, "end_time": end_time or start_time}
    return workout


class FakeHTTP:
    """Routes requests to canned responses and records every request.

    A route matches on method, URL (scheme, host and path) and, optionally, a
    subset of query parameters. A route given several replies serves them in
    order and then keeps repeating the last one.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.unmatched: list[httpx.Request] = []
        self._routes: list[tuple[str, str, dict[str, str], list[Reply]]] = []
        self.transport = httpx.MockTransport(self._handle)

    def route(
        self,
        method: str,
        url: str,
        *replies: Reply,
        params: dict[str, Any] | None = None,
        json: Any = None,
        status: int = 200,
    ) -> None:
        if not url.startswith("http"):
            url = HEVY + url
        if not replies:
            replies = (httpx.Response(status, json=json),)
        wanted = {k: str(v) for k, v in (params or {}).items()}
        # Newest routes win, so a test can override a default.
        self._routes.insert(0, (method, url, wanted, list(replies)))

    def get(self, url: str, *replies: Reply, **kwargs: Any) -> None:
        self.route("GET", url, *replies, **kwargs)

    def post(self, url: str, *replies: Reply, **kwargs: Any) -> None:
        self.route("POST", url, *replies, **kwargs)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Match the raw (still percent-encoded) path, as Hevy would see it.
        path = request.url.raw_path.split(b"?")[0].decode()
        url = f"{request.url.scheme}://{request.url.netloc.decode()}{path}"
        query = dict(request.url.params)
        for method, route_url, wanted, replies in self._routes:
            if method == request.method and route_url == url and wanted.items() <= query.items():
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                return reply(request) if callable(reply) else reply
        self.unmatched.append(request)
        return httpx.Response(418, json={"error": f"no fake route for {request.method} {url}"})

    def hevy_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.host == "api.hevyapp.com" and (path is None or r.url.path == path)
        ]


@pytest.fixture
def fake() -> FakeHTTP:
    return FakeHTTP()


@pytest.fixture
def make_client(fake: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Client]:
    """Build a client for a fresh server configured from the given env vars.

    Defaults to the ``HEVY_API_KEY`` override and no retry delay; pass
    ``HEVY_API_KEY=None`` to drop the override.
    """

    def _make(**env: str | None) -> Client:
        for key in list(os.environ):
            prefixed = key.startswith(("HEVY_", "VAULT_", "MCP_", "CF_ACCESS_"))
            if prefixed or key in {"TZ", "LOG_LEVEL"}:
                monkeypatch.delenv(key)
        settings = {"HEVY_API_KEY": TEST_API_KEY, "HEVY_RETRY_BACKOFF_SECONDS": "0"} | env
        for key, value in settings.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        return Client(create_server(transport=fake.transport))

    return _make


@pytest.fixture
async def client(make_client: Callable[..., Client], fake: FakeHTTP):
    async with make_client() as c:
        yield c
    assert not fake.unmatched, f"requests without a fake route: {fake.unmatched}"


@pytest.fixture
def make_http_app(fake: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Build the HTTP app the way main() serves it, configured from the given env vars."""

    def _make(**env: str | None) -> Any:
        for key in list(os.environ):
            if key.startswith(("HEVY_", "VAULT_", "MCP_", "CF_ACCESS_")):
                monkeypatch.delenv(key)
        for key, value in ({"HEVY_API_KEY": TEST_API_KEY} | env).items():
            if value is not None:
                monkeypatch.setenv(key, value)
        settings = Settings()
        server = create_server(settings, transport=fake.transport)
        return server.http_app(path="/mcp", **http_options(settings, transport=fake.transport))

    return _make


def http_mcp_client(
    app: Any, base_url: str = HTTP_BASE, headers: dict[str, str] | None = None
) -> Client:
    """An MCP client talking Streamable HTTP to the app at ``base_url``."""

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=base_url, **kwargs
        )

    return Client(
        StreamableHttpTransport(
            f"{base_url}/mcp", headers=headers or {}, httpx_client_factory=factory
        )
    )
