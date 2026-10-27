"""Exercise templates, template search and exercise history."""

import pytest
from fastmcp.exceptions import ToolError

from .conftest import load


async def test_list_exercise_templates_caps_page_size_at_100(client, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates_page1.json"))

    result = await client.call_tool("list_exercise_templates", {"page": 1, "page_size": 500})

    assert result.structured_content["page_count"] == 2
    assert [t["title"] for t in result.structured_content["exercise_templates"]] == [
        "Bench Press (Barbell)",
        "Squat (Barbell)",
        "Pull Up",
    ]
    [request] = fake.hevy_requests()
    assert request.url.params["pageSize"] == "100"


async def test_get_exercise_template_includes_muscles_and_equipment(client, fake):
    fake.get("/v1/exercise_templates/05293BCA", json=load("hevy/exercise_template.json"))

    result = await client.call_tool("get_exercise_template", {"exercise_template_id": "05293BCA"})

    assert result.structured_content == load("hevy/exercise_template.json")


async def test_get_exercise_template_not_found(client, fake):
    fake.get("/v1/exercise_templates/NOPE", status=404, json={"error": "not found"})

    with pytest.raises(ToolError, match="(?i)not found.*NOPE"):
        await client.call_tool("get_exercise_template", {"exercise_template_id": "NOPE"})


async def test_search_matches_titles_across_pages_ignoring_case(client, fake):
    fake.get(
        "/v1/exercise_templates",
        json=load("hevy/exercise_templates_page1.json"),
        params={"page": 1},
    )
    fake.get(
        "/v1/exercise_templates",
        json=load("hevy/exercise_templates_page2.json"),
        params={"page": 2},
    )

    result = await client.call_tool("search_exercise_templates", {"query": "BENCH press"})

    assert result.structured_content["query"] == "BENCH press"
    assert [t["id"] for t in result.structured_content["exercise_templates"]] == [
        "05293BCA",
        "3601968B",
    ]
    assert [r.url.params["page"] for r in fake.hevy_requests()] == ["1", "2"]


async def test_search_reuses_the_cached_templates(client, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))

    await client.call_tool("search_exercise_templates", {"query": "squat"})
    result = await client.call_tool("search_exercise_templates", {"query": "pull"})

    assert [t["title"] for t in result.structured_content["exercise_templates"]] == ["Pull Up"]
    assert len(fake.hevy_requests()) == 1


async def test_search_refetches_after_the_cache_expires(make_client, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))

    async with make_client(HEVY_TEMPLATE_CACHE_SECONDS="0") as client:
        await client.call_tool("search_exercise_templates", {"query": "squat"})
        await client.call_tool("search_exercise_templates", {"query": "squat"})

    assert len(fake.hevy_requests()) == 2


async def test_search_with_no_match_returns_an_empty_list(client, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))

    result = await client.call_tool("search_exercise_templates", {"query": "zercher"})

    assert result.structured_content["exercise_templates"] == []


async def test_search_rejects_a_blank_query(client, fake):
    with pytest.raises(ToolError):
        await client.call_tool("search_exercise_templates", {"query": "  "})

    assert fake.requests == []


async def test_exercise_history_passes_the_date_range_to_hevy(client, fake):
    fake.get("/v1/exercise_history/05293BCA", json=load("hevy/exercise_history.json"))

    result = await client.call_tool(
        "get_exercise_history",
        {"exercise_template_id": "05293BCA", "start_date": "2024-06-01", "end_date": "2024-06-30"},
    )

    [request] = fake.hevy_requests()
    assert request.url.params["start_date"] == "2024-06-01T00:00:00Z"
    assert request.url.params["end_date"] == "2024-06-30T23:59:59Z"
    assert result.structured_content["exercise_template_id"] == "05293BCA"
    entries = result.structured_content["exercise_history"]
    assert [(e["workout_start_time"], e["weight_kg"], e["reps"], e["rpe"]) for e in entries] == [
        ("2024-06-03T07:00:00Z", 60, 10, None),
        ("2024-06-03T07:00:00Z", 100, 8, 8.5),
        ("2024-06-05T18:00:00Z", 102.5, 5, 9),
    ]
    assert entries[0]["set_type"] == "warmup"


async def test_exercise_history_without_dates_gets_everything(client, fake):
    fake.get("/v1/exercise_history/05293BCA", json=load("hevy/exercise_history.json"))

    await client.call_tool("get_exercise_history", {"exercise_template_id": "05293BCA"})

    [request] = fake.hevy_requests()
    assert "start_date" not in request.url.params
    assert "end_date" not in request.url.params


async def test_exercise_history_rejects_end_before_start(client, fake):
    with pytest.raises(ToolError, match="before"):
        await client.call_tool(
            "get_exercise_history",
            {
                "exercise_template_id": "05293BCA",
                "start_date": "2024-06-30",
                "end_date": "2024-06-01",
            },
        )

    assert fake.requests == []


async def test_exercise_history_rejects_a_bad_date(client, fake):
    with pytest.raises(ToolError, match="start_date"):
        await client.call_tool(
            "get_exercise_history", {"exercise_template_id": "05293BCA", "start_date": "June"}
        )


def template(n: int, title: str) -> dict:
    return load("hevy/exercise_template.json") | {"id": f"T{n:04d}", "title": title}


async def test_search_without_page_count_pages_until_a_short_page(client, fake):
    full = [template(n, f"Curl {n}") for n in range(99)] + [template(99, "Bench Press (Barbell)")]
    fake.get(
        "/v1/exercise_templates",
        json={"page": 1, "page_count": None, "exercise_templates": full},
        params={"page": 1},
    )
    fake.get(
        "/v1/exercise_templates",
        json={"page": 2, "exercise_templates": [template(100, "Incline Bench Press")]},
        params={"page": 2},
    )

    result = await client.call_tool("search_exercise_templates", {"query": "bench"})

    assert [t["id"] for t in result.structured_content["exercise_templates"]] == [
        "T0099",
        "T0100",
    ]
    assert result.structured_content["truncated"] is False
    assert [r.url.params["page"] for r in fake.hevy_requests()] == ["1", "2"]


async def test_search_flags_truncation_at_the_page_cap(make_client, fake):
    full = [template(n, f"Bench {n}") for n in range(100)]
    fake.get(
        "/v1/exercise_templates",
        json={"page": 1, "page_count": 5, "exercise_templates": full},
    )

    async with make_client(HEVY_RANGE_MAX_PAGES="1") as client:
        result = await client.call_tool("search_exercise_templates", {"query": "bench"})

    assert len(result.structured_content["exercise_templates"]) == 100
    assert result.structured_content["truncated"] is True
    assert len(fake.hevy_requests()) == 1


async def test_complete_search_is_not_truncated(client, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))

    result = await client.call_tool("search_exercise_templates", {"query": "squat"})

    assert result.structured_content["truncated"] is False
