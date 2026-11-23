"""The write journal: what each write sent and, for updates, what it replaced."""

import json
import logging
import stat

import httpx
import pytest
from fastmcp.exceptions import ToolError

from .conftest import load

WORKOUT_ID = "b459cba5-cd6d-463c-abd6-54f8eafcadcb"
ROUTINE_ID = "c2ae1e2a-4d4b-4f0c-9a51-8f0c3e5d7b10"
BENCH = {"exercise_template_id": "05293BCA", "sets": [{"weight_kg": 100, "reps": 8}]}
NEW_WORKOUT = {
    "title": "Push",
    "start_time": "2024-06-03T07:00:00Z",
    "end_time": "2024-06-03T08:00:00Z",
    "exercises": [BENCH],
}


@pytest.fixture
def journal_file(tmp_path):
    return tmp_path / "journal.jsonl"


@pytest.fixture
async def journaling(make_client, fake, journal_file):
    """A client for a server with writes on and the journal in a file."""
    async with make_client(HEVY_ENABLE_WRITES="true", HEVY_JOURNAL_PATH=str(journal_file)) as c:
        yield c
    assert not fake.unmatched, f"requests without a fake route: {fake.unmatched}"


def entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def journal_records(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "hevy_mcp.journal"]


async def test_an_update_journals_the_record_it_replaces(journaling, fake, journal_file):
    original = load("hevy/workout.json")
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=original)
    fake.put(f"/v1/workouts/{WORKOUT_ID}", json=original)

    await journaling.call_tool(
        "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
    )

    [attempt, result] = entries(journal_file)
    assert attempt["event"] == "attempt"
    assert attempt["tool"] == "update_workout"
    assert attempt["kind"] == "workout"
    assert attempt["id"] == WORKOUT_ID
    assert attempt["caller"] == "unauthenticated"
    # Enough to put the workout back: the whole record, as Hevy returned it.
    assert attempt["before"] == original
    [put] = [r for r in fake.hevy_requests() if r.method == "PUT"]
    assert attempt["request"] == json.loads(put.content)["workout"]
    assert attempt["request"]["title"] == "Renamed"
    assert result == {
        "ts": result["ts"],
        "op": attempt["op"],
        "event": "result",
        "outcome": "ok",
        "id": WORKOUT_ID,
    }
    assert attempt["ts"].endswith("+00:00")


async def test_the_attempt_is_journaled_before_hevy_is_asked_to_change_anything(
    journaling, fake, journal_file
):
    seen = []

    def put(request: httpx.Request) -> httpx.Response:
        seen.extend(entries(journal_file))
        return httpx.Response(200, json=load("hevy/routine.json"))

    fake.get(f"/v1/routines/{ROUTINE_ID}", json=load("hevy/routine.json"))
    fake.put(f"/v1/routines/{ROUTINE_ID}", put)

    await journaling.call_tool("update_routine", {"routine_id": ROUTINE_ID, "notes": ""})

    [attempt] = seen
    assert attempt["event"] == "attempt"
    assert attempt["before"] == load("hevy/routine.json")["routine"]


@pytest.mark.parametrize(
    ("tool", "arguments", "path", "reply", "kind", "record_id"),
    [
        (
            "create_workout",
            NEW_WORKOUT,
            "/v1/workouts",
            httpx.Response(201, json={"workout": [load("hevy/workout.json")]}),
            "workout",
            WORKOUT_ID,
        ),
        (
            "create_routine",
            {"title": "Upper", "exercises": [BENCH]},
            "/v1/routines",
            httpx.Response(201, json=load("hevy/routine.json")),
            "routine",
            ROUTINE_ID,
        ),
        (
            "create_routine_folder",
            {"title": "Push Pull"},
            "/v1/routine_folders",
            httpx.Response(201, json={"routine_folder": load("hevy/routine_folder.json")}),
            "routine_folder",
            42,
        ),
        (
            "create_exercise_template",
            {
                "title": "Landmine Press",
                "exercise_type": "weight_reps",
                "equipment_category": "barbell",
                "muscle_group": "shoulders",
            },
            "/v1/exercise_templates",
            httpx.Response(200, text="16bb3c27-a14d-4afd-b86a-eebaaa515555"),
            "exercise_template",
            "16bb3c27-a14d-4afd-b86a-eebaaa515555",
        ),
        (
            "create_body_measurement",
            {"date": "2024-08-14", "measurements": {"weight_kg": 80.5}},
            "/v1/body_measurements",
            httpx.Response(201),
            "body_measurement",
            "2024-08-14",
        ),
    ],
)
async def test_a_create_journals_what_was_sent_and_the_new_id(
    journaling, fake, journal_file, tool, arguments, path, reply, kind, record_id
):
    fake.post(path, reply)

    await journaling.call_tool(tool, arguments)

    [attempt, result] = entries(journal_file)
    assert (attempt["tool"], attempt["kind"]) == (tool, kind)
    assert "before" not in attempt
    [post] = fake.hevy_requests(path)
    sent = json.loads(post.content)
    # Hevy wraps most bodies in one key; a body measurement is sent bare, with its date.
    wrapped = next(iter(sent.values()))
    assert attempt["request"] == wrapped or attempt["request"].items() <= sent.items()
    assert (result["outcome"], result["id"]) == ("ok", record_id)


async def test_create_plan_journals_the_folder_and_each_routine(journaling, fake, journal_file):
    fake.post(
        "/v1/routine_folders", status=201, json={"routine_folder": load("hevy/routine_folder.json")}
    )
    fake.post("/v1/routines", status=201, json=load("hevy/routine.json"))

    await journaling.call_tool(
        "create_plan", {"title": "Push Pull", "routines": [{"title": "Push", "exercises": [BENCH]}]}
    )

    folder_try, folder_done, routine_try, routine_done = entries(journal_file)
    assert (folder_try["tool"], folder_try["kind"]) == ("create_plan", "routine_folder")
    assert (folder_done["outcome"], folder_done["id"]) == ("ok", 42)
    assert (routine_try["tool"], routine_try["kind"]) == ("create_plan", "routine")
    assert routine_try["request"]["folder_id"] == 42
    assert (routine_done["outcome"], routine_done["id"]) == ("ok", ROUTINE_ID)


async def test_update_body_measurement_journals_the_day_as_it_was(journaling, fake, journal_file):
    fake.get("/v1/body_measurements/2024-08-14", json=load("hevy/body_measurement.json"))
    fake.put("/v1/body_measurements/2024-08-14", httpx.Response(200))

    await journaling.call_tool(
        "update_body_measurement", {"date": "2024-08-14", "clear": ["neck_cm"]}
    )

    [attempt, result] = entries(journal_file)
    assert attempt["id"] == "2024-08-14"
    assert attempt["before"] == load("hevy/body_measurement.json")
    assert "neck_cm" not in attempt["request"]
    assert result["outcome"] == "ok"


async def test_a_refused_write_is_journaled_as_rejected(journaling, fake, journal_file):
    fake.post("/v1/body_measurements", status=409, json={"error": "already exists"})

    with pytest.raises(ToolError, match="already exists"):
        await journaling.call_tool(
            "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
        )

    [_, result] = entries(journal_file)
    assert result["outcome"] == "rejected"
    assert "already exists" in result["error"]


async def test_a_refusal_after_an_unanswered_try_is_journaled_as_uncertain(
    journaling, fake, journal_file
):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    # The first PUT may have been saved before the 502; only the retry is refused.
    fake.put(f"/v1/workouts/{WORKOUT_ID}", httpx.Response(502), httpx.Response(401))

    with pytest.raises(ToolError, match="rejected the API key"):
        await journaling.call_tool(
            "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
        )

    [_, result] = entries(journal_file)
    assert result["outcome"] == "uncertain"
    assert "rejected the API key" in result["error"]


async def test_a_refusal_after_a_rate_limit_is_still_rejected(journaling, fake, journal_file):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    # A 429 means Hevy didn't act on the first PUT.
    fake.put(f"/v1/workouts/{WORKOUT_ID}", httpx.Response(429), httpx.Response(400, json={}))

    with pytest.raises(ToolError):
        await journaling.call_tool(
            "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
        )

    [_, result] = entries(journal_file)
    assert result["outcome"] == "rejected"


async def test_a_create_hevy_may_have_saved_is_journaled_as_uncertain(
    journaling, fake, journal_file
):
    fake.post("/v1/workouts", status=502)

    with pytest.raises(ToolError, match="may or may not have worked"):
        await journaling.call_tool("create_workout", NEW_WORKOUT)

    [attempt, result] = entries(journal_file)
    assert attempt["request"]["title"] == "Push"
    assert result["outcome"] == "uncertain"
    assert "id" not in result


async def test_a_write_whose_answer_cannot_be_read_is_journaled_as_uncertain(
    journaling, fake, journal_file
):
    fake.post("/v1/workouts", httpx.Response(201, json={"workout": "not a workout"}))

    with pytest.raises(ToolError):
        await journaling.call_tool("create_workout", NEW_WORKOUT)

    [_, result] = entries(journal_file)
    assert result["outcome"] == "uncertain"


async def test_calls_that_never_reach_hevy_are_not_journaled(journaling, fake, journal_file):
    fake.get("/v1/workouts/gone", status=404, json={"error": "Workout not found"})
    fake.get("/v1/user/info", json=load("hevy/user_info.json"))

    with pytest.raises(ToolError, match="(?i)not found"):
        await journaling.call_tool("update_workout", {"workout_id": "gone", "is_private": False})
    with pytest.raises(ToolError, match="end_time"):
        await journaling.call_tool(
            "create_workout", NEW_WORKOUT | {"end_time": "2024-06-03T06:00:00Z"}
        )
    await journaling.call_tool("get_user_info", {})

    assert entries(journal_file) == []


async def test_text_from_a_call_cannot_forge_a_journal_entry(journaling, fake, journal_file):
    fake.post(
        "/v1/routine_folders",
        httpx.Response(201, json={"routine_folder": load("hevy/routine_folder.json")}),
    )
    forged = 'x"}\n{"op":"forged","event":"result","outcome":"ok"}'

    await journaling.call_tool("create_routine_folder", {"title": forged})

    lines = journal_file.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request"] == {"title": forged}


async def test_the_journal_file_is_private_and_appended_to(journaling, fake, journal_file):
    fake.post("/v1/body_measurements", httpx.Response(201))
    journal_file.write_text('{"op":"earlier"}\n')
    journal_file.chmod(0o600)

    await journaling.call_tool(
        "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
    )

    assert [e["op"] for e in entries(journal_file)][0] == "earlier"
    assert len(entries(journal_file)) == 3
    assert stat.S_IMODE(journal_file.stat().st_mode) == 0o600


async def test_a_new_journal_file_is_readable_only_by_its_owner(journaling, journal_file):
    # Startup creates it, to prove it can be written before any write is accepted.
    assert journal_file.read_text() == ""
    assert stat.S_IMODE(journal_file.stat().st_mode) == 0o600


async def test_with_a_journal_file_the_log_points_at_entries_without_their_contents(
    journaling, fake, journal_file, caplog
):
    caplog.set_level(logging.INFO)
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    fake.put(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))

    await journaling.call_tool(
        "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
    )

    [attempt, result] = entries(journal_file)
    assert journal_records(caplog) == [
        f"{attempt['op']} attempt tool='update_workout' id='{WORKOUT_ID}'",
        f"{attempt['op']} result id='{WORKOUT_ID}' outcome='ok'",
    ]
    assert "Renamed" not in caplog.text
    assert "Bench Press" not in caplog.text


async def test_without_a_journal_file_entries_go_to_the_log(writer, fake, caplog):
    caplog.set_level(logging.INFO)
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    fake.put(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))

    await writer.call_tool(
        "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
    )

    [attempt, result] = [json.loads(message) for message in journal_records(caplog)]
    assert attempt["before"] == load("hevy/workout.json")
    assert attempt["request"]["title"] == "Renamed"
    assert (result["op"], result["outcome"]) == (attempt["op"], "ok")


async def test_a_quiet_log_level_does_not_drop_journal_entries(writer, fake, caplog):
    caplog.set_level(logging.ERROR)  # as LOG_LEVEL=ERROR sets the root logger
    caplog.handler.setLevel(logging.NOTSET)
    fake.post("/v1/body_measurements", httpx.Response(201))

    await writer.call_tool(
        "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
    )

    [attempt, result] = [json.loads(message) for message in journal_records(caplog)]
    assert (attempt["event"], result["outcome"]) == ("attempt", "ok")


async def test_an_entry_after_one_cut_short_is_still_a_line_of_its_own(
    journaling, fake, journal_file
):
    # What a full disk leaves behind: part of an entry, with no newline.
    journal_file.write_text('{"ts":"2024-08-14T07:00:00+00:00","op":"0badc0de","eve')
    fake.post("/v1/body_measurements", httpx.Response(201))

    await journaling.call_tool(
        "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
    )

    [fragment, attempt, result] = journal_file.read_text().splitlines()
    assert fragment.endswith('"eve')
    assert json.loads(attempt)["event"] == "attempt"
    assert json.loads(result)["outcome"] == "ok"


async def test_a_write_is_refused_when_its_journal_entry_cannot_be_saved(
    journaling, fake, journal_file
):
    fake.get(f"/v1/workouts/{WORKOUT_ID}", json=load("hevy/workout.json"))
    journal_file.unlink()
    journal_file.mkdir()  # appending to a directory fails, as a full disk would

    with pytest.raises(ToolError, match="journal can't be saved.*nothing was sent to Hevy"):
        await journaling.call_tool(
            "update_workout", {"workout_id": WORKOUT_ID, "title": "Renamed", "is_private": False}
        )
    with pytest.raises(ToolError, match="journal can't be saved"):
        await journaling.call_tool("create_workout", NEW_WORKOUT)

    assert [r.method for r in fake.hevy_requests()] == ["GET"]


async def test_a_lost_result_entry_does_not_fail_a_write_that_worked(
    journaling, fake, journal_file, caplog
):
    def post(request: httpx.Request) -> httpx.Response:
        journal_file.unlink()
        journal_file.mkdir()
        return httpx.Response(201)

    fake.post("/v1/body_measurements", post)

    result = await journaling.call_tool(
        "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
    )

    assert result.structured_content["weight_kg"] == 80
    [lost] = [r for r in caplog.records if "journal entry was lost" in r.getMessage()]
    assert lost.levelno == logging.ERROR


async def test_startup_fails_when_the_journal_file_cannot_be_written(make_client, tmp_path):
    missing = tmp_path / "no-such-directory" / "journal.jsonl"

    with pytest.raises(RuntimeError) as exc:
        async with make_client(HEVY_ENABLE_WRITES="true", HEVY_JOURNAL_PATH=str(missing)):
            pass

    assert "journal can't be saved" in str(exc.value)


async def test_the_journal_path_is_ignored_while_writes_are_off(make_client, tmp_path):
    missing = tmp_path / "no-such-directory" / "journal.jsonl"

    async with make_client(HEVY_JOURNAL_PATH=str(missing)) as client:
        tools = {t.name for t in await client.list_tools()}

    assert "create_workout" not in tools
    assert not missing.parent.exists()


async def test_a_blank_journal_path_means_the_log(make_client, fake, caplog):
    caplog.set_level(logging.INFO)
    fake.post("/v1/body_measurements", httpx.Response(201))

    async with make_client(HEVY_ENABLE_WRITES="true", HEVY_JOURNAL_PATH=" ") as client:
        await client.call_tool(
            "create_body_measurement", {"date": "2024-08-14", "measurements": {"weight_kg": 80}}
        )

    assert len(journal_records(caplog)) == 2
