"""Server-level behaviour: the tool list and the HTTP health route."""

import httpx

from hevy_mcp.server import create_server

from .conftest import TEST_API_KEY

EXPECTED_TOOLS = {
    "get_user_info",
    "list_workouts",
    "get_workout",
    "get_workout_count",
    "get_workouts_in_range",
    "get_training_summary",
    "list_workout_events",
    "list_exercise_templates",
    "get_exercise_template",
    "search_exercise_templates",
    "get_exercise_history",
    "list_body_measurements",
    "get_body_measurement",
    "get_body_measurements_in_range",
    "list_routines",
    "get_routine",
    "list_routine_folders",
    "get_routine_folder",
}

WRITE_TOOLS = {
    "create_workout": "create",
    "update_workout": "update",
    "create_routine": "create",
    "update_routine": "update",
    "create_routine_folder": "create",
    "create_exercise_template": "create",
    "create_body_measurement": "create",
    "update_body_measurement": "update",
}


async def test_every_tool_is_listed_and_read_only(client):
    tools = await client.list_tools()

    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is True, tool.name


async def test_write_tools_are_listed_only_when_enabled(writer, client):
    tools = {t.name: t for t in await writer.list_tools()}

    assert tools.keys() == EXPECTED_TOOLS | WRITE_TOOLS.keys()
    for name, kind in WRITE_TOOLS.items():
        hints = tools[name].annotations
        assert tools[name].description, name
        assert hints.read_only_hint is False, name
        assert hints.destructive_hint is (kind == "update"), name
        assert hints.idempotent_hint is (kind == "update"), name
        assert hints.open_world_hint is True, name
    for name in EXPECTED_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    assert "Confirm" in writer.instructions
    assert "Read-only" in client.instructions


async def test_health_route_reports_ok(monkeypatch):
    monkeypatch.setenv("HEVY_API_KEY", TEST_API_KEY)
    app = create_server().http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hevy-mcp.home.example"
    ) as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
