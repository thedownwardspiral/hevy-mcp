"""Derived statistics over workouts. Pure functions, no I/O."""

from collections import Counter
from collections.abc import Iterable
from datetime import tzinfo

from hevy_mcp.dates import DateRange, parse_timestamp
from hevy_mcp.models import (
    ExerciseSummary,
    ExerciseTemplate,
    MuscleGroupSummary,
    TrainingSummary,
    Workout,
    WorkoutSet,
)

WARMUP = "warmup"
UNKNOWN_MUSCLE_GROUP = "unknown"


def working_sets(sets: Iterable[WorkoutSet]) -> list[WorkoutSet]:
    return [s for s in sets if s.type != WARMUP]


def set_volume(s: WorkoutSet) -> float:
    return (s.weight_kg or 0) * (s.reps or 0)


def training_summary(
    workouts: list[Workout],
    templates: Iterable[ExerciseTemplate],
    period: DateRange,
    zone: tzinfo,
    *,
    truncated: bool,
) -> TrainingSummary:
    muscle_by_template = {t.id: t.primary_muscle_group for t in templates if t.id}
    exercises: dict[str | None, ExerciseSummary] = {}
    muscle_sets: Counter[str] = Counter()
    duration = 0.0

    for workout in workouts:
        start = parse_timestamp(workout.start_time, zone)
        end = parse_timestamp(workout.end_time, zone)
        if start and end and end > start:
            duration += (end - start).total_seconds()

        for exercise in workout.exercises:
            sets = working_sets(exercise.sets)
            if not sets:
                continue
            key = exercise.exercise_template_id
            summary = exercises.setdefault(
                key,
                ExerciseSummary(
                    exercise_template_id=key,
                    title=exercise.title,
                    sets=0,
                    reps=0,
                    volume_kg=0,
                    top_weight_kg=None,
                ),
            )
            summary.sets += len(sets)
            summary.reps += sum(s.reps or 0 for s in sets)
            summary.volume_kg += sum(set_volume(s) for s in sets)
            weights = [s.weight_kg for s in sets if s.weight_kg is not None]
            if weights:
                summary.top_weight_kg = max([*weights, summary.top_weight_kg or 0])
            muscle = muscle_by_template.get(key) or UNKNOWN_MUSCLE_GROUP
            muscle_sets[muscle] += len(sets)

    ranked = sorted(exercises.values(), key=lambda e: (-e.sets, e.title or ""))
    for e in ranked:
        e.volume_kg = round(e.volume_kg, 2)
    return TrainingSummary(
        start_date=period.start.isoformat(),
        end_date=period.end.isoformat(),
        workout_count=len(workouts),
        total_duration_seconds=round(duration),
        total_volume_kg=round(sum(e.volume_kg for e in ranked), 2),
        total_sets=sum(e.sets for e in ranked),
        total_reps=sum(e.reps for e in ranked),
        exercises=ranked,
        muscle_groups=[
            MuscleGroupSummary(muscle_group=m, sets=n)
            for m, n in sorted(muscle_sets.items(), key=lambda item: (-item[1], item[0]))
        ],
        truncated=truncated,
    )
