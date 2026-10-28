"""Cloudflare Access: every request except /health needs a valid Access JWT."""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hevy_mcp.access import access_middleware
from hevy_mcp.config import Settings
from hevy_mcp.server import create_server

from .conftest import TEST_API_KEY, FakeHTTP, load

TEAM = "hevy-test.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
CERTS = f"{ISSUER}/cdn-cgi/access/certs"
AUD = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
OWNER = "owner@example.com"
BASE = "https://hevy-mcp.example"

ACCESS_ENV = {
    "CF_ACCESS_TEAM_DOMAIN": TEAM,
    "CF_ACCESS_AUD": AUD,
    "CF_ACCESS_ALLOWED_EMAILS": f"someone-else@example.com, {OWNER}",
}

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROTATED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "kid-current"
ROTATED_KID = "kid-rotated"

TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "get_user_info", "arguments": {}},
}


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    return RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": kid, "alg": "RS256"}


def certs(*keys: tuple[rsa.RSAPrivateKey, str]) -> httpx.Response:
    """The team's certs endpoint, as Cloudflare serves it."""
    return httpx.Response(200, json={"keys": [jwk(key, kid) for key, kid in keys]})


def token(key: rsa.RSAPrivateKey = KEY, kid: str | None = KID, **claims: Any) -> str:
    """An Access application token; claims set to None are left out."""
    now = int(time.time())
    body = {
        "aud": [AUD],
        "iss": ISSUER,
        "email": OWNER,
        "sub": "7335d417-61da-459d-899c-0a01c76a2f94",
        "type": "app",
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    } | claims
    headers = {"kid": kid} if kid else {}
    return jwt.encode(
        {k: v for k, v in body.items() if v is not None}, key, "RS256", headers=headers
    )


@pytest.fixture
def make_app(fake: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Build the HTTP app as main() serves it, configured from the given env vars."""

    def _make(**env: str | None) -> Any:
        for key in list(os.environ):
            if key.startswith(("HEVY_", "VAULT_", "MCP_", "CF_ACCESS_")):
                monkeypatch.delenv(key)
        settings = {"HEVY_API_KEY": TEST_API_KEY} | ACCESS_ENV | env
        for key, value in settings.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        config = Settings()
        server = create_server(config, transport=fake.transport)
        return server.http_app(
            path="/mcp", middleware=access_middleware(config, transport=fake.transport)
        )

    return _make


@pytest.fixture
def hevy(fake: FakeHTTP) -> FakeHTTP:
    fake.get(CERTS, certs((KEY, KID)))
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))
    return fake


class Clock:
    """Stands in for the verifier's monotonic clock, so tests can skip ahead."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("hevy_mcp.access.monotonic", clock)
    return clock


def mcp_client(app: Any, assertion: str | None) -> Client:
    """An MCP client talking Streamable HTTP to the app, as Cloudflare would forward it."""
    headers = {"Cf-Access-Jwt-Assertion": assertion} if assertion else {}

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, **kwargs)

    return Client(
        StreamableHttpTransport(f"{BASE}/mcp", headers=headers, httpx_client_factory=factory)
    )


async def call_user_info(app: Any, assertion: str | None) -> Any:
    async with app.lifespan(app), mcp_client(app, assertion) as client:
        return await client.call_tool("get_user_info", {})


async def post(app: Any, path: str = "/mcp", **headers: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        return await http.post(
            path,
            json=TOOLS_CALL,
            headers={"Accept": "application/json, text/event-stream"} | headers,
        )


def certs_fetches(fake: FakeHTTP) -> int:
    return sum(1 for r in fake.requests if r.url.host == TEAM)


async def test_valid_token_can_list_and_call_tools(make_app, hevy):
    app = make_app()

    async with app.lifespan(app), mcp_client(app, token()) as client:
        tools = await client.list_tools()
        result = await client.call_tool("get_user_info", {})

    assert "get_user_info" in {t.name for t in tools}
    assert result.structured_content["username"]
    assert hevy.hevy_requests("/v1/user/info")


async def test_allowed_emails_ignore_case(make_app, hevy):
    app = make_app(CF_ACCESS_ALLOWED_EMAILS="Owner@Example.com")

    result = await call_user_info(app, token(email="OWNER@example.COM"))

    assert result.structured_content["username"]


async def test_team_domain_may_be_given_as_a_url(make_app, hevy):
    app = make_app(CF_ACCESS_TEAM_DOMAIN=f"https://{TEAM}/")

    result = await call_user_info(app, token())

    assert result.structured_content["username"]


async def test_small_clock_skew_is_tolerated(make_app, hevy):
    app = make_app()

    result = await call_user_info(app, token(exp=int(time.time()) - 10))

    assert result.structured_content["username"]


def assertion_for(case: str) -> str | None:
    now = int(time.time())
    return {
        "missing": None,
        "malformed": "not-a-jwt",
        "no key id": token(kid=None),
        "bad signature": token(key=ROTATED_KEY),
        "unknown key": token(key=ROTATED_KEY, kid=ROTATED_KID),
        "expired": token(iat=now - 900, nbf=now - 900, exp=now - 120),
        "not yet valid": token(nbf=now + 300),
        "wrong audience": token(aud=["some-other-application"]),
        "no audience": token(aud=None),
        "wrong issuer": token(iss="https://other-team.cloudflareaccess.com"),
        "email not allowed": token(email="intruder@example.com"),
        "no email": token(email=None, common_name="service-token.access"),
        "unsigned": jwt.encode({"aud": [AUD], "iss": ISSUER, "email": OWNER}, None, "none"),
    }[case]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "malformed",
        "no key id",
        "bad signature",
        "unknown key",
        "expired",
        "not yet valid",
        "wrong audience",
        "no audience",
        "wrong issuer",
        "email not allowed",
        "no email",
        "unsigned",
    ],
)
async def test_invalid_tokens_are_denied_before_any_tool_runs(make_app, hevy, case):
    assertion = assertion_for(case)
    headers = {"Cf-Access-Jwt-Assertion": assertion} if assertion else {}

    response = await post(make_app(), **headers)

    assert response.status_code == 403
    assert response.json() == {"error": "Access denied."}
    assert not hevy.hevy_requests()


async def test_denial_never_echoes_the_token_or_its_claims(make_app, hevy):
    assertion = token(email="intruder@example.com")

    response = await post(make_app(), **{"Cf-Access-Jwt-Assertion": assertion})

    assert assertion not in response.text
    assert "intruder" not in response.text


async def test_every_path_but_health_is_guarded(make_app, hevy):
    response = await post(make_app(), path="/")

    assert response.status_code == 403


async def test_health_needs_no_token(make_app, hevy):
    app = make_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_signing_keys_are_cached(make_app, hevy):
    app = make_app()

    async with app.lifespan(app), mcp_client(app, token()) as client:
        await client.call_tool("get_user_info", {})
        await client.call_tool("get_user_info", {})

    assert certs_fetches(hevy) == 1


async def test_rotated_key_is_picked_up_with_one_refresh(make_app, hevy):
    hevy.get(CERTS, certs((KEY, KID)), certs((KEY, KID), (ROTATED_KEY, ROTATED_KID)))
    app = make_app()

    assert await call_user_info(app, token())
    assert await call_user_info(app, token(key=ROTATED_KEY, kid=ROTATED_KID))
    assert certs_fetches(hevy) == 2


async def test_unknown_key_ids_cannot_force_repeated_refreshes(make_app, hevy):
    app = make_app()
    assert await call_user_info(app, token())
    stranger = token(key=ROTATED_KEY, kid="kid-nobody-has")

    for _ in range(3):
        response = await post(app, **{"Cf-Access-Jwt-Assertion": stranger})
        assert response.status_code == 403

    # One load for the valid token, then one refresh for the unknown id.
    assert certs_fetches(hevy) == 2


async def test_keys_loaded_for_a_request_are_not_refetched_for_its_unknown_id(make_app, hevy):
    stranger = token(key=ROTATED_KEY, kid="kid-nobody-has")

    response = await post(make_app(), **{"Cf-Access-Jwt-Assertion": stranger})

    assert response.status_code == 403
    assert certs_fetches(hevy) == 1


async def test_unreachable_certs_endpoint_fails_closed(make_app, fake):
    fake.get(CERTS, httpx.Response(502, text="Bad Gateway"))

    response = await post(make_app(), **{"Cf-Access-Jwt-Assertion": token()})

    assert response.status_code == 503
    assert response.json() == {"error": "Authentication is unavailable."}
    assert not fake.hevy_requests()


async def test_without_access_settings_requests_need_no_token(make_app, hevy, caplog):
    caplog.set_level(logging.WARNING)
    unset = dict.fromkeys(ACCESS_ENV)

    app = make_app(**unset)
    result = await call_user_info(app, None)

    assert result.structured_content["username"]
    assert "without authentication" in caplog.text
    assert certs_fetches(hevy) == 0


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"CF_ACCESS_AUD": None, "CF_ACCESS_ALLOWED_EMAILS": None}, "CF_ACCESS_AUD"),
        ({"CF_ACCESS_ALLOWED_EMAILS": None}, "CF_ACCESS_ALLOWED_EMAILS"),
        ({"CF_ACCESS_ALLOWED_EMAILS": " , "}, "CF_ACCESS_ALLOWED_EMAILS"),
        ({"CF_ACCESS_TEAM_DOMAIN": None}, "CF_ACCESS_TEAM_DOMAIN"),
        ({"CF_ACCESS_AUD": ""}, "CF_ACCESS_AUD"),
    ],
)
def test_partial_access_settings_stop_startup(make_app, env, missing):
    with pytest.raises(ValidationError) as exc:
        make_app(**env)

    assert missing in str(exc.value)
    assert TEST_API_KEY not in str(exc.value)


async def test_concurrent_requests_during_an_outage_make_one_certs_call(make_app, fake):
    fake.get(CERTS, httpx.Response(502, text="Bad Gateway"))
    app = make_app()
    stranger = token(key=ROTATED_KEY, kid="kid-nobody-has")

    responses = await asyncio.gather(
        *(post(app, **{"Cf-Access-Jwt-Assertion": stranger}) for _ in range(10))
    )

    assert {r.status_code for r in responses} == {503}
    assert certs_fetches(fake) == 1


async def test_certs_are_fetched_again_once_the_retry_pause_ends(make_app, hevy, clock):
    hevy.get(CERTS, httpx.Response(502, text="Bad Gateway"), certs((KEY, KID)))
    app = make_app()
    assertion = {"Cf-Access-Jwt-Assertion": token()}

    assert (await post(app, **assertion)).status_code == 503
    clock.advance(1)
    assert (await post(app, **assertion)).status_code == 503
    assert certs_fetches(hevy) == 1

    clock.advance(5)
    assert await call_user_info(app, token())
    assert certs_fetches(hevy) == 2


async def test_a_key_withdrawn_by_cloudflare_stops_being_trusted(make_app, hevy, clock):
    hevy.get(CERTS, certs((KEY, KID)), certs((ROTATED_KEY, ROTATED_KID)))
    app = make_app()
    assert await call_user_info(app, token())

    clock.advance(3600)
    response = await post(app, **{"Cf-Access-Jwt-Assertion": token()})

    assert response.status_code == 403
    assert certs_fetches(hevy) == 2


async def test_a_failed_refresh_of_old_keys_keeps_them(make_app, hevy, clock, caplog):
    hevy.get(CERTS, certs((KEY, KID)), httpx.Response(502, text="Bad Gateway"))
    app = make_app()
    assert await call_user_info(app, token())

    clock.advance(3600)
    result = await call_user_info(app, token())

    assert result.structured_content["username"]
    assert certs_fetches(hevy) == 2
    assert "Keeping the cached keys" in caplog.text


def test_websockets_are_refused(make_app, hevy):
    client = TestClient(make_app())

    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/mcp"):
        pass

    assert exc.value.code == 1008
