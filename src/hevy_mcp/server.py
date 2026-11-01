"""The FastMCP app: tool registration, error mapping and the health route."""

import logging
import sys
from collections.abc import AsyncIterator
from importlib.metadata import version
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from hevy_mcp.access import access_middleware, verified_caller
from hevy_mcp.analysis import training_summary
from hevy_mcp.config import Settings
from hevy_mcp.dates import (
    parse_bound,
    parse_day,
    parse_optional_range,
    parse_range,
    to_hevy,
)
from hevy_mcp.edits import measurement_values, routine_body, routine_sets, workout_body
from hevy_mcp.hevy import MAX_PAGE_SIZE, MAX_TEMPLATE_PAGE_SIZE, HevyClient, HevyError
from hevy_mcp.hosts import allowed_hosts_middleware
from hevy_mcp.models import (
    BodyMeasurement,
    BodyMeasurementInput,
    BodyMeasurementPage,
    BodyMeasurementRange,
    EquipmentCategory,
    ExerciseHistory,
    ExerciseTemplate,
    ExerciseTemplatePage,
    ExerciseTemplateSearch,
    ExerciseType,
    MeasurementField,
    MuscleGroup,
    Routine,
    RoutineExerciseInput,
    RoutineFolder,
    RoutineFolderPage,
    RoutinePage,
    TrainingSummary,
    UserInfo,
    Workout,
    WorkoutCount,
    WorkoutEventPage,
    WorkoutExerciseInput,
    WorkoutPage,
    WorkoutRange,
)
from hevy_mcp.secrets import SecretSourceError, resolve_api_key

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Hevy's API can't delete, so a create adds data but never destroys any.
CREATES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
# Updates overwrite values; repeating the same call changes nothing further.
UPDATES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
# How the tool-call log names the caller when Access isn't configured.
UNAUTHENTICATED = "unauthenticated"

Page = Annotated[int, Field(ge=1, description="Page number, starting at 1.")]
PageSize = Annotated[
    int, Field(ge=1, description=f"Items per page; values above {MAX_PAGE_SIZE} are capped.")
]
StartDate = Annotated[
    str,
    Field(
        description="First day or moment to include, ISO 8601 (2024-06-01 or "
        "2024-06-01T18:30:00Z). Dates without an offset use the server's timezone."
    ),
]
OptionalStartDate = Annotated[
    str | None, Field(description="Optional first day or moment to include, ISO 8601.")
]
OptionalEndDate = Annotated[
    str | None, Field(description="Optional last day or moment to include, ISO 8601.")
]
EndDate = Annotated[
    str,
    Field(description="Last day or moment to include, ISO 8601. A date includes that whole day."),
]
Moment = Annotated[
    str,
    Field(
        description="ISO 8601 date-time, e.g. 2024-06-01T18:30:00Z. Without an offset it is "
        "read in the server's timezone."
    ),
]
WorkoutExercises = Annotated[
    list[WorkoutExerciseInput],
    Field(description="The exercises in order, each with its sets in order."),
]
RoutineExercises = Annotated[
    list[RoutineExerciseInput],
    Field(description="The exercises in order, each with its planned sets in order."),
]
Day = Annotated[str, Field(description="The day, as YYYY-MM-DD.")]

INSTRUCTIONS = """\
Read-only access to the user's Hevy workout and body-measurement data.
Weights are in kilograms and distances in metres, as the field names say;
get_user_info returns the units the user prefers to see.
"""

WRITE_INSTRUCTIONS = """\
Access to the user's Hevy workout and body-measurement data, including tools
that create and update it. Weights are in kilograms and distances in metres,
as the field names say; get_user_info returns the units the user prefers to
see, so convert what the user says into kg and metres before writing.
The create_ and update_ tools change the user's real Hevy data. Confirm what
you are about to write with the user first. Hevy's API cannot delete
anything, so a mistaken create can only be removed in the Hevy app.
"""


class HevyErrorMiddleware(Middleware):
    """Turns domain errors into tool errors whose message reaches the client.

    FastMCP masks unexpected exceptions into a generic ToolError, keeping the
    original as the cause; Hevy errors carry a safe, user-facing message, so
    they are re-raised with that message instead.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except HevyError as exc:
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            if isinstance(exc.__cause__, HevyError):
                raise ToolError(str(exc.__cause__)) from exc.__cause__
            raise


class CallerLogMiddleware(Middleware):
    """Logs who called which tool, for an audit trail in ``docker logs``.

    The caller is the email Cloudflare Access verified, or "unauthenticated"
    when Access isn't configured. Nothing else from the request is logged.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        # The client names the tool, so it's quoted: a newline can't fake a log line.
        logger.info("Tool call %r by %s", context.message.name, _caller())
        return await call_next(context)


def _caller() -> str:
    try:
        request = get_http_request()
    except RuntimeError:
        return UNAUTHENTICATED
    return verified_caller(request) or UNAUTHENTICATED


class _State:
    hevy: HevyClient


def create_server(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """Build the server. ``transport`` replaces the network for Hevy and Vault."""
    settings = settings or Settings()
    state = _State()

    @lifespan
    async def connect(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            api_key = await resolve_api_key(settings, transport)
        except SecretSourceError as exc:
            logger.error("Startup failed: %s", exc)
            raise
        async with httpx.AsyncClient(
            base_url=settings.hevy_base_url,
            timeout=settings.hevy_timeout_seconds,
            transport=transport,
        ) as http:
            state.hevy = HevyClient(
                http,
                api_key,
                max_retries=settings.hevy_max_retries,
                backoff_seconds=settings.hevy_retry_backoff_seconds,
                max_pages=settings.hevy_range_max_pages,
                template_cache_seconds=settings.hevy_template_cache_seconds,
            )
            yield {}

    mcp = FastMCP(
        "hevy",
        version=version("hevy-mcp"),
        instructions=WRITE_INSTRUCTIONS if settings.hevy_enable_writes else INSTRUCTIONS,
        lifespan=connect,
        middleware=[CallerLogMiddleware(), HevyErrorMiddleware()],
        mask_error_details=True,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool(annotations=READ_ONLY)
    async def get_user_info() -> UserInfo:
        """Get the Hevy user's profile: name, username, profile URL, Pro status and
        preferred weight_unit and distance_unit. Use the preferred units when
        presenting weights (stored in kg) and distances (stored in metres)."""
        return await state.hevy.user_info()

    @mcp.tool(annotations=READ_ONLY)
    async def list_workouts(page: Page = 1, page_size: PageSize = MAX_PAGE_SIZE) -> WorkoutPage:
        """List workouts newest first, one page at a time, with every exercise and
        set. Returns the page number and page_count, so you can tell whether more
        pages exist. To look at a period, prefer get_workouts_in_range."""
        return await state.hevy.workouts(page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def get_workout(workout_id: str) -> Workout:
        """Get one workout in full: title, start and end time, and each exercise's
        sets with weight_kg, reps, rpe, distance_meters and duration_seconds."""
        return await state.hevy.workout(workout_id)

    @mcp.tool(annotations=READ_ONLY)
    async def get_workout_count() -> WorkoutCount:
        """Get the total number of workouts the user has logged."""
        return await state.hevy.workout_count()

    @mcp.tool(annotations=READ_ONLY)
    async def get_workouts_in_range(start_date: StartDate, end_date: EndDate) -> WorkoutRange:
        """Get every workout that started between two dates, newest first, for
        questions like "what did I do last week?". If truncated is true, the
        search hit its page limit before reaching start_date: narrow the range."""
        period = parse_range(start_date, end_date, settings.zone)
        workouts, truncated = await state.hevy.workouts_in_range(period, settings.zone)
        return WorkoutRange(
            start_date=period.start.isoformat(),
            end_date=period.end.isoformat(),
            workouts=workouts,
            truncated=truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_training_summary(start_date: StartDate, end_date: EndDate) -> TrainingSummary:
        """Summarise training between two dates: workout count, total duration,
        total volume (weight_kg x reps), sets and reps, per-exercise set counts and
        top weight, and sets per primary muscle group. Warm-up sets are excluded."""
        period = parse_range(start_date, end_date, settings.zone)
        workouts, truncated = await state.hevy.workouts_in_range(period, settings.zone)
        templates, templates_truncated = (
            await state.hevy.all_exercise_templates() if workouts else ([], False)
        )
        return training_summary(
            workouts,
            templates,
            period,
            settings.zone,
            truncated=truncated or templates_truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_workout_events(
        since: Annotated[
            str | None,
            Field(description="Only events after this ISO 8601 date or date-time."),
        ] = None,
        page: Page = 1,
        page_size: PageSize = MAX_PAGE_SIZE,
    ) -> WorkoutEventPage:
        """List workout changes since a date: "updated" events carry the workout,
        "deleted" events its id and deleted_at. Useful for "what changed recently?"."""
        moment = parse_bound(since, "since", settings.zone) if since else None
        return await state.hevy.workout_events(moment, page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def list_exercise_templates(
        page: Page = 1,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                description=f"Items per page; values above {MAX_TEMPLATE_PAGE_SIZE} are capped.",
            ),
        ] = MAX_TEMPLATE_PAGE_SIZE,
    ) -> ExerciseTemplatePage:
        """List exercise templates (Hevy's built-in and the user's custom exercises)
        with their primary and secondary muscle groups and equipment. To find one by
        name, use search_exercise_templates instead."""
        return await state.hevy.exercise_templates(page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def get_exercise_template(exercise_template_id: str) -> ExerciseTemplate:
        """Get one exercise template: title, type, primary and secondary muscle
        groups, equipment and whether it is a custom exercise."""
        return await state.hevy.exercise_template(exercise_template_id)

    @mcp.tool(annotations=READ_ONLY)
    async def search_exercise_templates(
        query: Annotated[
            str,
            Field(description='Part of the exercise name, e.g. "bench press". Case-insensitive.'),
        ],
    ) -> ExerciseTemplateSearch:
        """Find exercise templates whose title contains the query, ignoring case.
        Use it to get the exercise_template_id that get_exercise_history needs."""
        needle = query.strip().casefold()
        if not needle:
            raise ToolError("query must not be blank.")
        templates, truncated = await state.hevy.all_exercise_templates()
        return ExerciseTemplateSearch(
            query=query,
            exercise_templates=[t for t in templates if needle in (t.title or "").casefold()],
            truncated=truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_exercise_history(
        exercise_template_id: str,
        start_date: OptionalStartDate = None,
        end_date: OptionalEndDate = None,
    ) -> ExerciseHistory:
        """Get every logged set of one exercise, with weight_kg, reps,
        rpe, set_type and the workout's date, to track progress on a lift. Find the
        exercise_template_id with search_exercise_templates."""
        start, end = parse_optional_range(start_date, end_date, settings.zone)
        return await state.hevy.exercise_history(exercise_template_id, start, end)

    @mcp.tool(annotations=READ_ONLY)
    async def list_body_measurements(
        page: Page = 1, page_size: PageSize = MAX_PAGE_SIZE
    ) -> BodyMeasurementPage:
        """List body measurements one page at a time: weight_kg, lean_mass_kg,
        fat_percent and circumferences in cm. For a period, prefer
        get_body_measurements_in_range."""
        return await state.hevy.body_measurements(page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def get_body_measurement(
        date: Annotated[str, Field(description="The day, as YYYY-MM-DD.")],
    ) -> BodyMeasurement:
        """Get the body measurement logged on one day, e.g. "what did I weigh on
        1 June?"."""
        return await state.hevy.body_measurement(parse_day(date, "date"))

    @mcp.tool(annotations=READ_ONLY)
    async def get_body_measurements_in_range(
        start_date: StartDate, end_date: EndDate
    ) -> BodyMeasurementRange:
        """Get every body measurement between two dates, newest first, for trends
        such as weight change over a period. If truncated is true, the search hit
        its page limit before reaching start_date: narrow the range."""
        period = parse_range(start_date, end_date, settings.zone)
        found, truncated = await state.hevy.body_measurements_in_range(period, settings.zone)
        return BodyMeasurementRange(
            start_date=period.start.isoformat(),
            end_date=period.end.isoformat(),
            body_measurements=found,
            truncated=truncated,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_routines(page: Page = 1, page_size: PageSize = MAX_PAGE_SIZE) -> RoutinePage:
        """List the user's routines (planned workouts) with their exercises and
        planned sets, one page at a time."""
        return await state.hevy.routines(page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def get_routine(routine_id: str) -> Routine:
        """Get one routine: its folder, exercises, rest times and planned sets,
        including rep ranges. Compare with workouts to see plan versus actual."""
        return await state.hevy.routine(routine_id)

    @mcp.tool(annotations=READ_ONLY)
    async def list_routine_folders(
        page: Page = 1, page_size: PageSize = MAX_PAGE_SIZE
    ) -> RoutineFolderPage:
        """List the folders the user groups routines into, one page at a time."""
        return await state.hevy.routine_folders(page, page_size)

    @mcp.tool(annotations=READ_ONLY)
    async def get_routine_folder(folder_id: int) -> RoutineFolder:
        """Get one routine folder by its numeric id (a routine's folder_id)."""
        return await state.hevy.routine_folder(folder_id)

    if settings.hevy_enable_writes:
        _add_write_tools(mcp, state, settings)
    return mcp


def _workout_times(start_time: str, end_time: str, settings: Settings) -> tuple[str, str]:
    start = parse_bound(start_time, "start_time", settings.zone)
    end = parse_bound(end_time, "end_time", settings.zone)
    if end < start:
        raise ToolError(f"end_time ({end_time}) is before start_time ({start_time}).")
    return to_hevy(start), to_hevy(end)


def _dump(items: list[Any]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _dump_routine(exercises: list[RoutineExerciseInput]) -> list[dict[str, Any]]:
    return [exercise | {"sets": routine_sets(exercise["sets"])} for exercise in _dump(exercises)]


def _add_write_tools(mcp: FastMCP, state: _State, settings: Settings) -> None:
    """Register the tools that create and update Hevy data (HEVY_ENABLE_WRITES)."""

    @mcp.tool(annotations=CREATES)
    async def create_workout(
        title: str,
        start_time: Moment,
        end_time: Moment,
        exercises: WorkoutExercises,
        description: str | None = None,
        is_private: bool = False,
    ) -> Workout:
        """Log a completed workout in Hevy, with its exercises and sets. Weights are
        in kg and distances in metres. Find each exercise_template_id with
        search_exercise_templates. Not retried if Hevy's answer is lost, so a
        failure says whether to check for the workout before trying again."""
        start, end = _workout_times(start_time, end_time, settings)
        return await state.hevy.create_workout(
            {
                "title": title,
                "description": description,
                "start_time": start,
                "end_time": end,
                "is_private": is_private,
                "exercises": _dump(exercises),
            }
        )

    @mcp.tool(annotations=UPDATES)
    async def update_workout(
        workout_id: str,
        is_private: Annotated[
            bool,
            Field(
                description="Whether the workout is private. Required on every update: "
                "Hevy doesn't report it, and resets it to public when it's left out."
            ),
        ],
        title: str | None = None,
        description: Annotated[
            str | None, Field(description='New description; "" clears it.')
        ] = None,
        start_time: Moment | None = None,
        end_time: Moment | None = None,
        exercises: Annotated[
            list[WorkoutExerciseInput] | None,
            Field(
                description="Replaces every exercise and set. Leave out to keep them; to "
                "change one set, read the workout and send the full list back changed."
            ),
        ] = None,
    ) -> Workout:
        """Change a logged workout. Only the fields given change; the rest are kept
        as they are in Hevy. Get the current workout with get_workout first."""
        current = workout_body(await state.hevy.workout(workout_id))
        if title is not None:
            current["title"] = title
        if description is not None:
            current["description"] = description
        if exercises is not None:
            current["exercises"] = _dump(exercises)
        if start_time is not None or end_time is not None:
            current["start_time"], current["end_time"] = _workout_times(
                start_time or _stored_time(current["start_time"]),
                end_time or _stored_time(current["end_time"]),
                settings,
            )
        current["is_private"] = is_private
        return await state.hevy.update_workout(workout_id, current)

    @mcp.tool(annotations=CREATES)
    async def create_routine(
        title: str,
        exercises: RoutineExercises,
        folder_id: Annotated[
            int | None,
            Field(description='Routine folder to put it in; leave out for "My Routines".'),
        ] = None,
        notes: str | None = None,
    ) -> Routine:
        """Create a routine (a planned workout) with its exercises, rest times and
        planned sets, including rep ranges. Find exercise_template_ids with
        search_exercise_templates and folder ids with list_routine_folders. Hevy
        limits how many routines a user can have."""
        return await state.hevy.create_routine(
            {
                "title": title,
                "folder_id": folder_id,
                "notes": notes,
                "exercises": _dump_routine(exercises),
            }
        )

    @mcp.tool(annotations=UPDATES)
    async def update_routine(
        routine_id: str,
        notes: Annotated[
            str,
            Field(
                description="The routine's notes. Required on every update: Hevy doesn't "
                "return them, and erases them when they're left out. Ask the user what they "
                'are if you don\'t know; "" for none.'
            ),
        ],
        title: str | None = None,
        folder_id: Annotated[
            int | None, Field(description="Move the routine to this routine folder.")
        ] = None,
        exercises: Annotated[
            list[RoutineExerciseInput] | None,
            Field(
                description="Replaces every exercise and planned set. Leave out to keep "
                "them; to change one, read the routine and send the full list back changed."
            ),
        ] = None,
    ) -> Routine:
        """Change a routine. Only the fields given change; the rest are kept as they
        are in Hevy. Get the current routine with get_routine first."""
        current = routine_body(await state.hevy.routine(routine_id))
        if title is not None:
            current["title"] = title
        if folder_id is not None:
            current["folder_id"] = folder_id
        if exercises is not None:
            current["exercises"] = _dump_routine(exercises)
        current["notes"] = notes
        return await state.hevy.update_routine(routine_id, current)

    @mcp.tool(annotations=CREATES)
    async def create_routine_folder(title: str) -> RoutineFolder:
        """Create a routine folder. Hevy puts it first and moves the others down."""
        return await state.hevy.create_routine_folder(title)

    @mcp.tool(annotations=CREATES)
    async def create_exercise_template(
        title: str,
        exercise_type: Annotated[
            ExerciseType,
            Field(description="What a set records, e.g. weight_reps for weight and reps."),
        ],
        equipment_category: EquipmentCategory,
        muscle_group: Annotated[MuscleGroup, Field(description="The primary muscle group.")],
        other_muscles: Annotated[
            list[MuscleGroup], Field(description="Secondary muscle groups.")
        ] = [],  # noqa: B006  (FastMCP copies defaults; it is never mutated)
    ) -> ExerciseTemplate:
        """Create a custom exercise, for movements Hevy's built-in exercises don't
        cover. Search with search_exercise_templates first so as not to duplicate
        one. Hevy limits how many custom exercises a user can have."""
        exercise = {
            "title": title,
            "exercise_type": exercise_type,
            "equipment_category": equipment_category,
            "muscle_group": muscle_group,
            "other_muscles": other_muscles,
        }
        template_id = await state.hevy.create_exercise_template(exercise)
        return ExerciseTemplate(
            id=template_id,
            title=title,
            type=exercise_type,
            primary_muscle_group=muscle_group,
            secondary_muscle_groups=other_muscles,
            equipment=equipment_category,
            is_custom=True,
        )

    @mcp.tool(annotations=CREATES)
    async def create_body_measurement(
        date: Day, measurements: BodyMeasurementInput
    ) -> BodyMeasurement:
        """Log body measurements for a day: weight_kg, lean_mass_kg, fat_percent and
        circumferences in cm. Hevy keeps one entry per day; if the day already has
        one, use update_body_measurement instead."""
        day = parse_day(date, "date")
        values = measurements.model_dump(exclude_none=True)
        if not values:
            raise ToolError("Give at least one measurement to log.")
        await state.hevy.create_body_measurement(day, values)
        return BodyMeasurement(date=day.isoformat(), **values)

    @mcp.tool(annotations=UPDATES)
    async def update_body_measurement(
        date: Day,
        changes: Annotated[
            BodyMeasurementInput,
            Field(description="New values; fields left out keep their current value."),
        ] = BodyMeasurementInput(),  # noqa: B008  (immutable in practice; never mutated)
        clear: Annotated[
            list[MeasurementField], Field(description="Fields to erase, leaving them empty.")
        ] = [],  # noqa: B006
    ) -> BodyMeasurement:
        """Change the body measurement logged on a day. Only the fields in changes
        and clear change; the rest are kept as they are in Hevy."""
        day = parse_day(date, "date")
        updates = changes.model_dump(exclude_none=True)
        if not updates and not clear:
            raise ToolError("Give at least one change or a field to clear.")
        if both := sorted(updates.keys() & set(clear)):
            raise ToolError(f"Fields can't be both changed and cleared: {', '.join(both)}.")
        values = measurement_values(await state.hevy.body_measurement(day)) | updates
        for field in clear:
            values.pop(field, None)
        await state.hevy.update_body_measurement(day, values)
        return BodyMeasurement(date=day.isoformat(), **values)


def _stored_time(value: str | None) -> str:
    """A workout time as Hevy stored it, for checking against a new one."""
    if not value:
        raise ToolError("The workout has no start or end time in Hevy; give both.")
    return value


def http_options(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Keyword arguments for FastMCP's ``run`` and ``http_app``.

    They add the allowed-hosts check, then the Cloudflare Access check.
    """
    middleware = allowed_hosts_middleware(settings) + access_middleware(settings, transport)
    return {"middleware": middleware}


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        stream=sys.stdout,
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server(settings).run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path="/mcp",
        show_banner=False,
        **http_options(settings),
    )
