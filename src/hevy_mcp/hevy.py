"""Async client for Hevy's public API.

This module owns every HTTP concern: the ``api-key`` header, timeouts, retry
with jittered backoff, and mapping status codes to the domain errors below.
"""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import date, datetime, tzinfo
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, SecretStr

from hevy_mcp.dates import DateRange, parse_timestamp, to_hevy
from hevy_mcp.models import (
    BodyMeasurement,
    BodyMeasurementPage,
    ExerciseHistory,
    ExerciseTemplate,
    ExerciseTemplatePage,
    Routine,
    RoutineFolder,
    RoutineFolderPage,
    RoutinePage,
    UserInfo,
    Workout,
    WorkoutCount,
    WorkoutEventPage,
    WorkoutPage,
)

logger = logging.getLogger(__name__)


class HevyError(Exception):
    """Base for errors that tools report to the MCP client as-is."""


class HevyAuthError(HevyError):
    def __init__(self, status: int) -> None:
        super().__init__(
            f"Hevy rejected the API key (HTTP {status}). Hevy's API is only available "
            "with an active Hevy Pro subscription; check the subscription and the key "
            "at https://hevy.com/settings?developer."
        )


class HevyNotFound(HevyError):
    def __init__(self, what: str) -> None:
        super().__init__(f"Not found: {what} does not exist in Hevy.")


class HevyUnavailable(HevyError):
    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(
            f"Hevy is unavailable right now ({reason}, after {attempts} attempt"
            f"{'s' if attempts != 1 else ''}). Try again later."
        )


class HevyRequestError(HevyError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Hevy rejected the request (HTTP {status}): {detail}")


# Hevy's maximum page sizes. Larger requests are capped rather than rejected.
MAX_PAGE_SIZE = 10
MAX_TEMPLATE_PAGE_SIZE = 100


def _is_transient(status: int) -> bool:
    return status == 429 or status >= 500


def _is_last_page(number: int, page_count: int | None, items: int, page_size: int) -> bool:
    """Whether page ``number`` is the last. Without a page_count, a page that
    comes back short (fewer items than the page size) is the last one."""
    if page_count is None:
        return items < page_size
    return number >= page_count


def _segment(value: str) -> str:
    """Escape an id for use as one path segment, so it can't change the path."""
    return quote(value, safe="")


class HevyClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: SecretStr,
        *,
        max_retries: int,
        backoff_seconds: float,
        max_pages: int,
        template_cache_seconds: float,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._max_pages = max_pages
        self._template_cache_seconds = template_cache_seconds
        self._templates: tuple[list[ExerciseTemplate], bool] | None = None
        self._templates_fetched_at = 0.0
        self._templates_lock = asyncio.Lock()

    async def _get(self, path: str, *, what: str, params: dict[str, Any] | None = None) -> Any:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.get(
                    path,
                    params=params,
                    headers={"api-key": self._api_key.get_secret_value()},
                )
            except httpx.TimeoutException:
                reason = "request timed out"
            except httpx.TransportError as exc:
                reason = f"connection failed: {type(exc).__name__}"
            else:
                status = response.status_code
                if response.is_success:
                    return response.json()
                if status in (401, 403):
                    raise HevyAuthError(status)
                if status == 404:
                    raise HevyNotFound(what)
                if not _is_transient(status):
                    raise HevyRequestError(status, _error_detail(response))
                reason = f"HTTP {status}"

            if attempt < attempts:
                # Full jitter: a random delay up to an exponentially growing cap.
                delay = random.uniform(0, self._backoff_seconds * 2 ** (attempt - 1))
                logger.warning(
                    "Hevy GET %s failed (%s); retry %d of %d in %.2fs",
                    path,
                    reason,
                    attempt,
                    self._max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
        raise HevyUnavailable(reason, attempts)

    async def _page[M: BaseModel](
        self, model: type[M], path: str, page: int, page_size: int, limit: int, **params: Any
    ) -> M:
        params |= {"page": page, "pageSize": min(page_size, limit)}
        return model.model_validate(await self._get(path, what=f"page {page}", params=params))

    async def _collect_range[I](
        self,
        model: type[BaseModel],
        path: str,
        items: str,
        period: DateRange,
        when: Callable[[I], datetime | None],
    ) -> tuple[list[I], bool]:
        """Page newest first, keeping items in ``period``, until a page reaches
        past its start. Returns the items and whether the page cap cut it short."""
        found: list[I] = []
        for number in range(1, self._max_pages + 1):
            page = await self._page(model, path, number, MAX_PAGE_SIZE, MAX_PAGE_SIZE)
            batch: list[I] = getattr(page, items)
            moments = [when(item) for item in batch]
            found += [item for item, moment in zip(batch, moments, strict=True) if moment in period]
            known = [m for m in moments if m is not None]
            if (
                not batch
                or _is_last_page(number, page.page_count, len(batch), MAX_PAGE_SIZE)
                or (known and min(known) < period.start)
            ):
                return found, False
        return found, True

    async def user_info(self) -> UserInfo:
        body = await self._get("/v1/user/info", what="the Hevy user")
        return UserInfo.model_validate(body.get("data", body))

    async def workouts(self, page: int, page_size: int) -> WorkoutPage:
        return await self._page(WorkoutPage, "/v1/workouts", page, page_size, MAX_PAGE_SIZE)

    async def workout(self, workout_id: str) -> Workout:
        body = await self._get(
            f"/v1/workouts/{_segment(workout_id)}", what=f"workout {workout_id!r}"
        )
        return Workout.model_validate(body)

    async def workout_count(self) -> WorkoutCount:
        return WorkoutCount.model_validate(
            await self._get("/v1/workouts/count", what="the workout count")
        )

    async def workout_events(
        self, since: datetime | None, page: int, page_size: int
    ) -> WorkoutEventPage:
        params = {"since": to_hevy(since)} if since else {}
        return await self._page(
            WorkoutEventPage, "/v1/workouts/events", page, page_size, MAX_PAGE_SIZE, **params
        )

    async def workouts_in_range(
        self, period: DateRange, zone: tzinfo
    ) -> tuple[list[Workout], bool]:
        return await self._collect_range(
            WorkoutPage,
            "/v1/workouts",
            "workouts",
            period,
            lambda w: parse_timestamp(w.start_time, zone),
        )

    async def exercise_templates(self, page: int, page_size: int) -> ExerciseTemplatePage:
        return await self._page(
            ExerciseTemplatePage,
            "/v1/exercise_templates",
            page,
            page_size,
            MAX_TEMPLATE_PAGE_SIZE,
        )

    async def exercise_template(self, template_id: str) -> ExerciseTemplate:
        body = await self._get(
            f"/v1/exercise_templates/{_segment(template_id)}",
            what=f"exercise template {template_id!r}",
        )
        return ExerciseTemplate.model_validate(body)

    async def exercise_history(
        self, template_id: str, start: datetime | None, end: datetime | None
    ) -> ExerciseHistory:
        params = {}
        if start:
            params["start_date"] = to_hevy(start)
        if end:
            params["end_date"] = to_hevy(end)
        body = await self._get(
            f"/v1/exercise_history/{_segment(template_id)}",
            what=f"exercise template {template_id!r}",
            params=params,
        )
        return ExerciseHistory.model_validate({"exercise_template_id": template_id} | body)

    async def body_measurements(self, page: int, page_size: int) -> BodyMeasurementPage:
        return await self._page(
            BodyMeasurementPage, "/v1/body_measurements", page, page_size, MAX_PAGE_SIZE
        )

    async def body_measurement(self, day: date) -> BodyMeasurement:
        body = await self._get(
            f"/v1/body_measurements/{day.isoformat()}",
            what=f"a body measurement for {day.isoformat()}",
        )
        return BodyMeasurement.model_validate(body)

    async def body_measurements_in_range(
        self, period: DateRange, zone: tzinfo
    ) -> tuple[list[BodyMeasurement], bool]:
        return await self._collect_range(
            BodyMeasurementPage,
            "/v1/body_measurements",
            "body_measurements",
            period,
            lambda m: parse_timestamp(m.date, zone),
        )

    async def routines(self, page: int, page_size: int) -> RoutinePage:
        return await self._page(RoutinePage, "/v1/routines", page, page_size, MAX_PAGE_SIZE)

    async def routine(self, routine_id: str) -> Routine:
        body = await self._get(
            f"/v1/routines/{_segment(routine_id)}", what=f"routine {routine_id!r}"
        )
        return Routine.model_validate(body.get("routine", body))

    async def routine_folders(self, page: int, page_size: int) -> RoutineFolderPage:
        return await self._page(
            RoutineFolderPage, "/v1/routine_folders", page, page_size, MAX_PAGE_SIZE
        )

    async def routine_folder(self, folder_id: int) -> RoutineFolder:
        body = await self._get(
            f"/v1/routine_folders/{folder_id}", what=f"routine folder {folder_id}"
        )
        return RoutineFolder.model_validate(body)

    async def all_exercise_templates(self) -> tuple[list[ExerciseTemplate], bool]:
        """Every exercise template, cached briefly because they rarely change.
        Returns the templates and whether the page cap cut the walk short."""
        async with self._templates_lock:
            age = time.monotonic() - self._templates_fetched_at
            if self._templates is None or age > self._template_cache_seconds:
                self._templates = await self._walk_templates()
                self._templates_fetched_at = time.monotonic()
            return self._templates

    async def _walk_templates(self) -> tuple[list[ExerciseTemplate], bool]:
        templates: list[ExerciseTemplate] = []
        for number in range(1, self._max_pages + 1):
            page = await self._page(
                ExerciseTemplatePage,
                "/v1/exercise_templates",
                number,
                MAX_TEMPLATE_PAGE_SIZE,
                MAX_TEMPLATE_PAGE_SIZE,
            )
            batch = page.exercise_templates
            templates += batch
            if not batch or _is_last_page(
                number, page.page_count, len(batch), MAX_TEMPLATE_PAGE_SIZE
            ):
                return templates, False
        return templates, True


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "no detail"
    detail = body.get("error") if isinstance(body, dict) else None
    return str(detail or body)[:300]
