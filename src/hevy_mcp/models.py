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
    truncated: bool = Field(
        description="True if the page cap cut the workouts or the exercise templates short."
    )


class ExerciseTemplateSearch(BaseModel):
    query: str
    exercise_templates: list[ExerciseTemplate]
    truncated: bool = Field(
        description="True if the page cap stopped the template listing early, so some "
        "matches may be missing."
    )


class ExerciseHistoryEntry(HevyModel):
    """One set of the exercise, with the workout it belongs to."""

    workout_id: str | None = None
    workout_title: str | None = None
    workout_start_time: str | None = None
    workout_end_time: str | None = None
    exercise_template_id: str | None = None
    weight_kg: Number | None = None
    reps: Number | None = None
    distance_meters: Number | None = None
    duration_seconds: Number | None = None
    rpe: Number | None = None
    custom_metric: Number | None = None
    set_type: str | None = None


class ExerciseHistory(HevyModel):
    exercise_template_id: str | None = None
    exercise_history: list[ExerciseHistoryEntry] = []


class BodyMeasurement(HevyModel):
    date: str | None = None
    weight_kg: Number | None = None
    lean_mass_kg: Number | None = None
    fat_percent: Number | None = None
    neck_cm: Number | None = None
    shoulder_cm: Number | None = None
    chest_cm: Number | None = None
    left_bicep_cm: Number | None = None
    right_bicep_cm: Number | None = None
    left_forearm_cm: Number | None = None
    right_forearm_cm: Number | None = None
    # Hevy leaves the unit off these names; like the others, they are in cm.
    abdomen: Number | None = None
    waist: Number | None = None
    hips: Number | None = None
    left_thigh: Number | None = None
    right_thigh: Number | None = None
    left_calf: Number | None = None
    right_calf: Number | None = None


class BodyMeasurementPage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    body_measurements: list[BodyMeasurement] = []


class BodyMeasurementRange(BaseModel):
    start_date: str
    end_date: str
    body_measurements: list[BodyMeasurement]
    truncated: bool = Field(
        description="True if the page cap stopped the search before reaching start_date."
    )


class RepRange(HevyModel):
    start: Number | None = None
    end: Number | None = None


class RoutineSet(HevyModel):
    index: int | None = None
    type: str | None = None
    weight_kg: Number | None = None
    reps: Number | None = None
    rep_range: RepRange | None = None
    distance_meters: Number | None = None
    duration_seconds: Number | None = None
    custom_metric: Number | None = None


class RoutineExercise(HevyModel):
    index: int | None = None
    title: str | None = None
    rest_seconds: Number | None = None
    notes: str | None = None
    exercise_template_id: str | None = None
    superset_id: int | None = None
    sets: list[RoutineSet] = []


class Routine(HevyModel):
    id: str | None = None
    title: str | None = None
    folder_id: int | None = None
    updated_at: str | None = None
    created_at: str | None = None
    exercises: list[RoutineExercise] = []


class RoutinePage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    routines: list[Routine] = []


class RoutineFolder(HevyModel):
    id: int | None = None
    index: int | None = None
    title: str | None = None
    updated_at: str | None = None
    created_at: str | None = None


class RoutineFolderPage(HevyModel):
    page: int | None = None
    page_count: int | None = None
    routine_folders: list[RoutineFolder] = []
