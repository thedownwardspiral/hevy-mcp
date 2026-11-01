"""Request bodies for updates, built from the record as Hevy returns it.

Hevy's PUT endpoints replace the whole record, so the update tools read the
current record, turn it into a request body here, apply only the changes the
caller asked for, and send the result. A field the caller didn't mention keeps
its value instead of being wiped.

Two fields can't be carried over because Hevy never returns them: a workout's
``is_private`` and a routine's ``notes``, and Hevy resets both when they're
left out. The update tools make the caller give them.

Hevy returns nulls it won't take back: a routine set's ``rep_range`` and any
empty body-measurement field are refused as null, so both are left out instead.
"""

from typing import Any

from hevy_mcp.models import (
    BodyMeasurement,
    BodyMeasurementInput,
    Routine,
    RoutineExerciseInput,
    RoutineSetInput,
    Workout,
    WorkoutExerciseInput,
    WorkoutSetInput,
)

# The fields each request body takes, so a value Hevy returns but won't accept
# back (an exercise's title and index, say) isn't sent.
_WORKOUT_EXERCISE_KEYS = WorkoutExerciseInput.model_fields.keys() - {"sets"}
_WORKOUT_SET_KEYS = WorkoutSetInput.model_fields.keys()
_ROUTINE_EXERCISE_KEYS = RoutineExerciseInput.model_fields.keys() - {"sets"}
_ROUTINE_SET_KEYS = RoutineSetInput.model_fields.keys()
MEASUREMENT_KEYS = tuple(BodyMeasurementInput.model_fields)


def _pick(record: Any, keys: Any) -> dict[str, Any]:
    dumped = record.model_dump()
    return {key: dumped.get(key) for key in keys}


def routine_sets(sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Routine sets without a null rep_range, which Hevy's routine PUT refuses."""
    return [
        {key: value for key, value in s.items() if key != "rep_range" or value is not None}
        for s in sets
    ]


def workout_body(workout: Workout) -> dict[str, Any]:
    return {
        "title": workout.title,
        "description": workout.description,
        "start_time": workout.start_time,
        "end_time": workout.end_time,
        "exercises": [
            _pick(exercise, _WORKOUT_EXERCISE_KEYS)
            | {"sets": [_pick(s, _WORKOUT_SET_KEYS) for s in exercise.sets]}
            for exercise in workout.exercises
        ],
    }


def routine_body(routine: Routine) -> dict[str, Any]:
    return {
        "title": routine.title,
        "folder_id": routine.folder_id,
        "exercises": [
            _pick(exercise, _ROUTINE_EXERCISE_KEYS)
            | {"sets": routine_sets([_pick(s, _ROUTINE_SET_KEYS) for s in exercise.sets])}
            for exercise in routine.exercises
        ],
    }


def measurement_values(measurement: BodyMeasurement) -> dict[str, Any]:
    """The measurement's fields that have a value."""
    values = _pick(measurement, MEASUREMENT_KEYS)
    return {key: value for key, value in values.items() if value is not None}
