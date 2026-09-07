from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .governed_domain_core import GovernedDomainConflict
from .personal_routine_contracts import RoutineCadence, RoutineDefinition


SOURCE_PERMISSIONS = {
    "WORK_ITEM": "APP.WORK:VIEW",
    "MAIL": "APP.MAIL:VIEW",
    "CALENDAR": "APP.CALENDAR:VIEW",
}


def permissions_for_sources(definition: RoutineDefinition) -> tuple[str, ...]:
    return tuple(SOURCE_PERMISSIONS[source.value] for source in definition.sources)


def preview_next_run(
    definition: RoutineDefinition,
    *,
    after: datetime,
) -> datetime:
    try:
        zone = ZoneInfo(definition.time_zone)
    except ZoneInfoNotFoundError as error:
        raise GovernedDomainConflict("The routine time zone is unavailable.") from error
    reference = after.astimezone(UTC)
    hour, minute = (int(part) for part in definition.local_time.split(":"))
    local_reference = reference.astimezone(zone)
    first_day = max(
        local_reference.date(),
        definition.active_from or local_reference.date(),
    )
    for days in range(0, 371):
        day = first_day + timedelta(days=days)
        if definition.active_until and day > definition.active_until:
            break
        if not _matches_cadence(definition, day):
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=zone)
        candidate = _after_quiet_hours(candidate, definition, zone)
        if definition.active_until and candidate.date() > definition.active_until:
            continue
        if not _valid_local(candidate, zone) or candidate.astimezone(UTC) <= reference:
            continue
        return candidate.astimezone(UTC)
    raise GovernedDomainConflict("The routine preview could not resolve its next run time.")


def _valid_local(candidate: datetime, zone: ZoneInfo) -> bool:
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    return (
        round_trip.date() == candidate.date()
        and round_trip.hour == candidate.hour
        and round_trip.minute == candidate.minute
    )


def _matches_cadence(definition: RoutineDefinition, day: date) -> bool:
    if definition.cadence == RoutineCadence.WEEKDAYS:
        return day.weekday() < 5
    if definition.cadence == RoutineCadence.WEEKLY:
        return day.isoweekday() in definition.week_days
    return True


def _after_quiet_hours(
    candidate: datetime,
    definition: RoutineDefinition,
    zone: ZoneInfo,
) -> datetime:
    if not definition.quiet_hours_start or not definition.quiet_hours_end:
        return candidate
    start = _clock(definition.quiet_hours_start)
    end = _clock(definition.quiet_hours_end)
    current = candidate.timetz().replace(tzinfo=None)
    if start < end and start <= current < end:
        return datetime.combine(candidate.date(), end, tzinfo=zone)
    if start > end and current >= start:
        return datetime.combine(candidate.date() + timedelta(days=1), end, tzinfo=zone)
    if start > end and current < end:
        return datetime.combine(candidate.date(), end, tzinfo=zone)
    return candidate


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)
