"""get_user_info, plus the Hevy error handling every tool shares."""

import httpx
import pytest
from fastmcp.exceptions import ToolError

from .conftest import TEST_API_KEY, load


async def test_get_user_info_returns_profile_and_units(client, fake):
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))

    result = await client.call_tool("get_user_info", {})

    assert result.structured_content == {
        "id": "9c465af3-de7d-42bc-9c7c-f0170396358b",
        "username": "jhon",
        "name": "John doe",
        "url": "https://hevy.com/user/jhon",
        "weight_unit": "kg",
        "distance_unit": "kilometers",
        "is_pro": True,
    }
    [request] = fake.hevy_requests("/v1/user/info")
    assert request.headers["api-key"] == TEST_API_KEY


async def test_unknown_fields_from_hevy_are_kept(client, fake):
    body = load("hevy/user_info.json")
    body["data"]["new_field"] = "added later by Hevy"
    fake.get("/v1/user/info", json=body)

    result = await client.call_tool("get_user_info", {})

    assert result.structured_content["new_field"] == "added later by Hevy"


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_gives_auth_error_that_mentions_pro(client, fake, status):
    fake.get("/v1/user/info", status=status, json={"error": "Unauthorized"})

    with pytest.raises(ToolError) as exc:
        await client.call_tool("get_user_info", {})

    message = str(exc.value)
    assert "API key" in message
    assert "Hevy Pro" in message
    assert TEST_API_KEY not in message
    assert len(fake.hevy_requests()) == 1, "auth errors are not retried"


async def test_not_found_gives_not_found_error(client, fake):
    fake.get("/v1/user/info", status=404, json={"error": "User not found"})

    with pytest.raises(ToolError, match="(?i)not found"):
        await client.call_tool("get_user_info", {})


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_transient_failures_are_retried_then_succeed(client, fake, status):
    fake.get(
        "/v1/user/info",
        httpx.Response(status),
        httpx.Response(status),
        httpx.Response(200, json=load("hevy/user_info.json")),
    )

    result = await client.call_tool("get_user_info", {})

    assert result.structured_content["username"] == "jhon"
    assert len(fake.hevy_requests()) == 3


async def test_timeouts_are_retried_then_succeed(client, fake):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fake.get("/v1/user/info", timeout, httpx.Response(200, json=load("hevy/user_info.json")))

    result = await client.call_tool("get_user_info", {})

    assert result.structured_content["username"] == "jhon"


async def test_exhausted_retries_give_unavailable_error(client, fake):
    fake.get("/v1/user/info", status=503)

    with pytest.raises(ToolError, match="(?i)unavailable"):
        await client.call_tool("get_user_info", {})

    assert len(fake.hevy_requests()) == 4, "one attempt plus three retries"


async def test_retry_count_is_configurable(make_client, fake):
    fake.get("/v1/user/info", status=500)

    async with make_client(HEVY_MAX_RETRIES="1") as client:
        with pytest.raises(ToolError, match="(?i)unavailable"):
            await client.call_tool("get_user_info", {})

    assert len(fake.hevy_requests()) == 2


async def test_base_url_is_configurable(make_client, fake):
    fake.get("https://hevy.test/v1/user/info", json=load("hevy/user_info.json"))

    async with make_client(HEVY_BASE_URL="https://hevy.test") as client:
        result = await client.call_tool("get_user_info", {})

    assert result.structured_content["username"] == "jhon"
