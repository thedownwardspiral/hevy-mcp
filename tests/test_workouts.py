"""Workout tools: listing, details, count, events, range queries and the training summary."""

import pytest
from fastmcp.exceptions import ToolError

from .conftest import load, page, workout_at


async def test_list_workouts_returns_page_and_page_count(client, fake):
    fake.get(
        "/v1/workouts",
        json=page("workouts", [load("hevy/workout.json")], page=2, page_count=7),
        params={"page": 2, "pageSize": 5},
    )

    result = await client.call_tool("list_workouts", {"page": 2, "page_size": 5})

    assert result.structured_content["page"] == 2
    assert result.structured_content["page_count"] == 7
    [workout] = result.structured_content["workouts"]
    assert workout["title"] == "Morning Workout 💪"
    assert workout["exercises"][0]["sets"][1] == {
        "index": 1,
        "type": "normal",
        "weight_kg": 100,
        "reps": 8,
        "distance_meters": None,
        "duration_seconds": None,
        "rpe": 8.5,
        "custom_metric": None,
    }


async def test_list_workouts_caps_page_size_at_hevy_limit(client, fake):
    fake.get("/v1/workouts", json=page("workouts", []))

    await client.call_tool("list_workouts", {"page_size": 50})

    [request] = fake.hevy_requests("/v1/workouts")
    assert request.url.params["pageSize"] == "10"
    assert request.url.params["page"] == "1"


async def test_list_workouts_rejects_page_below_one(client):
    with pytest.raises(ToolError):
        await client.call_tool("list_workouts", {"page": 0})


async def test_get_workout_returns_full_details(client, fake):
    fake.get("/v1/workouts/b459cba5-cd6d-463c-abd6-54f8eafcadcb", json=load("hevy/workout.json"))

    result = await client.call_tool(
        "get_workout", {"workout_id": "b459cba5-cd6d-463c-abd6-54f8eafcadcb"}
    )

    assert result.structured_content == load("hevy/workout.json")


async def test_get_workout_not_found(client, fake):
    fake.get("/v1/workouts/missing", status=404, json={"error": "Workout not found"})

    with pytest.raises(ToolError, match="(?i)not found.*missing"):
        await client.call_tool("get_workout", {"workout_id": "missing"})


async def test_get_workout_escapes_the_id_in_the_path(client, fake):
    fake.get("/v1/workouts/..%2Fuser%2Finfo", json=load("hevy/workout.json"))

    await client.call_tool("get_workout", {"workout_id": "../user/info"})

    assert [r.url.raw_path for r in fake.hevy_requests()] == [b"/v1/workouts/..%2Fuser%2Finfo"]


async def test_get_workout_count(client, fake):
    fake.get("/v1/workouts/count", json={"workout_count": 42})

    result = await client.call_tool("get_workout_count", {})

    assert result.structured_content == {"workout_count": 42}


async def test_list_workout_events_since_a_date(client, fake):
    fake.get("/v1/workouts/events", json=load("hevy/workout_events.json"))

    result = await client.call_tool(
        "list_workout_events", {"since": "2024-06-01", "page": 1, "page_size": 20}
    )

    [request] = fake.hevy_requests("/v1/workouts/events")
    assert request.url.params["since"] == "2024-06-01T00:00:00Z"
    assert request.url.params["pageSize"] == "10"
    updated, deleted = result.structured_content["events"]
    assert updated["type"] == "updated"
    assert updated["workout"]["id"] == "b459cba5-cd6d-463c-abd6-54f8eafcadcb"
    assert deleted == {
        "type": "deleted",
        "workout": None,
        "id": "9d1f3a52-7c4e-4b8e-8f60-2a1c5b7e9d03",
        "deleted_at": "2024-06-04T10:00:00Z",
    }


async def test_list_workout_events_without_since_lets_hevy_default(client, fake):
    fake.get("/v1/workouts/events", json=load("hevy/workout_events.json"))

    await client.call_tool("list_workout_events", {})

    [request] = fake.hevy_requests("/v1/workouts/events")
    assert "since" not in request.url.params


async def test_workouts_in_range_pages_until_past_the_start_date(client, fake):
    fake.get(
        "/v1/workouts",
        json=page(
            "workouts",
            [
                workout_at("w-after-end", "2024-07-02T08:00:00Z"),
                workout_at("w4", "2024-06-30T08:00:00Z"),
            ],
            page=1,
            page_count=5,
        ),
        params={"page": 1},
    )
    fake.get(
        "/v1/workouts",
        json=page(
            "workouts",
            [
                workout_at("w3", "2024-06-15T08:00:00Z"),
                workout_at("w-before-start", "2024-05-31T08:00:00Z"),
            ],
            page=2,
            page_count=5,
        ),
        params={"page": 2},
    )

    result = await client.call_tool(
        "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
    )

    assert [w["id"] for w in result.structured_content["workouts"]] == ["w4", "w3"]
    assert result.structured_content["truncated"] is False
    assert result.structured_content["start_date"] == "2024-06-01T00:00:00+00:00"
    assert result.structured_content["end_date"] == "2024-06-30T23:59:59.999999+00:00"
    assert [r.url.params["page"] for r in fake.hevy_requests()] == ["1", "2"]
    assert all(r.url.params["pageSize"] == "10" for r in fake.hevy_requests())


async def test_workouts_in_range_stops_at_the_last_page(client, fake):
    fake.get(
        "/v1/workouts",
        json=page("workouts", [workout_at("w1", "2024-06-10T08:00:00Z")], page=1, page_count=1),
    )

    result = await client.call_tool(
        "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
    )

    assert [w["id"] for w in result.structured_content["workouts"]] == ["w1"]
    assert len(fake.hevy_requests()) == 1


async def test_workouts_in_range_flags_truncation_at_the_page_cap(make_client, fake):
    for n in range(1, 4):
        fake.get(
            "/v1/workouts",
            json=page(
                "workouts", [workout_at(f"w{n}", f"2024-06-{20 - n}T08:00:00Z")], n, page_count=9
            ),
            params={"page": n},
        )

    async with make_client(HEVY_RANGE_MAX_PAGES="2") as client:
        result = await client.call_tool(
            "get_workouts_in_range", {"start_date": "2024-01-01", "end_date": "2024-12-31"}
        )

    assert [w["id"] for w in result.structured_content["workouts"]] == ["w1", "w2"]
    assert result.structured_content["truncated"] is True
    assert len(fake.hevy_requests()) == 2


async def test_date_only_inputs_use_the_configured_timezone(make_client, fake):
    fake.get(
        "/v1/workouts",
        json=page(
            "workouts",
            [
                workout_at("late-on-the-30th-local", "2024-06-30T21:30:00Z"),
                workout_at("early-on-the-1st-local", "2024-05-31T22:30:00Z"),
            ],
        ),
    )

    async with make_client(TZ="Europe/Berlin") as client:
        result = await client.call_tool(
            "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
        )

    assert [w["id"] for w in result.structured_content["workouts"]] == [
        "late-on-the-30th-local",
        "early-on-the-1st-local",
    ]
    assert result.structured_content["start_date"] == "2024-06-01T00:00:00+02:00"


@pytest.mark.parametrize(
    ("start_date", "end_date", "message"),
    [
        ("last tuesday", "2024-06-30", "start_date"),
        ("2024-06-01", "2024-13-40", "end_date"),
        ("2024-06-30", "2024-06-01", "before"),
    ],
)
@pytest.mark.parametrize("tool", ["get_workouts_in_range", "get_training_summary"])
async def test_range_tools_reject_bad_dates(client, fake, tool, start_date, end_date, message):
    with pytest.raises(ToolError, match=message):
        await client.call_tool(tool, {"start_date": start_date, "end_date": end_date})

    assert fake.requests == []


async def test_training_summary_from_a_known_block(client, fake):
    fake.get(
        "/v1/workouts",
        json=page(
            "workouts", [load("hevy/workout_evening.json"), load("hevy/workout.json")], page_count=1
        ),
    )
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))

    result = await client.call_tool(
        "get_training_summary", {"start_date": "2024-06-01", "end_date": "2024-06-07"}
    )

    summary = result.structured_content
    assert summary["workout_count"] == 2
    assert summary["total_duration_seconds"] == 3600 + 2700
    # Working sets only: the 60 kg x 10 warm-up is excluded.
    assert summary["total_volume_kg"] == 100 * 8 + 100 * 7 + 140 * 5 + 140 * 4 + 102.5 * 5
    assert summary["total_sets"] == 7
    assert summary["total_reps"] == 8 + 7 + 5 + 4 + 5 + 10
    assert summary["truncated"] is False
    assert summary["exercises"] == [
        {
            "exercise_template_id": "05293BCA",
            "title": "Bench Press (Barbell)",
            "sets": 3,
            "reps": 20,
            "volume_kg": 2012.5,
            "top_weight_kg": 102.5,
        },
        {
            "exercise_template_id": "D04AC939",
            "title": "Squat (Barbell)",
            "sets": 2,
            "reps": 9,
            "volume_kg": 1260,
            "top_weight_kg": 140,
        },
        {
            "exercise_template_id": "1B2B1E7C",
            "title": "Pull Up",
            "sets": 1,
            "reps": 10,
            "volume_kg": 0,
            "top_weight_kg": None,
        },
        {
            "exercise_template_id": "243710DE",
            "title": "Treadmill",
            "sets": 1,
            "reps": 0,
            "volume_kg": 0,
            "top_weight_kg": None,
        },
    ]
    assert summary["muscle_groups"] == [
        {"muscle_group": "chest", "sets": 3},
        {"muscle_group": "quadriceps", "sets": 2},
        {"muscle_group": "cardio", "sets": 1},
        {"muscle_group": "lats", "sets": 1},
    ]
    [templates_request] = fake.hevy_requests("/v1/exercise_templates")
    assert templates_request.url.params["pageSize"] == "100"


async def test_training_summary_caches_exercise_templates(client, fake):
    fake.get("/v1/workouts", json=page("workouts", [load("hevy/workout.json")]))
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))
    week = {"start_date": "2024-06-01", "end_date": "2024-06-07"}

    await client.call_tool("get_training_summary", week)
    await client.call_tool("get_training_summary", week)

    assert len(fake.hevy_requests("/v1/workouts")) == 2
    assert len(fake.hevy_requests("/v1/exercise_templates")) == 1


async def test_training_summary_counts_unknown_templates_as_unknown(client, fake):
    fake.get("/v1/workouts", json=page("workouts", [load("hevy/workout.json")]))
    fake.get("/v1/exercise_templates", json=page("exercise_templates", []))

    result = await client.call_tool(
        "get_training_summary", {"start_date": "2024-06-01", "end_date": "2024-06-07"}
    )

    assert result.structured_content["muscle_groups"] == [{"muscle_group": "unknown", "sets": 4}]


def page_without_count(workouts: list[dict], number: int) -> dict:
    return {"page": number, "page_count": None, "workouts": workouts}


async def test_range_without_page_count_pages_until_a_short_page(client, fake):
    full = [workout_at(f"a{n}", f"2024-06-20T{n:02d}:00:00Z") for n in range(10)]
    short = [workout_at(f"b{n}", f"2024-06-10T{n:02d}:00:00Z") for n in range(3)]
    fake.get("/v1/workouts", json=page_without_count(full, 1), params={"page": 1})
    fake.get("/v1/workouts", json=page_without_count(short, 2), params={"page": 2})

    result = await client.call_tool(
        "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
    )

    assert len(result.structured_content["workouts"]) == 13
    assert result.structured_content["truncated"] is False
    assert [r.url.params["page"] for r in fake.hevy_requests()] == ["1", "2"]


async def test_range_without_page_count_stops_at_an_empty_page(client, fake):
    full = [workout_at(f"a{n}", f"2024-06-20T{n:02d}:00:00Z") for n in range(10)]
    fake.get("/v1/workouts", json=page_without_count(full, 1), params={"page": 1})
    fake.get("/v1/workouts", json={"page": 2, "workouts": []}, params={"page": 2})

    result = await client.call_tool(
        "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
    )

    assert len(result.structured_content["workouts"]) == 10
    assert result.structured_content["truncated"] is False


async def test_range_without_page_count_flags_truncation_at_the_cap(make_client, fake):
    for number in (1, 2):
        full = [
            workout_at(f"p{number}-{n}", f"2024-06-2{number}T{n:02d}:00:00Z") for n in range(10)
        ]
        fake.get("/v1/workouts", json=page_without_count(full, number), params={"page": number})

    async with make_client(HEVY_RANGE_MAX_PAGES="2") as client:
        result = await client.call_tool(
            "get_workouts_in_range", {"start_date": "2024-06-01", "end_date": "2024-06-30"}
        )

    assert len(result.structured_content["workouts"]) == 20
    assert result.structured_content["truncated"] is True
    assert len(fake.hevy_requests()) == 2


async def test_training_summary_flags_truncated_templates(make_client, fake):
    fake.get("/v1/workouts", json=page("workouts", [load("hevy/workout.json")]))
    templates = load("hevy/exercise_templates.json")["exercise_templates"]
    fake.get(
        "/v1/exercise_templates",
        json=page("exercise_templates", (templates * 25)[:100], page_count=3),
    )

    async with make_client(HEVY_RANGE_MAX_PAGES="1") as client:
        result = await client.call_tool(
            "get_training_summary", {"start_date": "2024-06-01", "end_date": "2024-06-07"}
        )

    assert result.structured_content["truncated"] is True
