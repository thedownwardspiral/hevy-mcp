"""Routine and routine folder tools."""

import pytest
from fastmcp.exceptions import ToolError

from .conftest import load, page


async def test_list_routines_caps_page_size(client, fake):
    fake.get(
        "/v1/routines",
        json=page("routines", [load("hevy/routine.json")["routine"]], 2, 2),
        params={"page": 2},
    )

    result = await client.call_tool("list_routines", {"page": 2, "page_size": 99})

    assert result.structured_content["page"] == 2
    [routine] = result.structured_content["routines"]
    assert routine["title"] == "Upper Body 💪"
    [request] = fake.hevy_requests()
    assert request.url.params["pageSize"] == "10"


async def test_get_routine_returns_planned_sets(client, fake):
    fake.get("/v1/routines/c2ae1e2a-4d4b-4f0c-9a51-8f0c3e5d7b10", json=load("hevy/routine.json"))

    result = await client.call_tool(
        "get_routine", {"routine_id": "c2ae1e2a-4d4b-4f0c-9a51-8f0c3e5d7b10"}
    )

    assert result.structured_content == load("hevy/routine.json")["routine"]
    planned = result.structured_content["exercises"][0]["sets"][1]
    assert planned["rep_range"] == {"start": 8, "end": 12}


async def test_get_routine_not_found(client, fake):
    fake.get("/v1/routines/gone", status=404, json={"error": "Routine not found"})

    with pytest.raises(ToolError, match="(?i)not found.*gone"):
        await client.call_tool("get_routine", {"routine_id": "gone"})


async def test_list_routine_folders(client, fake):
    fake.get(
        "/v1/routine_folders", json=page("routine_folders", [load("hevy/routine_folder.json")])
    )

    result = await client.call_tool("list_routine_folders", {"page_size": 11})

    assert result.structured_content["routine_folders"] == [load("hevy/routine_folder.json")]
    [request] = fake.hevy_requests()
    assert request.url.params["pageSize"] == "10"


async def test_get_routine_folder(client, fake):
    fake.get("/v1/routine_folders/42", json=load("hevy/routine_folder.json"))

    result = await client.call_tool("get_routine_folder", {"folder_id": 42})

    assert result.structured_content["title"] == "Push Pull 🏋️‍♂️"


async def test_get_routine_folder_not_found(client, fake):
    fake.get("/v1/routine_folders/7", status=404, json={"error": "Routine folder not found"})

    with pytest.raises(ToolError, match="(?i)not found.*7"):
        await client.call_tool("get_routine_folder", {"folder_id": 7})
