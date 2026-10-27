"""Startup: reading the Hevy API key from Vault with AppRole, or from HEVY_API_KEY."""

import json

import httpx
import pytest

from .conftest import load

VAULT = "https://vault.home.example:8200"
ROLE_ID = "test-role-id-6a1f"
SECRET_ID = "test-secret-id-93c2"
VAULT_TOKEN = "hvs.test-client-token"
VAULT_HEVY_KEY = "11111111-2222-4333-8444-555555555555"
LOGIN = f"{VAULT}/v1/auth/approle/login"
KV_READ = f"{VAULT}/v1/secret/data/hevy-mcp"
CREDENTIALS = (ROLE_ID, SECRET_ID, VAULT_TOKEN, VAULT_HEVY_KEY)

VAULT_ENV = {
    "HEVY_API_KEY": None,
    "VAULT_ADDR": VAULT,
    "VAULT_ROLE_ID": ROLE_ID,
    "VAULT_SECRET_ID": SECRET_ID,
}


def login_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "auth": {
                "client_token": VAULT_TOKEN,
                "policies": ["default", "hevy-mcp"],
                "lease_duration": 300,
                "renewable": True,
            }
        },
    )


def kv_ok(data: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "data": {"api_key": VAULT_HEVY_KEY} if data is None else data,
                "metadata": {"version": 3, "destroyed": False},
            }
        },
    )


@pytest.fixture
def vault(fake):
    fake.post(LOGIN, login_ok())
    fake.get(KV_READ, kv_ok())
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))
    return fake


async def startup_error(make_client, **env) -> str:
    with pytest.raises(RuntimeError) as exc:
        async with make_client(**VAULT_ENV | env):
            pass
    message = str(exc.value)
    for credential in CREDENTIALS:
        assert credential not in message
    return message


async def test_key_is_read_from_vault_and_used_for_hevy(make_client, vault):
    async with make_client(**VAULT_ENV) as client:
        result = await client.call_tool("get_user_info", {})

    assert result.structured_content["username"] == "jhon"
    login, kv_read, hevy = vault.requests
    assert login.url == LOGIN
    assert json.loads(login.content) == {"role_id": ROLE_ID, "secret_id": SECRET_ID}
    assert kv_read.url == KV_READ
    assert kv_read.headers["x-vault-token"] == VAULT_TOKEN
    assert hevy.headers["api-key"] == VAULT_HEVY_KEY


async def test_vault_is_read_once_per_process(make_client, vault):
    async with make_client(**VAULT_ENV) as client:
        await client.call_tool("get_user_info", {})
        await client.call_tool("get_user_info", {})

    assert len([r for r in vault.requests if r.url.host == "vault.home.example"]) == 2


async def test_credentials_can_come_from_files(make_client, vault, tmp_path):
    (tmp_path / "role_id").write_text(ROLE_ID + "\n")
    (tmp_path / "secret_id").write_text(SECRET_ID + "\n")

    env = VAULT_ENV | {
        "VAULT_ROLE_ID": None,
        "VAULT_SECRET_ID": None,
        "VAULT_ROLE_ID_FILE": str(tmp_path / "role_id"),
        "VAULT_SECRET_ID_FILE": str(tmp_path / "secret_id"),
    }
    async with make_client(**env) as client:
        await client.call_tool("get_user_info", {})

    login = vault.requests[0]
    assert json.loads(login.content) == {"role_id": ROLE_ID, "secret_id": SECRET_ID}


async def test_mounts_and_path_are_configurable(make_client, fake):
    fake.post(f"{VAULT}/v1/auth/home-approle/login", login_ok())
    fake.get(f"{VAULT}/v1/kv/data/apps/hevy", kv_ok())
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))

    env = VAULT_ENV | {
        "VAULT_APPROLE_MOUNT": "home-approle",
        "VAULT_KV_MOUNT": "kv",
        "VAULT_KV_PATH": "apps/hevy",
    }
    async with make_client(**env) as client:
        await client.call_tool("get_user_info", {})

    assert fake.hevy_requests()[0].headers["api-key"] == VAULT_HEVY_KEY


async def test_hevy_api_key_override_skips_vault(make_client, fake):
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))

    async with make_client(**VAULT_ENV | {"HEVY_API_KEY": "override-key"}) as client:
        await client.call_tool("get_user_info", {})

    assert [r.url.host for r in fake.requests] == ["api.hevyapp.com"]
    assert fake.requests[0].headers["api-key"] == "override-key"


async def test_unreachable_vault_fails_startup(make_client, fake):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fake.post(LOGIN, refuse)

    message = await startup_error(make_client)

    assert "reach Vault" in message
    assert VAULT in message


async def test_denied_login_fails_startup(make_client, fake):
    fake.post(LOGIN, status=400, json={"errors": ["invalid role or secret ID"]})

    message = await startup_error(make_client)

    assert "login" in message
    assert "denied" in message
    assert VAULT in message


async def test_missing_secret_path_fails_startup(make_client, fake):
    fake.post(LOGIN, login_ok())
    fake.get(KV_READ, status=404, json={"errors": []})

    message = await startup_error(make_client)

    assert "secret/hevy-mcp" in message
    assert VAULT in message


async def test_forbidden_secret_path_fails_startup(make_client, fake):
    fake.post(LOGIN, login_ok())
    fake.get(KV_READ, status=403, json={"errors": ["permission denied"]})

    message = await startup_error(make_client)

    assert "permission denied" in message
    assert "secret/hevy-mcp" in message


async def test_missing_api_key_field_fails_startup(make_client, fake):
    fake.post(LOGIN, login_ok())
    fake.get(KV_READ, kv_ok({"apikey": VAULT_HEVY_KEY}))

    message = await startup_error(make_client)

    assert "'api_key'" in message
    assert "secret/hevy-mcp" in message


@pytest.mark.parametrize(
    ("unset", "named"),
    [
        ({"VAULT_ADDR": None}, "VAULT_ADDR"),
        ({"VAULT_ROLE_ID": None}, "VAULT_ROLE_ID"),
        ({"VAULT_SECRET_ID": None}, "VAULT_SECRET_ID"),
    ],
)
async def test_missing_settings_fail_startup(make_client, fake, unset, named):
    message = await startup_error(make_client, **unset)

    assert named in message
    assert fake.requests == []


async def test_unreadable_credential_file_fails_startup(make_client, fake, tmp_path):
    message = await startup_error(
        make_client, VAULT_SECRET_ID=None, VAULT_SECRET_ID_FILE=str(tmp_path / "nope")
    )

    assert "VAULT_SECRET_ID_FILE" in message
    assert fake.requests == []


async def test_bad_ca_bundle_fails_startup(make_client, fake, tmp_path):
    (tmp_path / "ca.pem").write_text("not a certificate")

    message = await startup_error(make_client, VAULT_CACERT=str(tmp_path / "ca.pem"))

    assert "VAULT_CACERT" in message
    assert fake.requests == []


async def test_null_auth_in_the_login_response_fails_startup(make_client, fake):
    fake.post(LOGIN, json={"auth": None, "errors": []})

    message = await startup_error(make_client)

    assert "AppRole login" in message
    assert VAULT in message


@pytest.mark.parametrize("body", [{"data": None}, {"data": {"data": None}}])
async def test_null_data_in_the_kv_response_fails_startup(make_client, fake, body):
    fake.post(LOGIN, login_ok())
    fake.get(KV_READ, json=body)

    message = await startup_error(make_client)

    assert "secret/hevy-mcp" in message
    assert VAULT in message


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_hevy_api_key_is_ignored_in_favour_of_vault(make_client, vault, blank):
    async with make_client(**VAULT_ENV | {"HEVY_API_KEY": blank}) as client:
        await client.call_tool("get_user_info", {})

    assert vault.requests[0].url == LOGIN
    assert vault.hevy_requests()[0].headers["api-key"] == VAULT_HEVY_KEY


async def test_blank_hevy_api_key_without_vault_fails_startup(make_client, fake):
    message = await startup_error(make_client, HEVY_API_KEY=" ", VAULT_ADDR=None)

    assert "HEVY_API_KEY" in message
    assert "VAULT_ADDR" in message
    assert fake.requests == []
