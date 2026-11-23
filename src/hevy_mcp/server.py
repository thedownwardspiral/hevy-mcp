"""The FastMCP app: tool registration, error mapping and the health route."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from importlib.metadata import version
from typing import Annotated, Any, Literal

import httpx
from fastmcp import Context, FastMCP
from fastmcp.apps import UI_EXTENSION_ID
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
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
from hevy_mcp.hevy import (
    MAX_PAGE_SIZE,
    MAX_TEMPLATE_PAGE_SIZE,
    HevyAuthError,
    HevyClient,
    HevyConflict,
    HevyError,
    HevyLimitReached,
    HevyNotFound,
    HevyRequestError,
    HevyWriteUncertain,
)
from hevy_mcp.hosts import allowed_hosts_middleware
from hevy_mcp.journal import Journal, JournalError
from hevy_mcp.models import (
    BodyMeasurement,
    BodyMeasurementInput,
    BodyMeasurementPage,
    BodyMeasurementRange,
    EquipmentCategory,
    ExerciseHistory,
    ExerciseTemplate,
    ExerciseTemplateMatches,
    ExerciseTemplatePage,
    ExerciseTemplateSearch,
    ExerciseType,
    MeasurementField,
    MuscleGroup,
    Plan,
    PlanRoutineInput,
    Routine,
    RoutineExerciseInput,
    RoutineExercises,
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
from hevy_mcp.plan_preview import (
    PendingPlan,
    PendingPlans,
    plan_summary,
    plan_view,
    text_only_summary,
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
# Enough for every exercise of a long plan. Search is local, so these two only
# bound the size of one answer: at most 50 x 20 templates.
MAX_SEARCH_QUERIES = 50
MAX_MATCHES_PER_NAME = 20
# update_routine's folder_id for Hevy's default folder, which has no id.
MY_ROUTINES = "my_routines"
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
To create a plan, show it with preview_plan first, and save it with save_plan
once the user confirms (its Save to Hevy button sends a message asking for it).
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
    journal: Journal


def create_server(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """Build the server. ``transport`` replaces the network for Hevy and Vault."""
    settings = settings or Settings()
    state = _State()
    state.journal = Journal(settings.hevy_journal_path)

    @lifespan
    async def connect(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            api_key = await resolve_api_key(settings, transport)
        except SecretSourceError as exc:
            logger.error("Startup failed: %s", exc)
            raise
        if settings.hevy_enable_writes and settings.hevy_journal_path:
            try:
                state.journal.check()
            except JournalError as exc:
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
            str | list[str],
            Field(
                description='Part of an exercise name, e.g. "bench press", or a list of '
                "them to look up several exercises at once. Case-insensitive."
            ),
        ],
    ) -> ExerciseTemplateSearch:
        """Find exercise templates whose title contains the query, ignoring case.
        Pass a list to look up every exercise of a routine or workout in one call;
        the answer is then under searches, with at most 20 matches per name. Use it
        to get the exercise_template_id that other tools need."""
        queries = [query] if isinstance(query, str) else query
        if not queries:
            raise ToolError("Give at least one name to search for.")
        if len(queries) > MAX_SEARCH_QUERIES:
            raise ToolError(f"Search for at most {MAX_SEARCH_QUERIES} names at once.")
        needles = [q.strip().casefold() for q in queries]
        if not all(needles):
            raise ToolError("Search names must not be blank.")
        templates, truncated = await state.hevy.all_exercise_templates()
        matches = [[t for t in templates if n in (t.title or "").casefold()] for n in needles]
        if isinstance(query, str):
            # One name keeps the answer's original shape, so older callers still work.
            return ExerciseTemplateSearch(
                query=query, exercise_templates=matches[0], truncated=truncated
            )
        return ExerciseTemplateSearch(
            searches=[
                ExerciseTemplateMatches(
                    query=q,
                    match_count=len(found),
                    exercise_templates=found[:MAX_MATCHES_PER_NAME],
                )
                for q, found in zip(queries, matches, strict=True)
            ],
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
        _add_plan_preview(mcp, state)
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


def _unfinished_plan(
    folder: RoutineFolder, created: list[Routine], failed: str, total: int, error: Exception
) -> str:
    """Explain a create_plan that stopped partway, so the client can finish it.

    Routines are named by position as well as title, since titles may repeat.
    """
    position = len(created) + 1
    at = f"routine {position} of {total}, {failed!r}"
    done = ", ".join(f"{n} {r.title!r} (id {r.id})" for n, r in enumerate(created, 1)) or "none"
    if isinstance(error, _REFUSALS) and not error.maybe_applied:
        stopped = f"The plan stopped at {at}: {error}"
        check = ""
    else:
        # No clear answer, so Hevy may have saved the routine.
        reason = str(error) if isinstance(error, HevyError) else type(error).__name__
        stopped = f"The plan stopped at {at}, which may or may not have been created ({reason})."
        check = (
            f" Check with list_routines whether routine {position} ({failed!r}) is in folder "
            f"{folder.id} before adding it, or it may be created twice."
        )
    if position == total:
        rest = ""
    elif position + 1 == total:
        rest = f" Routine {total} was not tried."
    else:
        rest = f" Routines {position + 1} to {total} were not tried."
    return (
        f"{stopped} Already created: routine folder {folder.title!r} (folder_id "
        f"{folder.id}) and routines: {done}.{rest}{check} Hevy's API can't delete, so add "
        f"the missing routines with create_routine and folder_id {folder.id} instead of "
        "creating the plan again."
    )


# Hevy answered with a refusal, so that request changed nothing.
_REFUSALS = (HevyAuthError, HevyConflict, HevyLimitReached, HevyNotFound, HevyRequestError)


def _plan_bodies(routines: list[PlanRoutineInput]) -> list[dict[str, Any]]:
    """Each routine's request body, built before any write so bad input creates nothing."""
    return [
        {
            "title": routine.title,
            "notes": routine.notes,
            "exercises": _dump_routine(routine.exercises),
        }
        for routine in routines
    ]


async def _save_plan(state: _State, tool: str, title: str, bodies: list[dict[str, Any]]) -> Plan:
    """Create a routine folder and then each routine in it, journaling every write."""
    folder = await _journaled(
        state.journal,
        tool,
        "routine_folder",
        {"title": title},
        lambda: state.hevy.create_routine_folder(title),
        created_id=lambda created: created.id,
    )
    if folder.id is None:
        # Without an id the routines would land in "My Routines" instead.
        raise ToolError(
            f"Hevy created routine folder {title!r} but didn't return its id, so no "
            "routines were created. Find the folder with list_routine_folders and add "
            "the routines with create_routine and its folder_id, instead of creating "
            "the plan again."
        )
    created: list[Routine] = []
    for body in bodies:
        routine = body | {"folder_id": folder.id}
        try:
            created.append(
                await _journaled(
                    state.journal,
                    tool,
                    "routine",
                    routine,
                    # partial binds this routine now; a lambda would close over
                    # the loop variable (ruff B023).
                    partial(state.hevy.create_routine, routine),
                    created_id=lambda new: new.id,
                )
            )
        except Exception as exc:
            # Anything that goes wrong here leaves a folder behind, so the
            # client must hear what exists rather than a generic error.
            raise ToolError(
                _unfinished_plan(folder, created, body["title"], len(bodies), exc)
            ) from None
    return Plan(folder=folder, routines=created)


async def _journaled[R](
    journal: Journal,
    tool: str,
    kind: str,
    request: Any,
    write: Callable[[], Awaitable[R]],
    *,
    record_id: str | int | None = None,
    before: BaseModel | None = None,
    created_id: Callable[[R], Any] = lambda _: None,
) -> R:
    """Run one write to Hevy with a journal entry before it and one after.

    ``before`` is the record an update replaces, as Hevy returned it. A create
    has no id until Hevy answers, so ``created_id`` reads it from the result.
    """
    op = journal.begin(
        caller=_caller(),
        tool=tool,
        kind=kind,
        id=record_id,
        before=before.model_dump(mode="json") if before else None,
        request=request,
    )
    try:
        result = await write()
    except _REFUSALS as exc:
        # A refusal of a retry says nothing about the try before it, which
        # Hevy may have saved without answering.
        journal.end(op, "uncertain" if exc.maybe_applied else "rejected", error=str(exc))
        raise
    except HevyError as exc:
        journal.end(op, "uncertain", error=str(exc))
        raise
    except BaseException as exc:
        # Anything else may have struck after Hevy saved the write.
        journal.end(op, "uncertain", error=type(exc).__name__)
        raise
    journal.end(op, "ok", id=record_id if record_id is not None else created_id(result))
    return result


def _add_write_tools(mcp: FastMCP, state: _State, settings: Settings) -> None:
    """Register the tools that create and update Hevy data (HEVY_ENABLE_WRITES)."""

    async def journaled[R](
        tool: str, kind: str, request: Any, write: Callable[[], Awaitable[R]], **entry: Any
    ) -> R:
        return await _journaled(state.journal, tool, kind, request, write, **entry)

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
        workout = {
            "title": title,
            "description": description,
            "start_time": start,
            "end_time": end,
            "is_private": is_private,
            "exercises": _dump(exercises),
        }
        return await journaled(
            "create_workout",
            "workout",
            workout,
            lambda: state.hevy.create_workout(workout),
            created_id=lambda created: created.id,
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
        before = await state.hevy.workout(workout_id)
        current = workout_body(before)
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
        return await journaled(
            "update_workout",
            "workout",
            current,
            lambda: state.hevy.update_workout(workout_id, current),
            record_id=workout_id,
            before=before,
        )

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
        routine = {
            "title": title,
            "folder_id": folder_id,
            "notes": notes,
            "exercises": _dump_routine(exercises),
        }
        return await journaled(
            "create_routine",
            "routine",
            routine,
            lambda: state.hevy.create_routine(routine),
            created_id=lambda created: created.id,
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
            int | Literal["my_routines"] | None,
            Field(
                description='Move the routine to this routine folder, or "my_routines" to '
                'take it out of its folder and back to Hevy\'s default "My Routines".'
            ),
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
        before = await state.hevy.routine(routine_id)
        current = routine_body(before)
        if title is not None:
            current["title"] = title
        if folder_id == MY_ROUTINES:
            current["folder_id"] = None
        elif folder_id is not None:
            current["folder_id"] = folder_id
        if exercises is not None:
            current["exercises"] = _dump_routine(exercises)
        current["notes"] = notes
        return await journaled(
            "update_routine",
            "routine",
            current,
            lambda: state.hevy.update_routine(routine_id, current),
            record_id=routine_id,
            before=before,
        )

    @mcp.tool(annotations=CREATES)
    async def create_routine_folder(title: str) -> RoutineFolder:
        """Create a routine folder. Hevy puts it first and moves the others down."""
        return await journaled(
            "create_routine_folder",
            "routine_folder",
            {"title": title},
            lambda: state.hevy.create_routine_folder(title),
            created_id=lambda created: created.id,
        )

    @mcp.tool(annotations=CREATES)
    async def create_plan(
        title: Annotated[
            str, Field(description="The plan's name, which becomes its routine folder's title.")
        ],
        routines: Annotated[
            list[PlanRoutineInput],
            Field(min_length=1, description="The plan's routines, in order."),
        ],
    ) -> Plan:
        """Create a plan: a new routine folder holding several routines, such as the
        days of a split, in one call. Find every exercise_template_id first with one
        search_exercise_templates call given a list of names. If Hevy refuses a
        routine, for example at its routine limit, this stops and says what was
        already created; Hevy's API can't delete, so finish the plan from there
        rather than starting it again."""
        return await _save_plan(state, "create_plan", title, _plan_bodies(routines))

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
        template_id = await journaled(
            "create_exercise_template",
            "exercise_template",
            exercise,
            lambda: state.hevy.create_exercise_template(exercise),
            created_id=lambda created: created,
        )
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
        await journaled(
            "create_body_measurement",
            "body_measurement",
            values,
            lambda: state.hevy.create_body_measurement(day, values),
            record_id=day.isoformat(),
        )
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
        before = await state.hevy.body_measurement(day)
        values = measurement_values(before) | updates
        for field in clear:
            values.pop(field, None)
        await journaled(
            "update_body_measurement",
            "body_measurement",
            values,
            lambda: state.hevy.update_body_measurement(day, values),
            record_id=day.isoformat(),
            before=before,
        )
        return BodyMeasurement(date=day.isoformat(), **values)


def _add_plan_preview(mcp: FastMCP, state: _State) -> None:
    """Register preview_plan, which shows a plan as a card, and save_plan, which
    saves a previewed plan once the user confirms. See plan_preview.py."""
    pending = PendingPlans()

    @mcp.tool(annotations=CREATES)
    async def save_plan(
        preview_id: Annotated[str, Field(description="The id preview_plan returned.")],
    ) -> Plan:
        """Save a plan shown with preview_plan, once the user has confirmed it: the
        card's Save to Hevy button sends a message asking for it. It saves exactly
        the plan that was previewed, and only once."""
        plan = pending.take(preview_id)
        if plan is None:
            raise ToolError(
                "That preview was already saved or has expired, so nothing was saved this "
                "time. Check list_routine_folders before showing the plan again."
            )
        try:
            return await _save_plan(state, "save_plan", plan.title, plan.routines)
        except HevyError as exc:
            # Only the folder's create gets here: _save_plan turns a routine's
            # failure into a ToolError. If Hevy can't have saved the folder,
            # nothing exists yet, so the same preview may be saved again.
            if not isinstance(exc, HevyWriteUncertain) and not exc.maybe_applied:
                pending.put_back(preview_id, plan)
            raise

    @mcp.tool(app=True, annotations=READ_ONLY)
    async def preview_plan(
        title: Annotated[
            str, Field(description="The plan's name, which becomes its routine folder's title.")
        ],
        routines: Annotated[
            list[PlanRoutineInput],
            Field(min_length=1, description="The plan's routines, in order."),
        ],
        ctx: Context,
    ) -> ToolResult:
        """Show the user a plan (a new routine folder holding several routines, such
        as the days of a split) as a card with a Save to Hevy button, before
        anything is saved. It takes the same arguments as create_plan and returns a
        preview_id; once the user confirms, save it with save_plan. To change the
        plan, call this again with the change."""
        bodies = _plan_bodies(routines)
        wanted = {e["exercise_template_id"] for body in bodies for e in body["exercises"]}
        titles, user = await asyncio.gather(_template_titles(state, wanted), state.hevy.user_info())
        units = (user.weight_unit or "kg", user.distance_unit or "kilometers")
        plan = PendingPlan(title=title, routines=bodies)
        preview_id = pending.add(plan)
        if not ctx.client_supports_extension(UI_EXTENSION_ID):
            return ToolResult(content=text_only_summary(plan, titles, preview_id, units))
        view = plan_view(plan, titles, *units, preview_id=preview_id)
        summary = plan_summary(plan, titles, preview_id, units)
        return ToolResult(content=summary, structured_content=view)


async def _template_titles(state: _State, wanted: set[str]) -> dict[str, str]:
    """Each wanted exercise template's title, refusing an id that isn't a template.

    The cached list may have been cut short by the page cap, or be older than a
    template made in the Hevy app, so an id it lacks is looked up on its own
    before it is refused. Saving a plan with a bad id would leave its folder
    behind, which the API can't delete.
    """
    templates, _ = await state.hevy.all_exercise_templates()
    titles = {t.id: t.title or t.id for t in templates if t.id in wanted}
    unknown = sorted(wanted - titles.keys())

    async def lookup(template_id: str) -> ExerciseTemplate | None:
        try:
            return await state.hevy.exercise_template(template_id)
        except HevyNotFound:
            return None

    looked_up = dict(zip(unknown, await asyncio.gather(*map(lookup, unknown)), strict=True))
    if missing := [template_id for template_id, t in looked_up.items() if t is None]:
        raise ToolError(
            f"No exercise template has the id {', '.join(missing)}. Find each "
            "exercise_template_id with search_exercise_templates."
        )
    return titles | {
        template_id: t.title or template_id for template_id, t in looked_up.items() if t
    }


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
