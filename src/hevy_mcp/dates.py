"""ISO 8601 parsing for tool inputs.

Date-only inputs are whole days in the configured timezone, so a range from
2024-06-01 to 2024-06-30 covers both days in full. Date-times without an
offset are also read in that timezone.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo

from fastmcp.exceptions import ToolError

EXAMPLES = "for example 2024-06-01 or 2024-06-01T18:30:00Z"


@dataclass(frozen=True)
class DateRange:
    start: datetime
    end: datetime

    def __contains__(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment <= self.end


def parse_bound(value: str, name: str, zone: tzinfo, *, end: bool = False) -> datetime:
    """Parse a tool's date input; date-only ends extend to the end of that day."""
    text = value.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        return datetime.combine(day, time.max if end else time.min, tzinfo=zone)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(
            f"{name} must be an ISO 8601 date or date-time ({EXAMPLES}), got {value!r}."
        ) from None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(
            f"{name} must be an ISO 8601 date such as 2024-06-01, got {value!r}."
        ) from None


def parse_optional_range(
    start_date: str | None, end_date: str | None, zone: tzinfo
) -> tuple[datetime | None, datetime | None]:
    """Parse a range whose ends may each be left open."""
    start = parse_bound(start_date, "start_date", zone) if start_date is not None else None
    end = parse_bound(end_date, "end_date", zone, end=True) if end_date is not None else None
    if start and end and end < start:
        raise ToolError(f"end_date ({end_date}) is before start_date ({start_date}).")
    return start, end


def parse_range(start_date: str, end_date: str, zone: tzinfo) -> DateRange:
    start, end = parse_optional_range(start_date, end_date, zone)
    return DateRange(start, end)  # type: ignore[arg-type]  # both given, so both parsed


def parse_timestamp(value: str | None, zone: tzinfo) -> datetime | None:
    """Parse a timestamp or date from a Hevy response, or None if it isn't one."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if isinstance(moment, datetime) and moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment


def to_hevy(moment: datetime) -> str:
    """Format a moment the way Hevy's query parameters expect: UTC with a Z."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
