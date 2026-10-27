"""Pydantic models for Hevy response shapes.

Hevy warns that its API may change, so every model keeps unknown fields and
treats almost every field as optional. Field names keep Hevy's unit suffixes
(``weight_kg``, ``distance_meters``) so that units are never ambiguous.
"""

from pydantic import BaseModel, ConfigDict, Field


class HevyModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class UserInfo(HevyModel):
    id: str | None = None
    username: str | None = None
    name: str | None = None
    url: str | None = None
    weight_unit: str | None = None
    distance_unit: str | None = None
    is_pro: bool | None = None


type Number = int | float


class WorkoutSet(HevyModel):
    index: int | None = None
    type: str | None = None
    weight_kg: Number | None = None
    reps: Number | None = None
    distance_meters: Number | None = None
    duration_seconds: Number | None = None
    rpe: Number | None = None
    custom_metric: Number | None = None


class WorkoutExercise(HevyModel):
    index: int | None = None
    title: str | None = None
    notes: str | None = None
    exercise_template_id: str | None = None
    superset_id: int | None = None
    sets: list[WorkoutSet] = []


class Workout(HevyModel):
    id: str | None = None
    title: str | None = None
    routine_id: str | None = None
    description: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    updated_at: str | None = None
    created_at: str | None = None
    exercises: list[WorkoutExercise] = []


class WorkoutPage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    workouts: list[Workout] = []


class WorkoutCount(HevyModel):
    workout_count: int | None = None


class WorkoutEvent(HevyModel):
    """An ``updated`` event carries the workout; a ``deleted`` one its id and time."""

    type: str | None = None
    workout: Workout | None = None
    id: str | None = None
    deleted_at: str | None = None


class WorkoutEventPage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    events: list[WorkoutEvent] = []


class ExerciseTemplate(HevyModel):
    id: str | None = None
    title: str | None = None
    type: str | None = None
    primary_muscle_group: str | None = None
    secondary_muscle_groups: list[str] = []
    equipment: str | None = None
    is_custom: bool | None = None


class ExerciseTemplatePage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    exercise_templates: list[ExerciseTemplate] = []


class WorkoutRange(BaseModel):
    start_date: str
    end_date: str
    workouts: list[Workout]
    truncated: bool = Field(
        description="True if the page cap stopped the search before reaching start_date."
    )


class ExerciseSummary(BaseModel):
    exercise_template_id: str | None
    title: str | None
    sets: int
    reps: Number
    volume_kg: Number
    top_weight_kg: Number | None


class MuscleGroupSummary(BaseModel):
    muscle_group: str
    sets: int


class TrainingSummary(BaseModel):
    start_date: str
    end_date: str
    workout_count: int
    total_duration_seconds: Number
    total_volume_kg: Number = Field(description="Sum of weight_kg x reps over working sets.")
    total_sets: int = Field(description="Working sets; warm-ups are excluded throughout.")
    total_reps: Number
    exercises: list[ExerciseSummary]
    muscle_groups: list[MuscleGroupSummary] = Field(
        description="Working sets per primary muscle group of each exercise template."
    )
    truncated: bool
