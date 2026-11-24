"""The plan preview: a card that shows a plan before it is saved to Hevy.

``preview_plan`` checks a plan, remembers it here under a one-time preview id
and shows it to the user as a card with a Save to Hevy button. The button
sends a chat message asking for the plan to be saved; the model then calls
``save_plan`` with the id. (Claude doesn't let a card call the server itself,
see ADR 0004.) ``save_plan`` saves the stored plan, not anything the model
sends, so what is saved is what was shown, and only once. Hevy's API can't
delete, so a plan saved by mistake can only be removed by hand in the Hevy app.

Previews are kept in memory, so a restart forgets them and the model shows the
plan again.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import groupby
from typing import Any

from prefab_ui.actions import SetState
from prefab_ui.actions.mcp import SendMessage
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Badge,
    Button,
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
    Column,
    Muted,
    Row,
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
    Text,
)
from prefab_ui.components.control_flow import Else, If
from prefab_ui.rx import STATE

KG_PER_LB = 0.45359237
METRES_PER_MILE = 1609.344
# How long a preview can be saved, and how many wait at once.
PREVIEW_SECONDS = 3600
MAX_PREVIEWS = 50
# A table cell whose text wraps, top-aligned so a row of several lines reads
# from the top.
WRAP = "whitespace-normal break-words align-top"


@dataclass(frozen=True)
class PendingPlan:
    """A previewed plan: its title and each routine's request body."""

    title: str
    routines: list[dict[str, Any]]


class PendingPlans:
    """Previewed plans waiting for the user to confirm, each saveable once."""

    def __init__(
        self,
        seconds: float = PREVIEW_SECONDS,
        limit: int = MAX_PREVIEWS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._limit = limit
        self._clock = clock
        self._plans: dict[str, tuple[float, PendingPlan]] = {}

    def add(self, plan: PendingPlan) -> str:
        preview_id = secrets.token_urlsafe(16)
        self.put_back(preview_id, plan)
        return preview_id

    def put_back(self, preview_id: str, plan: PendingPlan) -> None:
        """Keep a plan under this id again, for a save that Hevy refused before
        writing anything, so the same preview can still be saved."""
        self._expire()
        while len(self._plans) >= self._limit:
            # Dicts keep insertion order, so this drops the oldest preview.
            del self._plans[next(iter(self._plans))]
        self._plans[preview_id] = (self._clock() + self._seconds, plan)

    def take(self, preview_id: str) -> PendingPlan | None:
        """The plan, removed so it can't be saved twice; None if it's gone."""
        self._expire()
        entry = self._plans.pop(preview_id, None)
        return entry[1] if entry else None

    def _expire(self) -> None:
        now = self._clock()
        for preview_id in [p for p, (expires, _) in self._plans.items() if expires <= now]:
            del self._plans[preview_id]


def _number(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def weight(kg: float, unit: str) -> str:
    """A weight in the user's unit: Hevy says "lbs" or "kg"."""
    if unit == "lbs":
        return f"{_number(kg / KG_PER_LB)} lb"
    return f"{_number(kg)} kg"


def distance(metres: float, unit: str) -> str:
    """A distance in the user's unit: Hevy says "miles" or "kilometers"."""
    if unit == "miles":
        return f"{metres / METRES_PER_MILE:.2f} mi"
    if metres >= 1000:
        return f"{metres / 1000:.2f} km"
    return f"{_number(metres)} m"


def _duration(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    if not minutes:
        return f"{rest}s"
    return f"{minutes}:{rest:02d}" if rest else f"{minutes} min"


def _one_set(s: dict[str, Any], weight_unit: str, distance_unit: str) -> str:
    """One planned set: "8–12 reps @ 135 lb", "5:00, 1.00 mi", "10 reps (warmup)"."""
    if (rep_range := s.get("rep_range")) is not None:
        reps = f"{rep_range['start']}–{rep_range['end']} reps"
    elif s.get("reps") is not None:
        reps = f"{s['reps']} reps"
    else:
        reps = ""
    if s.get("weight_kg") is not None:
        load = weight(s["weight_kg"], weight_unit)
        reps = f"{reps} @ {load}" if reps else load
    extras = []
    if s.get("distance_meters") is not None:
        extras.append(distance(s["distance_meters"], distance_unit))
    if s.get("duration_seconds") is not None:
        extras.append(_duration(s["duration_seconds"]))
    if s.get("custom_metric") is not None:
        extras.append(_number(s["custom_metric"]))
    text = ", ".join(part for part in [reps, *extras] if part) or "no target"
    kind = s.get("type", "normal")
    return text if kind == "normal" else f"{text} ({kind})"


def set_groups(sets: list[dict[str, Any]], weight_unit: str, distance_unit: str) -> list[str]:
    """The planned sets, with repeated sets counted: ["3 × 8 reps @ 60 kg", ...]."""
    described = [_one_set(s, weight_unit, distance_unit) for s in sets]
    runs = [(text, len(list(run))) for text, run in groupby(described)]
    return [text if count == 1 else f"{count} × {text}" for text, count in runs]


def describe_sets(sets: list[dict[str, Any]], weight_unit: str, distance_unit: str) -> str:
    """The planned sets in a line: "5 reps @ 60 kg (warmup); 3 × 5 reps @ 100 kg"."""
    return "; ".join(set_groups(sets, weight_unit, distance_unit))


def _outline(plan: PendingPlan, titles: dict[str, str], units: tuple[str, str]) -> list[str]:
    """The plan as text, with everything the card shows and save_plan saves:
    notes, sets in the user's units, rest times and supersets."""
    lines = [f"Routine folder: {plan.title}"]
    for n, routine in enumerate(plan.routines, 1):
        lines.append(f"{n}. {routine['title']}")
        if routine.get("notes"):
            lines.append(f"   Notes: {routine['notes']}")
        supersets = superset_labels(routine["exercises"])
        for exercise in routine["exercises"]:
            details = [describe_sets(exercise["sets"], *units)]
            if rest := exercise.get("rest_seconds"):
                details.append(f"rest {_duration(rest)}")
            if (superset_id := exercise.get("superset_id")) is not None:
                details.append(f"superset {supersets[superset_id]}")
            title = titles[exercise["exercise_template_id"]]
            lines.append(f"   - {title}: {'; '.join(details)}")
            if exercise.get("notes"):
                lines.append(f"     Notes: {exercise['notes']}")
    return lines


def save_request(plan: PendingPlan, preview_id: str) -> str:
    """The chat message the Save to Hevy button sends."""
    return f"Save the plan {plan.title!r} to Hevy (preview {preview_id})."


def _how_to_save(plan: PendingPlan, preview_id: str) -> str:
    return (
        f"Its preview_id is {preview_id}. Nothing is saved yet. Once the user confirms "
        f"(the button sends {save_request(plan, preview_id)!r}, or they may say so), call "
        "save_plan with that preview_id, which saves exactly this plan, once. Don't call "
        "create_plan for it. To change the plan, call preview_plan again with the change "
        "and use the new preview_id."
    )


def plan_summary(
    plan: PendingPlan, titles: dict[str, str], preview_id: str, units: tuple[str, str]
) -> str:
    """What the model is told when the card is shown, since it can't see it."""
    return "\n".join(
        [
            f"The user should now see the plan {plan.title!r} as a card with a Save to "
            "Hevy button, showing every exercise, set and rest time. Don't repeat the plan "
            "in your reply; say it's ready to check and how to save or change it. If the "
            "user says the card didn't appear, show them the plan below (already in their "
            f"units) instead. {_how_to_save(plan, preview_id)}",
            "",
            *_outline(plan, titles, units),
        ]
    )


def text_only_summary(
    plan: PendingPlan, titles: dict[str, str], preview_id: str, units: tuple[str, str]
) -> str:
    """What the model is told when the client can't show the card."""
    return "\n".join(
        [
            "This client can't show the preview card, so show the user the plan below "
            f"(already in their units). {_how_to_save(plan, preview_id)}",
            "",
            *_outline(plan, titles, units),
        ]
    )


def superset_labels(exercises: list[dict[str, Any]]) -> dict[int, str]:
    """Letters for a routine's supersets, in order of appearance: Hevy numbers
    them from 0, which reads oddly as "Superset 0"."""
    ids = dict.fromkeys(e["superset_id"] for e in exercises if e.get("superset_id") is not None)
    return {superset_id: chr(ord("A") + n % 26) for n, superset_id in enumerate(ids)}


def _routine_card(
    routine: dict[str, Any], titles: dict[str, str], weight_unit: str, distance_unit: str
) -> None:
    supersets = superset_labels(routine["exercises"])
    with Card():
        with CardHeader():
            CardTitle(routine["title"])
            if routine.get("notes"):
                CardDescription(routine["notes"])
        with CardContent(), Table():
            with TableHeader(), TableRow():
                TableHead("Exercise")
                TableHead("Sets")
                TableHead("Rest")
            with TableBody():
                for exercise in routine["exercises"]:
                    with TableRow():
                        # Table cells don't wrap by default, so long sets or notes
                        # would widen the table and scroll Rest out of the card.
                        # The notes go under the name: a row of their own would
                        # squeeze them into the first of three columns.
                        with TableCell(css_class=WRAP), Column(gap=1):
                            with Row(gap=2, align="center"):
                                Text(titles[exercise["exercise_template_id"]], bold=True)
                                if exercise.get("superset_id") is not None:
                                    label = supersets[exercise["superset_id"]]
                                    Badge(f"Superset {label}", variant="outline")
                            if exercise.get("notes"):
                                Muted(exercise["notes"])
                        # One line per group of sets.
                        with TableCell(css_class=WRAP), Column(gap=0):
                            for group in set_groups(exercise["sets"], weight_unit, distance_unit):
                                Text(group)
                        rest = exercise.get("rest_seconds")
                        TableCell(_duration(rest) if rest else "—", css_class="align-top")


def plan_view(
    plan: PendingPlan,
    titles: dict[str, str],
    weight_unit: str,
    distance_unit: str,
    preview_id: str,
) -> PrefabApp:
    """The preview card. Its button asks, in the chat, for the plan to be saved."""
    count = len(plan.routines)
    with Column(gap=4, css_class="p-4") as view:
        with Card(), CardHeader():
            CardTitle(plan.title)
            CardDescription(
                f"A new routine folder with {count} routine{'s' if count != 1 else ''}. "
                f"Weights in {'lb' if weight_unit == 'lbs' else 'kg'}."
            )
        for routine in plan.routines:
            _routine_card(routine, titles, weight_unit, distance_unit)
        with If(STATE.asked):
            Muted(
                "The save request is in the chat. If it's waiting in your message box, "
                "send it to save the plan."
            )
        with Else(), Column(gap=2):
            Button(
                "Save to Hevy",
                on_click=[SendMessage(save_request(plan, preview_id)), SetState("asked", True)],
            )
            Muted(
                "Hevy's API can't delete, so check the plan first. To change something, "
                "say so in the chat and a new preview will replace this one."
            )
    return PrefabApp(view=view, state={"asked": False})
