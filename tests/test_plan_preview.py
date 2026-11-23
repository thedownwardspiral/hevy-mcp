"""The plan preview: preview_plan shows a plan, and save_plan saves that plan once."""

import json
import re
from typing import Any

import httpx
import pytest
from fastmcp.apps import UI_EXTENSION_ID
from fastmcp.exceptions import ToolError
from mcp.client.extension import advertise

from hevy_mcp.plan_preview import PendingPlan, PendingPlans, describe_sets, superset_labels

from .conftest import load, sent
from .test_journal import entries
from .test_writes import created_routine

BENCH = {"exercise_template_id": "05293BCA", "sets": [{"weight_kg": 100, "reps": 8}] * 3}
SQUAT = {
    "exercise_template_id": "D04AC939",
    "rest_seconds": 180,
    "sets": [{"type": "warmup", "weight_kg": 60, "reps": 5}, {"weight_kg": 140, "reps": 5}],
}
PLAN = {
    "title": "Upper Lower",
    "routines": [
        {"title": "Upper", "notes": "Heavy", "exercises": [BENCH]},
        {"title": "Lower", "exercises": [SQUAT]},
    ],
}


@pytest.fixture
async def ui_writer(make_client, fake, tmp_path):
    """A write-enabled client that can show app UIs, journaling to a file."""
    async with make_client(
        extensions=[advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})],
        HEVY_ENABLE_WRITES="true",
        HEVY_JOURNAL_PATH=str(tmp_path / "journal.jsonl"),
    ) as c:
        yield c
    assert not fake.unmatched, f"requests without a fake route: {fake.unmatched}"


@pytest.fixture
def hevy(fake):
    """Hevy with the template list, a lbs user, and room for a plan."""
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))
    user = load("hevy/user_info.json")
    user["data"] |= {"weight_unit": "lbs", "distance_unit": "miles"}
    fake.get("/v1/user/info", json=user)
    folder = load("hevy/routine_folder.json") | {"title": "Upper Lower"}
    fake.post("/v1/routine_folders", status=201, json={"routine_folder": folder})
    fake.post("/v1/routines", created_routine("Upper", "r1"), created_routine("Lower", "r2"))
    return fake


def writes(fake) -> list:
    return [r for r in fake.hevy_requests() if r.method != "GET"]


def preview_id(result) -> str:
    """The preview_id the model is told."""
    return re.search(r"preview_id is (\S+)\.", result.content[0].text).group(1)


def actions(result, kind: str) -> list[dict[str, Any]]:
    """Every action of one kind in a preview's UI tree."""
    found = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("action") == kind:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result.structured_content["view"])
    return found


async def preview(client, plan=PLAN):
    return await client.call_tool("preview_plan", plan)


async def test_preview_shows_the_plan_and_writes_nothing(ui_writer, hevy):
    result = await preview(ui_writer)

    ui = json.dumps(result.structured_content, ensure_ascii=False)
    for shown in ["Upper Lower", "Bench Press (Barbell)", "Squat (Barbell)", "Heavy", "3 min"]:
        assert shown in ui
    # 100 kg and 140 kg in the user's pounds, the warmup set counted apart.
    assert "3 × 8 reps @ 220.5 lb" in ui
    assert "5 reps @ 132.3 lb (warmup); 5 reps @ 308.6 lb" in ui
    assert writes(hevy) == []
    text = result.content[0].text
    assert "call save_plan with that preview_id" in text and "Don't call create_plan" in text
    assert "Don't repeat the plan" in text
    assert "1. Upper: Bench Press (Barbell)" in text
    assert "2. Lower: Squat (Barbell)" in text


async def test_the_button_asks_in_the_chat_and_calls_no_tool(ui_writer, hevy):
    result = await preview(ui_writer)

    [message] = actions(result, "sendMessage")
    assert (
        message["content"] == f"Save the plan 'Upper Lower' to Hevy (preview {preview_id(result)})."
    )
    # Claude refuses a card's own tool calls ("Method not found"), so there are none.
    assert actions(result, "toolCall") == []


async def test_save_plan_saves_exactly_what_was_previewed(ui_writer, hevy):
    result = await preview(ui_writer)

    saved = await ui_writer.call_tool("save_plan", {"preview_id": preview_id(result)})

    [folder] = hevy.hevy_requests("/v1/routine_folders")
    assert sent(folder) == {"routine_folder": {"title": "Upper Lower"}}
    upper, lower = (sent(r)["routine"] for r in hevy.hevy_requests("/v1/routines"))
    assert (upper["title"], upper["folder_id"], upper["notes"]) == ("Upper", 42, "Heavy")
    assert [(s["weight_kg"], s["reps"]) for s in upper["exercises"][0]["sets"]] == [(100, 8)] * 3
    assert (lower["title"], lower["exercises"][0]["rest_seconds"]) == ("Lower", 180)
    assert saved.structured_content["folder"]["id"] == 42
    assert [r["id"] for r in saved.structured_content["routines"]] == ["r1", "r2"]


async def test_a_preview_saves_once(ui_writer, hevy):
    arguments = {"preview_id": preview_id(await preview(ui_writer))}
    await ui_writer.call_tool("save_plan", arguments)

    with pytest.raises(ToolError, match="already saved or has expired"):
        await ui_writer.call_tool("save_plan", arguments)

    assert len(hevy.hevy_requests("/v1/routine_folders")) == 1


async def test_an_unknown_preview_saves_nothing(ui_writer, fake):
    with pytest.raises(ToolError, match="already saved or has expired"):
        await ui_writer.call_tool("save_plan", {"preview_id": "made-up"})

    assert writes(fake) == []


async def test_a_plan_that_stops_partway_says_what_exists(ui_writer, hevy):
    hevy.post(
        "/v1/routines",
        created_routine("Upper", "r1"),
        httpx.Response(403, json={"error": "routine-limit-exceeded"}),
    )
    arguments = {"preview_id": preview_id(await preview(ui_writer))}

    with pytest.raises(ToolError, match=r"routine 2 of 2, 'Lower'.*folder_id 42"):
        await ui_writer.call_tool("save_plan", arguments)


async def test_saving_is_journaled_as_save_plan(ui_writer, hevy, tmp_path):
    await ui_writer.call_tool("save_plan", {"preview_id": preview_id(await preview(ui_writer))})

    journal = entries(tmp_path / "journal.jsonl")
    assert {(e.get("tool"), e.get("kind")) for e in journal if "tool" in e} == {
        ("save_plan", "routine_folder"),
        ("save_plan", "routine"),
    }
    assert [e["outcome"] for e in journal if "outcome" in e] == ["ok", "ok", "ok"]


async def test_an_unknown_exercise_is_refused_before_anything_is_shown(ui_writer, fake):
    fake.get("/v1/exercise_templates", json=load("hevy/exercise_templates.json"))
    unknown = BENCH | {"exercise_template_id": "NOPE"}
    plan = {"title": "Odd", "routines": [{"title": "A", "exercises": [unknown]}]}

    with pytest.raises(ToolError, match="NOPE.*search_exercise_templates"):
        await preview(ui_writer, plan)

    assert writes(fake) == []


async def test_a_client_without_ui_gets_the_plan_as_text_and_saves_the_same_way(writer, hevy):
    result = await preview(writer)

    assert result.structured_content is None
    text = result.content[0].text
    assert "can't show the preview card" in text
    assert "   - Bench Press (Barbell): 3 × 8 reps @ 220.5 lb" in text
    assert writes(hevy) == []
    await writer.call_tool("save_plan", {"preview_id": preview_id(result)})
    assert len(hevy.hevy_requests("/v1/routines")) == 2


async def test_the_preview_tools_need_writes_enabled(client, writer):
    off = {t.name for t in await client.list_tools()}
    on = {t.name: t for t in await writer.list_tools()}

    assert {"preview_plan", "save_plan"}.isdisjoint(off)
    assert on["preview_plan"].meta["ui"]["resourceUri"].startswith("ui://prefab/")
    assert on["preview_plan"].annotations.read_only_hint is True
    assert on["save_plan"].annotations.read_only_hint is False


@pytest.mark.parametrize(
    ("sets", "unit", "text"),
    [
        ([{"reps": 8, "weight_kg": 60}] * 2, "kg", "2 × 8 reps @ 60 kg"),
        ([{"rep_range": {"start": 8, "end": 12}, "weight_kg": 20}], "lbs", "8–12 reps @ 44.1 lb"),
        ([{"duration_seconds": 300, "distance_meters": 1609.344}], "lbs", "1.00 mi, 5 min"),
        ([{"duration_seconds": 45}], "kg", "45s"),
        ([{"type": "failure", "reps": 10}], "kg", "10 reps (failure)"),
        ([{}], "kg", "no target"),
    ],
)
def test_sets_read_as_one_line(sets, unit, text):
    distance_unit = "miles" if unit == "lbs" else "kilometers"
    assert describe_sets(sets, unit, distance_unit) == text


def test_supersets_are_lettered_in_order_of_appearance():
    exercises = [{"superset_id": 3}, {}, {"superset_id": 0}, {"superset_id": 3}]

    assert superset_labels(exercises) == {3: "A", 0: "B"}


def test_previews_expire_and_the_oldest_gives_way():
    now = [0.0]
    pending = PendingPlans(seconds=60, limit=2, clock=lambda: now[0])
    plan = PendingPlan(title="P", routines=[])

    first, second = pending.add(plan), pending.add(plan)
    third = pending.add(plan)
    assert pending.take(first) is None
    now[0] = 61
    assert pending.take(second) is None
    assert pending.take(third) is None
