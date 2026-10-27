"""The FastMCP app: tool registration, error mapping and the health route."""

import logging
import sys
from collections.abc import AsyncIterator
from importlib.metadata import version
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from hevy_mcp.analysis import training_summary
from hevy_mcp.config import Settings
from hevy_mcp.dates import parse_bound, parse_day, parse_optional_range, parse_range
from hevy_mcp.hevy import MAX_PAGE_SIZE, MAX_TEMPLATE_PAGE_SIZE, HevyClient, HevyError
from hevy_mcp.models import (
    BodyMeasurement,
    BodyMeasurementPage,
    BodyMeasurementRange,
    ExerciseHistory,
    ExerciseTemplate,
    ExerciseTemplatePage,
    ExerciseTemplateSearch,
    Routine,
    RoutineFolder,
    RoutineFolderPage,
    RoutinePage,
    TrainingSummary,
    UserInfo,
    Workout,
    WorkoutCount,
    WorkoutEventPage,
    WorkoutPage,
    WorkoutRange,
)
from hevy_mcp.secrets import SecretSourceError, resolve_api_key

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

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

INSTRUCTIONS = """\
Read-only access to the user's Hevy workout and body-measurement data.
Weights are in kilograms and distances in metres, as the field names say;
get_user_info returns the units the user prefers to see.
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
        instructions=INSTRUCTIONS,
        lifespan=connect,
        middleware=[HevyErrorMiddleware()],
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
        templates = await state.hevy.all_exercise_templates() if workouts else []
        return training_summary(workouts, templates, period, settings.zone, truncated=truncated)

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
        templates = await state.hevy.all_exercise_templates()
        return ExerciseTemplateSearch(
            query=query,
            exercise_templates=[t for t in templates if needle in (t.title or "").casefold()],
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

    return mcp


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
    )
