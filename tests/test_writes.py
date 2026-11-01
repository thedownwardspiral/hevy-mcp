"""The tools that create and update Hevy data, enabled by HEVY_ENABLE_WRITES."""

import typing

import httpx
import pytest
from fastmcp.exceptions import ToolError

from hevy_mcp.models import BodyMeasurementInput, MeasurementField

from .conftest import load, sent

WORKOUT_ID = "b459cba5-cd6d-463c-abd6-54f8eafcadcb"
ROUTINE_ID = "c2ae1e2a-4d4b-4f0c-9a51-8f0c3e5d7b10"
BENCH = {"exercise_template_id": "05293BCA", "sets": [{"weight_kg": 100, "reps": 8, "rpe": 8.5}]}
PLANNED = {"exercise_template_id": "05293BCA", "sets": [{"weight_kg": 100, "reps": 8}]}


def new_workout(**overrides):
    return {
        "title": "Push",
        "start_time": "2024-06-03T07:00:00Z",
        "end_time": "2024-06-03T08:00:00Z",
        "exercises": [BENCH],
    } | overrides


# Workouts


async def test_create_workout_posts_the_workout_and_returns_it(writer, fake):
    fake.post("/v1/workouts", status=201, json={"workout": [load("hevy/workout.json")]})

    result = await writer.call_tool("create_workout", new_workout())

    assert result.structured_content["id"] == WORKOUT_ID
    [request] = fake.hevy_requests("/v1/workouts")
    assert sent(request) == {
        "workout": {
            "title": "Push",
            "description": None,
            "start_time": "2024-06-03T07:00:00Z",
            "end_time": "2024-06-03T08:00:00Z",
            "is_private": False,
            "exercises": [
                {
                    "exercise_template_id": "05293BCA",
                    "superset_id": None,
                    "notes": None,
                    "sets": [
                        {
                            "type": "normal",
                            "weight_kg": 100.0,
                            "reps": 8,
                            "distance_meters": None,
                            "duration_seconds": None,
                            "custom_metric": None,
                            "rpe": 8.5,
                        }
                    ],
                }
            ],
        }
    }


async def test_create_workout_reads_times_without_offset_in_server_timezone(make_client, fake):
    fake.post("/v1/workouts", status=201, json={"workout": [load("hevy/workout.json")]})

    async with make_client(HEVY_ENABLE_WRITES="true", TZ="Europe/Paris") as writer:
        await writer.call_tool(
            "create_workout",
            new_workout(start_time="2024-06-03T09:00:00", end_time="2024-06-03T10:00:00"),
        )

    [request] = fake.hevy_requests("/v1/workouts")
    assert sent(request)["workout"]["start_time"] == "2024-06-03T07:00:00Z"
    assert sent(request)["workout"]["end_time"] == "2024-06-03T08:00:00Z"


async def test_create_workout_rejects_end_before_start(writer, fake):
    with pytest.raises(ToolError, match="end_time.*before start_time"):
        await writer.call_tool("create_workout", new_workout(end_time="2024-06-03T06:00:00Z"))

    assert fake.hevy_requests() == []


@pytest.mark.parametrize(
    "bad_set",
    [{"reps": 8, "rpe": 11}, {"reps": -1}, {"reps": 8, "type": "amrap"}, {"reps": 8, "kg": 100}],
)
async def test_create_workout_rejects_invalid_sets(writer, fake, bad_set):
    exercise = {"exercise_template_id": "05293BCA", "sets": [bad_set]}

    with pytest.raises(ToolError):
        await writer.call_tool("create_workout", new_workout(exercises=[exercise]))

    assert fake.hevy_requests() == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_create_is_not_retried_after_server_error(writer, fake, status):
    fake.post("/v1/workouts", status=status)

    with pytest.raises(ToolError, match="may or may not have worked.*Check whether it exists"):
        await writer.call_tool("create_workout", new_workout())

    assert len(fake.hevy_requests("/v1/workouts")) == 1


async def test_create_is_not_retried_after_timeout(writer, fake):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fake.post("/v1/workouts", timeout)

    with pytest.raises(ToolError, match="may or may not have worked.*timed out"):
        await writer.call_tool("create_workout", new_workout())

    assert len(fake.hevy_requests("/v1/workouts")) == 1


async def test_create_is_retried_when_hevy_cannot_have_acted(writer, fake):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake.post(
        "/v1/workouts",
        refused,
        httpx.Response(429),
        httpx.Response(201, json={"workout": [load("hevy/workout.json")]}),
    )

    result = await writer.call_tool("create_workout", new_workout())

    assert result.structured_content["id"] == WORKOUT_ID
    assert len(fake.hevy_requests("/v1/workouts")) == 3


async def test_update_workout_keeps_what_it_is_not_told_to_change(writer, fake):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    fake.put(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))

    await writer.call_tool(
        "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": True}
    )

    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    body = sent(put)["workout"]
    original = load("hevy/workout.json")
    assert body["title"] == "Renamed"
    assert body["is_private"] is True
    assert body["description"] == original["description"]
    assert body["start_time"] == original["start_time"]
    assert body["end_time"] == original["end_time"]
    # Hevy's read-only fields (an exercise's title and index) aren't sent back.
    [bench, squat] = body["exercises"]
    assert set(bench) == {"exercise_template_id", "superset_id", "notes", "sets"}
    assert bench["notes"] == original["exercises"][0]["notes"]
    assert [s["weight_kg"] for s in squat["sets"]] == [140, 140]
    assert "index" not in squat["sets"][0]
    assert squat["sets"][1]["type"] == "failure"


async def test_update_workout_replaces_exercises_when_given(writer, fake):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    fake.put(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))

    await writer.call_tool(
        "update_workout", {"workout_id": WORKOUT_ID, "is_private": False, "exercises": [BENCH]}
    )

    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    [exercise] = sent(put)["workout"]["exercises"]
    assert exercise["sets"][0]["reps"] == 8


async def test_update_workout_needs_is_private(writer, fake):
    with pytest.raises(ToolError, match="is_private"):
        await writer.call_tool("update_workout", {"workout_id": WORKOUT_ID, "title": "x"})

    assert fake.hevy_requests() == []


async def test_update_workout_checks_a_new_end_against_the_stored_start(writer, fake):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))

    with pytest.raises(ToolError, match="end_time.*before start_time"):
        await writer.call_tool(
            "update_workout",
            {"workout_id": WORKOUT_ID, "is_private": False, "end_time": "2024-06-03T06:00:00Z"},
        )

    assert [r.method for r in fake.hevy_requests()] == ["GET"]


async def test_update_is_retried_after_server_error(writer, fake):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    fake.put(
        f"/v1/workouts/{WORKOUT_ID}",
        httpx.Response(503),
        httpx.Response(200, json=load("hevy/workout.json")),
    )

    await writer.call_tool("update_workout", {"workout_id": WORKOUT_ID, "is_private": False})

    assert [r.method for r in fake.hevy_requests()] == ["GET", "PUT", "PUT"]


async def test_update_workout_not_found(writer, fake):
    fake.get("/v1/workouts/gone", status=404, json={"error": "Workout not found"})

    with pytest.raises(ToolError, match="(?i)not found.*gone"):
        await writer.call_tool("update_workout", {"workout_id": "gone", "is_private": False})


# Routines and folders


async def test_create_routine_posts_planned_sets(writer, fake):
    fake.post("/v1/routines", status=201, json=load("hevy/routine.json"))
    exercise = {
        "exercise_template_id": "05293BCA",
        "rest_seconds": 90,
        "sets": [
            {"type": "warmup", "weight_kg": 60, "reps": 10},
            {"weight_kg": 100, "rep_range": {"start": 8, "end": 12}},
        ],
    }

    result = await writer.call_tool(
        "create_routine", {"title": "Upper", "exercises": [exercise], "folder_id": 42}
    )

    assert result.structured_content["id"] == ROUTINE_ID
    [request] = fake.hevy_requests("/v1/routines")
    routine = sent(request)["routine"]
    assert routine["folder_id"] == 42
    assert routine["notes"] is None
    [warmup, planned] = routine["exercises"][0]["sets"]
    assert planned["rep_range"] == {"start": 8, "end": 12}
    assert "rpe" not in planned
    # Hevy refuses a null rep_range, so a set without one leaves it out.
    assert "rep_range" not in warmup


async def test_update_routine_can_move_back_to_my_routines(writer, fake):
    fake.get(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))
    fake.put(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))

    await writer.call_tool(
        "update_routine", {"routine_id": ROUTINE_ID, "notes": "", "folder_id": "my_routines"}
    )

    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    assert sent(put)["routine"]["folder_id"] is None


@pytest.mark.parametrize("tool", ["create_workout", "create_routine"])
async def test_negative_custom_metric_is_refused(writer, fake, tool):
    exercise = {"exercise_template_id": "05293BCA", "sets": [{"custom_metric": -1}]}
    arguments = (
        new_workout(exercises=[exercise])
        if tool == "create_workout"
        else {
            "title": "Upper",
            "exercises": [exercise],
        }
    )

    with pytest.raises(ToolError, match="custom_metric"):
        await writer.call_tool(tool, arguments)

    assert fake.hevy_requests() == []


async def test_create_routine_rejects_a_backwards_rep_range(writer, fake):
    exercise = {
        "exercise_template_id": "05293BCA",
        "sets": [{"rep_range": {"start": 12, "end": 8}}],
    }

    with pytest.raises(ToolError, match="rep_range"):
        await writer.call_tool("create_routine", {"title": "Upper", "exercises": [exercise]})

    assert fake.hevy_requests() == []


async def test_create_routine_over_the_limit_is_not_an_auth_error(writer, fake):
    fake.post("/v1/routines", status=403, json={"error": "routine-limit-exceeded"})

    with pytest.raises(ToolError, match="routine limit is reached.*routine-limit-exceeded"):
        await writer.call_tool("create_routine", {"title": "Upper", "exercises": [PLANNED]})


async def test_update_routine_keeps_what_it_is_not_told_to_change(writer, fake):
    fake.get(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))
    fake.put(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))

    result = await writer.call_tool(
        "update_routine", {"routine_id": ROUTINE_ID, "title": "Upper", "notes": ""}
    )

    assert result.structured_content["id"] == ROUTINE_ID
    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    body = sent(put)["routine"]
    assert body["title"] == "Upper"
    assert body["folder_id"] == 42
    assert body["notes"] == ""
    [bench] = body["exercises"]
    assert bench["rest_seconds"] == 90
    assert bench["sets"][1]["rep_range"] == {"start": 8, "end": 12}
    assert "rep_range" not in bench["sets"][0]
    assert "title" not in bench


async def test_update_routine_needs_notes(writer, fake):
    with pytest.raises(ToolError, match="notes"):
        await writer.call_tool("update_routine", {"routine_id": ROUTINE_ID, "title": "x"})

    assert fake.hevy_requests() == []


async def test_update_routine_sets_notes_and_folder_when_given(writer, fake):
    fake.get(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))
    fake.put(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))

    await writer.call_tool(
        "update_routine", {"routine_id": ROUTINE_ID, "notes": "Slow negatives", "folder_id": 7}
    )

    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    assert sent(put)["routine"]["notes"] == "Slow negatives"
    assert sent(put)["routine"]["folder_id"] == 7


async def test_create_routine_folder(writer, fake):
    fake.post(
        "/v1/routine_folders",
        status=201,
        json={"routine_folder": load("hevy/routine_folder.json")},
    )

    result = await writer.call_tool("create_routine_folder", {"title": "Push Pull 🏋️‍♂️"})

    assert result.structured_content["id"] == 42
    [request] = fake.hevy_requests("/v1/routine_folders")
    assert sent(request) == {"routine_folder": {"title": "Push Pull 🏋️‍♂️"}}


# Custom exercises


# Hevy answers with the bare id as text; its docs promise {"id": <int>}.
@pytest.mark.parametrize(
    ("reply", "template_id"),
    [
        (
            httpx.Response(200, text="16bb3c27-a14d-4afd-b86a-eebaaa515555"),
            "16bb3c27-a14d-4afd-b86a-eebaaa515555",
        ),
        (httpx.Response(200, json={"id": 123}), "123"),
    ],
)
async def test_create_exercise_template_returns_the_new_template(writer, fake, reply, template_id):
    fake.post("/v1/exercise_templates", reply)

    result = await writer.call_tool(
        "create_exercise_template",
        {
            "title": "Landmine Press",
            "exercise_type": "weight_reps",
            "equipment_category": "barbell",
            "muscle_group": "shoulders",
            "other_muscles": ["triceps"],
        },
    )

    assert result.structured_content == {
        "id": template_id,
        "title": "Landmine Press",
        "type": "weight_reps",
        "primary_muscle_group": "shoulders",
        "secondary_muscle_groups": ["triceps"],
        "equipment": "barbell",
        "is_custom": True,
    }
    [request] = fake.hevy_requests("/v1/exercise_templates")
    assert sent(request)["exercise"]["other_muscles"] == ["triceps"]


async def test_create_exercise_template_refreshes_the_search_cache(writer, fake):
    templates = load("hevy/exercise_templates.json")
    custom = {"id": "123", "title": "Landmine Press", "is_custom": True}
    fake.get("/v1/exercise_templates", json=templates)
    await writer.call_tool("search_exercise_templates", {"query": "press"})
    fake.post("/v1/exercise_templates", json={"id": 123})
    fake.get("/v1/exercise_templates", json=templates | {"exercise_templates": [custom]})

    await writer.call_tool(
        "create_exercise_template",
        {
            "title": "Landmine Press",
            "exercise_type": "weight_reps",
            "equipment_category": "barbell",
            "muscle_group": "shoulders",
        },
    )
    result = await writer.call_tool("search_exercise_templates", {"query": "landmine"})

    assert [t["id"] for t in result.structured_content["exercise_templates"]] == ["123"]


async def test_create_exercise_template_over_the_limit(writer, fake):
    fake.post("/v1/exercise_templates", status=403, json={"error": "exceeds-custom-exercise-limit"})

    with pytest.raises(ToolError, match="custom exercise limit is reached"):
        await writer.call_tool(
            "create_exercise_template",
            {
                "title": "Landmine Press",
                "exercise_type": "weight_reps",
                "equipment_category": "barbell",
                "muscle_group": "shoulders",
            },
        )


# Body measurements


async def test_create_body_measurement_posts_only_the_given_values(writer, fake):
    fake.post("/v1/body_measurements", httpx.Response(201))

    result = await writer.call_tool(
        "create_body_measurement",
        {"date": "2024-08-14", "measurements": {"weight_kg": 80.5, "waist": 80}},
    )

    assert result.structured_content["weight_kg"] == 80.5
    [request] = fake.hevy_requests("/v1/body_measurements")
    assert sent(request) == {"date": "2024-08-14", "weight_kg": 80.5, "waist": 80.0}


async def test_create_body_measurement_for_a_logged_day_is_a_conflict(writer, fake):
    fake.post("/v1/body_measurements", status=409, json={"error": "already exists"})

    with pytest.raises(ToolError, match="(?i)conflict.*2024-08-14 already exists"):
        await writer.call_tool(
            "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
        )


async def test_create_body_measurement_needs_a_value(writer, fake):
    with pytest.raises(ToolError, match="at least one measurement"):
        await writer.call_tool(
            "create_body_measurement", {"date": "2024-08-14", "measurements": {}}
        )

    assert fake.hevy_requests() == []


async def test_update_body_measurement_keeps_other_values(writer, fake):
    fake.get("/v1/body_measurements/2024-08-14", json=load("hevy/body_measurement.json"))
    fake.put("/v1/body_measurements/2024-08-14", httpx.Response(200))

    result = await writer.call_tool(
        "update_body_measurement",
        {"date": "2024-08-14", "changes": {"weight_kg": 79.9}, "clear": ["neck_cm"]},
    )

    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    expected = load("hevy/body_measurement.json")
    del expected["date"], expected["neck_cm"]
    expected["weight_kg"] = 79.9
    # Hevy refuses nulls; a field left out is the one it empties.
    assert sent(put) == expected
    assert result.structured_content["weight_kg"] == 79.9
    assert result.structured_content["waist"] == 80
    assert result.structured_content["neck_cm"] is None


async def test_hevy_validation_errors_are_reported_on_one_line(writer, fake):
    fake.get("/v1/body_measurements/2024-08-14", json=load("hevy/body_measurement.json"))
    issues = (
        '[\n  {\n    "path": [\n      "waist"\n    ],\n    "message": "Expected number"\n  }\n]'
    )
    fake.put("/v1/body_measurements/2024-08-14", status=400, json={"error": issues})

    with pytest.raises(ToolError) as caught:
        await writer.call_tool(
            "update_body_measurement", {"date": "2024-08-14", "changes": {"waist": 81}}
        )

    assert str(caught.value) == (
        'Hevy rejected the request (HTTP 400): [ { "path": [ "waist" ], '
        '"message": "Expected number" } ]'
    )


async def test_update_body_measurement_not_found(writer, fake):
    fake.get("/v1/body_measurements/2024-08-15", status=404, json={"error": "not found"})

    with pytest.raises(ToolError, match="(?i)not found.*2024-08-15"):
        await writer.call_tool(
            "update_body_measurement", {"date": "2024-08-15", "changes": {"weight_kg": 80}}
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "at least one change"),
        ({"changes": {"waist": 80}, "clear": ["waist"]}, "both changed and cleared: waist"),
    ],
)
async def test_update_body_measurement_rejects_empty_or_contradictory_calls(
    writer, fake, arguments, message
):
    with pytest.raises(ToolError, match=message):
        await writer.call_tool("update_body_measurement", {"date": "2024-08-14"} | arguments)

    assert fake.hevy_requests() == []


def test_clearable_fields_match_the_measurement_fields():
    clearable = typing.get_args(MeasurementField.__value__)

    assert clearable == tuple(BodyMeasurementInput.model_fields)
