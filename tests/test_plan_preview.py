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


def find(result, key: str, value: str) -> list[dict[str, Any]]:
    """Every node in a preview's UI tree whose key has this value."""
    found = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get(key) == value:
                found.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(result.structured_content["view"])
    return found


def actions(result, kind: str) -> list[dict[str, Any]]:
    """Every action of one kind in a preview's UI tree."""
    return find(result, "action", kind)


async def preview(client, plan=PLAN):
    return await client.call_tool("preview_plan", plan)


async def test_preview_shows_the_plan_and_writes_nothing(ui_writer, hevy):
    result = await preview(ui_writer)

    ui = json.dumps(result.structured_content, ensure_ascii=False)
    for shown in ["Upper Lower", "Bench Press (Barbell)", "Squat (Barbell)", "Heavy", "3 min"]:
        assert shown in ui
    # 100 kg and 140 kg in the user's pounds, the warmup set counted apart.
    assert "3 × 8 reps @ 220.5 lb" in ui
    assert '"5 reps @ 132.3 lb (warmup)"' in ui and '"5 reps @ 308.6 lb"' in ui
    assert writes(hevy) == []
    text = result.content[0].text
    assert "call save_plan with that preview_id" in text and "Don't call create_plan" in text
    assert "Don't repeat the plan" in text
    # In case the card doesn't appear, the model has the whole plan to show.
    assert "didn't appear" in text
    assert "   - Bench Press (Barbell): 3 × 8 reps @ 220.5 lb" in text
    assert "   - Squat (Barbell): 5 reps @ 132.3 lb (warmup); 5 reps @ 308.6 lb; rest 3 min" in text


def texts(node: Any) -> list[str]:
    """The text a component shows, in order."""
    if isinstance(node, dict):
        own = [node["content"]] if isinstance(node.get("content"), str) else []
        return own + [t for child in node.get("children", []) for t in texts(child)]
    return []


async def test_long_sets_wrap_and_keep_rest_on_the_card(ui_writer, hevy):
    # Prefab's table cells don't wrap, so one long line of sets used to widen
    # the table and scroll the Rest column out of the card.
    sets = [{"type": "warmup", "reps": 10, "weight_kg": 40}, {"type": "warmup", "reps": 5}]
    sets += [{"rep_range": {"start": 6, "end": 8}, "weight_kg": 100}] * 4
    sets += [{"type": "dropset", "reps": 12, "weight_kg": 70}]
    plan = {"title": "P", "routines": [{"title": "A", "exercises": [SQUAT | {"sets": sets}]}]}

    result = await preview(ui_writer, plan)

    [row] = [r for r in find(result, "type", "TableRow") if "3 min" in texts(r)]
    name, sets_cell, rest = row["children"]
    for cell in (name, sets_cell):
        assert "whitespace-normal" in cell["cssClass"].split()
    assert "whitespace-normal" not in (rest.get("cssClass") or "")
    assert texts(sets_cell) == [
        "10 reps @ 88.2 lb (warmup)",
        "5 reps (warmup)",
        "4 × 6–8 reps @ 220.5 lb",
        "12 reps @ 154.3 lb (dropset)",
    ]
    assert texts(rest) == ["3 min"]


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


async def test_a_save_hevy_refused_before_writing_can_be_tried_again(ui_writer, hevy):
    folder = load("hevy/routine_folder.json") | {"title": "Upper Lower"}
    hevy.post(
        "/v1/routine_folders",
        httpx.Response(400, json={"error": "bad request"}),
        httpx.Response(201, json={"routine_folder": folder}),
    )
    arguments = {"preview_id": preview_id(await preview(ui_writer))}
    with pytest.raises(ToolError, match="400"):
        await ui_writer.call_tool("save_plan", arguments)

    saved = await ui_writer.call_tool("save_plan", arguments)

    assert saved.structured_content["folder"]["id"] == 42
    assert len(hevy.hevy_requests("/v1/routines")) == 2


async def test_a_save_that_may_have_written_the_folder_is_not_repeated(ui_writer, hevy):
    # A lost answer to the folder's POST: Hevy may have created it.
    hevy.post("/v1/routine_folders", httpx.Response(500, json={"error": "oops"}))
    arguments = {"preview_id": preview_id(await preview(ui_writer))}
    with pytest.raises(ToolError):
        await ui_writer.call_tool("save_plan", arguments)

    with pytest.raises(ToolError, match="already saved or has expired"):
        await ui_writer.call_tool("save_plan", arguments)


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


async def test_an_unknown_exercise_is_refused_before_anything_is_shown(ui_writer, hevy):
    hevy.get("/v1/exercise_templates/NOPE", status=404, json={"error": "not found"})
    unknown = BENCH | {"exercise_template_id": "NOPE"}
    plan = {"title": "Odd", "routines": [{"title": "A", "exercises": [unknown]}]}

    with pytest.raises(ToolError, match="NOPE.*search_exercise_templates"):
        await preview(ui_writer, plan)

    assert writes(hevy) == []


async def test_an_exercise_the_template_list_lacks_is_looked_up(ui_writer, hevy):
    # Made in the Hevy app after the list was cached, or past its page cap.
    template = load("hevy/exercise_template.json") | {"id": "NEW1", "title": "Cable Fly"}
    hevy.get("/v1/exercise_templates/NEW1", json=template)
    fly = BENCH | {"exercise_template_id": "NEW1"}
    plan = {"title": "Chest", "routines": [{"title": "A", "exercises": [fly]}]}

    result = await preview(ui_writer, plan)

    assert "Cable Fly" in json.dumps(result.structured_content)
    assert "   - Cable Fly: 3 × 8 reps @ 220.5 lb" in result.content[0].text


async def test_a_client_without_ui_gets_the_plan_as_text_and_saves_the_same_way(writer, hevy):
    result = await preview(writer)

    assert result.structured_content is None
    text = result.content[0].text
    assert "can't show the preview card" in text
    # Everything save_plan will write: notes, sets, rest times.
    assert "   Notes: Heavy" in text
    assert "   - Bench Press (Barbell): 3 × 8 reps @ 220.5 lb" in text
    assert "rest 3 min" in text
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


async def test_the_text_plan_shows_supersets_and_exercise_notes(writer, hevy):
    curl = {"exercise_template_id": "05293BCA", "superset_id": 0, "notes": "Slow negatives"}
    plan = {
        "title": "Arms",
        "routines": [
            {
                "title": "A",
                "exercises": [
                    curl | {"sets": [{"reps": 10}]},
                    {"exercise_template_id": "D04AC939", "superset_id": 0, "sets": [{"reps": 5}]},
                ],
            }
        ],
    }

    text = (await preview(writer, plan)).content[0].text

    assert "   - Bench Press (Barbell): 10 reps; superset A\n     Notes: Slow negatives" in text
    assert "   - Squat (Barbell): 5 reps; superset A" in text


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
