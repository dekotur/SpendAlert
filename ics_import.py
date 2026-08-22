"""Parse .ics calendar invites (RFC 5545) sent as Telegram documents.

Uses the `icalendar` package rather than hand-rolled parsing because
real-world invites (Outlook/Exchange in particular) declare their own
non-IANA TZID names (e.g. "(UTC+03:00) Moscow, St. Petersburg") resolved via
an embedded VTIMEZONE block — icalendar computes the correct UTC offset from
that block instead of needing to recognize the TZID string itself.

Two consumers, both in bot.py: a deterministic document handler (any .ics
dropped on the bot becomes a task immediately, no AI involved) and the /ai
handler (formats the same parsed event as text context for the model).
"""
from datetime import date as date_cls, datetime

from icalendar import Calendar
from zoneinfo import ZoneInfo

MAX_EVENTS_PER_FILE = 20  # guard against a pathological .ics with many VEVENTs

# Upfront size cap, checked against Telegram's Document.file_size *before*
# downloading — MAX_EVENTS_PER_FILE only trims the returned list after
# Calendar.from_ical() has already parsed the whole component tree, so it
# does nothing to bound parse cost for a file packed with many components.
# Real invites are a few KB; this is generous headroom.
MAX_ICS_FILE_BYTES = 2 * 1024 * 1024  # 2 MB


def is_ics_filename(filename: str | None, mime_type: str | None = None) -> bool:
    """True if a Telegram document looks like a calendar invite."""
    if filename and filename.lower().endswith(".ics"):
        return True
    if mime_type and mime_type.lower() in ("text/calendar", "application/ics"):
        return True
    return False


_RRULE_COMPLICATING_KEYS = (
    "BYDAY", "BYMONTHDAY", "BYMONTH", "BYYEARDAY", "BYWEEKNO",
    "BYSETPOS", "COUNT", "UNTIL",
)


def _period_from_rrule(rrule) -> tuple[str, int | None]:
    """Best-effort mapping of a simple RRULE to this app's period model.
    Anything not cleanly expressible as plain FREQ+INTERVAL — BYDAY combos,
    COUNT/UNTIL limits, etc. — falls back to 'none' (one-time) rather than
    guessing wrong (e.g. FREQ=WEEKLY;BYDAY=MO,WE,FR is 3x/week, NOT a plain
    7-day period). The user can always make it recurring via /edit."""
    if not rrule:
        return "none", None
    if any(rrule.get(key) for key in _RRULE_COMPLICATING_KEYS):
        return "none", None
    freq_list = rrule.get("FREQ")
    freq = freq_list[0] if freq_list else None
    interval_list = rrule.get("INTERVAL")
    interval = interval_list[0] if interval_list else 1
    if freq == "DAILY":
        return "custom", int(interval)
    if freq == "WEEKLY" and interval == 1:
        return "custom", 7
    if freq == "MONTHLY" and interval == 1:
        return "month", None
    if freq == "YEARLY" and interval == 1:
        return "year", None
    return "none", None


def _to_datetime(value) -> datetime:
    """DTSTART.dt is a `date` for all-day events, a naive datetime for
    RFC5545 'floating' time, or a tz-aware datetime otherwise."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_cls):
        return datetime(value.year, value.month, value.day)
    raise ValueError(f"Unsupported DTSTART type: {type(value)}")


def parse_ics_events(data: bytes) -> list[dict]:
    """Parse raw .ics bytes into a list of event dicts:
    {title, start, location, organizer, period, period_days}.
    Raises ValueError on unparseable input; returns [] if the file parses
    but has no VEVENT with a usable DTSTART."""
    try:
        cal = Calendar.from_ical(data)
    except Exception as e:
        raise ValueError(f"Could not parse the .ics file: {e}") from e

    events = []
    for component in cal.walk("VEVENT"):
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        try:
            start = _to_datetime(dtstart.dt)
        except ValueError:
            continue

        summary = str(component.get("SUMMARY") or "Meeting").strip()
        location = component.get("LOCATION")
        location = str(location).strip() or None if location else None

        organizer = component.get("ORGANIZER")
        organizer_name = None
        if organizer:
            organizer_name = organizer.params.get("CN") or str(organizer).replace("MAILTO:", "").replace("mailto:", "")

        period, period_days = _period_from_rrule(component.get("RRULE"))

        events.append({
            "title": summary,
            "start": start,
            "location": location,
            "organizer": organizer_name,
            "period": period,
            "period_days": period_days,
        })

        if len(events) >= MAX_EVENTS_PER_FILE:
            break

    return events


def event_local_date_and_time(event: dict, user_tz: str) -> tuple[date_cls, str | None]:
    """Convert a parsed event's start instant to the (date, 'HH:MM') pair
    this app stores expenses with, in the user's own configured timezone —
    matches utils.parse_date's return contract exactly."""
    start = event["start"]
    if start.tzinfo is not None:
        local = start.astimezone(ZoneInfo(user_tz))
    else:
        local = start  # RFC5545 "floating" time — treat as already-local wall clock
    return local.date(), local.strftime("%H:%M")


def format_event_for_ai(event: dict, user_tz: str) -> str:
    """Human-readable block to inject into the /ai prompt context."""
    local_date, local_time = event_local_date_and_time(event, user_tz)
    lines = [
        f"Title: {event['title']}",
        f"Date and time: {local_date.strftime('%d.%m.%Y')} {local_time} ({user_tz})",
    ]
    if event.get("location"):
        lines.append(f"Location or link: {event['location']}")
    if event.get("organizer"):
        lines.append(f"Organizer: {event['organizer']}")
    if event["period"] != "none":
        lines.append(f"Repeats: {event['period']}" + (f" (every {event['period_days']} days)" if event['period_days'] else ""))
    return "\n".join(lines)
