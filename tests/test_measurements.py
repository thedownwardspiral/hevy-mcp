"""Body measurement tools."""

import pytest
from fastmcp.exceptions import ToolError

from .conftest import load, page


def measurement_on(day: str, weight_kg: float) -> dict:
    return load("hevy/body_measurement.json") | {"date": day, "weight_kg": weight_kg}


async def test_list_body_measurements_caps_page_size(client, fake):
    fake.get(
        "/v1/body_measurements",
        json=page("body_measurements", [load("hevy/body_measurement.json")], 1, 3),
    )

    result = await client.call_tool("list_body_measurements", {"page_size": 25})

    assert result.structured_content["page_count"] == 3
    assert result.structured_content["body_measurements"] == [load("hevy/body_measurement.json")]
    [request] = fake.hevy_requests()
    assert request.url.params["pageSize"] == "10"


async def test_get_body_measurement_for_a_date(client, fake):
    fake.get("/v1/body_measurements/2024-08-14", json=load("hevy/body_measurement.json"))

    result = await client.call_tool("get_body_measurement", {"date": "2024-08-14"})

    assert result.structured_content["weight_kg"] == 80.5
    assert result.structured_content["fat_percent"] == 18.5


async def test_get_body_measurement_not_found(client, fake):
    fake.get("/v1/body_measurements/2024-06-01", status=404, json={"error": "not found"})

    with pytest.raises(ToolError, match="(?i)not found.*2024-06-01"):
        await client.call_tool("get_body_measurement", {"date": "2024-06-01"})


@pytest.mark.parametrize("bad", ["1 June", "2024-06-01T08:00:00Z", "2024-02-30"])
async def test_get_body_measurement_rejects_a_non_date(client, fake, bad):
    with pytest.raises(ToolError, match="date"):
        await client.call_tool("get_body_measurement", {"date": bad})

    assert fake.requests == []


async def test_body_measurements_in_range_page_until_past_the_start(client, fake):
    fake.get(
        "/v1/body_measurements",
        json=page(
            "body_measurements",
            [measurement_on("2024-07-03", 79.0), measurement_on("2024-06-30", 79.4)],
            1,
            4,
        ),
        params={"page": 1},
    )
    fake.get(
        "/v1/body_measurements",
        json=page(
            "body_measurements",
            [measurement_on("2024-06-01", 80.6), measurement_on("2024-05-28", 80.9)],
            2,
            4,
        ),
        params={"page": 2},
    )

    result = await client.call_tool(
        "get_body_measurements_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
    )

    assert [
        (m["date"], m["weight_kg"]) for m in result.structured_content["body_measurements"]
    ] == [
        ("2024-06-30", 79.4),
        ("2024-06-01", 80.6),
    ]
    assert result.structured_content["truncated"] is False
    assert [r.url.params["page"] for r in fake.hevy_requests()] == ["1", "2"]


async def test_body_measurements_in_range_flag_truncation(make_client, fake):
    fake.get(
        "/v1/body_measurements",
        json=page("body_measurements", [measurement_on("2024-06-20", 79.0)], 1, 6),
    )

    async with make_client(HEVY_RANGE_MAX_PAGES="1") as client:
        result = await client.call_tool(
            "get_body_measurements_in_range",
            {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        )

    assert result.structured_content["truncated"] is True
    assert len(fake.hevy_requests()) == 1


async def test_body_measurements_in_range_reject_end_before_start(client, fake):
    with pytest.raises(ToolError, match="before"):
        await client.call_tool(
            "get_body_measurements_in_range",
            {"start_date": "2024-06-30", "end_date": "2024-06-01"},
        )

    assert fake.requests == []
