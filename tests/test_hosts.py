"""Allowed hosts: with MCP_ALLOWED_HOSTS set, other Host headers are refused."""

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from .conftest import TEST_API_KEY, http_mcp_client, load

PUBLIC = "hevy-mcp.example.com"
LAN = "hevy-mcp.home.example"
ALLOWED = {"MCP_ALLOWED_HOSTS": f"{PUBLIC}, {LAN}"}


@pytest.fixture
def hevy(fake):
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))
    return fake


async def user_info(app: Any, host: str) -> Any:
    async with app.lifespan(app), http_mcp_client(app, base_url=f"https://{host}") as client:
        return await client.call_tool("get_user_info", {})


async def get(app: Any, url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        return await http.get(url)


async def post_mcp(app: Any, host: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        return await http.post(
            f"https://{host}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )


@pytest.mark.parametrize("host", [PUBLIC, LAN, "HEVY-MCP.Example.COM", f"{PUBLIC}:443"])
async def test_allowed_hosts_reach_the_tools(make_http_app, hevy, host):
    app = make_http_app(**ALLOWED)

    result = await user_info(app, host)

    assert result.structured_content["username"]


@pytest.mark.parametrize("host", ["attacker.example", "evil.hevy-mcp.example.com", "10.0.0.5"])
async def test_other_hosts_are_refused_before_any_tool_runs(make_http_app, hevy, host):
    app = make_http_app(**ALLOWED)

    response = await post_mcp(app, host)

    assert response.status_code == 421
    assert not hevy.hevy_requests()


async def test_other_hosts_are_refused_on_every_path(make_http_app):
    app = make_http_app(**ALLOWED)

    response = await get(app, "https://attacker.example/health")

    assert response.status_code == 421


async def test_the_container_health_check_still_passes(make_http_app):
    # The Docker HEALTHCHECK calls /health on loopback inside the container.
    app = make_http_app(**ALLOWED)

    response = await get(app, "http://127.0.0.1:8000/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_without_the_setting_any_host_is_accepted(make_http_app, hevy):
    app = make_http_app()

    result = await user_info(app, "attacker.example")

    assert result.structured_content["username"]


async def test_host_check_runs_before_the_access_check(make_http_app):
    app = make_http_app(
        **ALLOWED,
        CF_ACCESS_TEAM_DOMAIN="hevy-test.cloudflareaccess.com",
        CF_ACCESS_AUD="aud",
        CF_ACCESS_ALLOWED_EMAILS="owner@example.com",
    )

    wrong_host = await post_mcp(app, "attacker.example")
    right_host = await post_mcp(app, PUBLIC)

    assert wrong_host.status_code == 421
    assert right_host.status_code == 403


@pytest.mark.parametrize(
    "value",
    ["https://hevy-mcp.example.com", "hevy-mcp.example.com/mcp", "*", "example.com, *"],
)
def test_urls_and_a_bare_wildcard_stop_startup(make_http_app, value):
    with pytest.raises(ValidationError) as exc:
        make_http_app(MCP_ALLOWED_HOSTS=value)

    assert "MCP_ALLOWED_HOSTS" in str(exc.value)
    assert TEST_API_KEY not in str(exc.value)
