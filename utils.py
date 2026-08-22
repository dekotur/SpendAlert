import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

# No external time/timezone API (timeapi.io) — current time comes from the
# local system clock (the host should keep it synchronized with NTP) and
# the timezone list comes from the stdlib tzdata. Both used to be blocking
# network calls on the request path, which meant one slow/unavailable
# third-party API could stall every user's command.
DEFAULT_TZ = "Europe/Moscow"

POPULAR_TIMEZONES = [
    "Europe/Moscow",
    "Europe/Kaliningrad",
    "Asia/Yekaterinburg",
    "Asia/Novosibirsk",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Dubai",
    "America/New_York",
    "Asia/Tokyo",
    "UTC",
]

MSK = ZoneInfo(DEFAULT_TZ)

_timezones_lock = threading.Lock()
_timezones_cache: dict = {"list": None, "fetched_at": None}


def _normalize_tz(tz: str | None) -> str:
    return tz or DEFAULT_TZ


def _zone_info(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid timezone %s, falling back to %s", tz, DEFAULT_TZ)
        return MSK


def clear_tz_cache(tz: str | None = None) -> None:
    """No-op kept for backward compatibility (time is no longer cached — it
    comes straight from the system clock)."""


def clear_msk_cache() -> None:
    """Backward-compatible alias."""
    clear_tz_cache()


def now_in_tz(tz: str = DEFAULT_TZ) -> datetime:
    """Current time in the given IANA timezone (from the local system clock)."""
    return datetime.now(_zone_info(_normalize_tz(tz)))


def today_in_tz(tz: str = DEFAULT_TZ):
    """Current date in the given IANA timezone."""
    return now_in_tz(tz).date()


def now_msk():
    """Current time in MSK (thin wrapper for diagnostics)."""
    return now_in_tz(DEFAULT_TZ)


def today_msk():
    """Current date in MSK."""
    return today_in_tz(DEFAULT_TZ)


def to_user_tz(dt, tz: str = DEFAULT_TZ):
    """Convert naive UTC datetime to user timezone-aware datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone_info(tz))


def to_msk(dt):
    """Backward-compatible alias for MSK conversion."""
    return to_user_tz(dt, DEFAULT_TZ)


def parse_date(date_str, tz: str = DEFAULT_TZ):
    """Parse date string, optionally with time. Returns (date, time_str_or_None)."""
    tz = _normalize_tz(tz)
    date_str = date_str.strip()

    time_str = None
    time_match = re.search(r'(\d{1,2}):(\d{2})', date_str)
    if time_match:
        time_str = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        date_str = re.sub(r'\s*\d{1,2}:\d{2}\s*', ' ', date_str).strip()

    date_only = date_str.strip()

    if date_only.lower() in ('today',):
        result_date = today_in_tz(tz)
    elif date_only.lower() in ('tomorrow', 'tmr'):
        result_date = today_in_tz(tz) + timedelta(days=1)
    else:
        formats = [
            '%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d',
            '%d/%m/%y', '%d/%m/%Y', '%d-%m-%y', '%d-%m-%Y',
        ]
        result_date = None
        for fmt in formats:
            try:
                result_date = datetime.strptime(date_only, fmt).date()
                break
            except ValueError:
                continue
        if result_date is None:
            raise ValueError(f"Cannot parse date: {date_only}")

    return result_date, time_str


def fetch_available_timezones() -> list[str]:
    """Full IANA timezone list from the stdlib tzdata (no network call, cached
    in-process since available_timezones() rescans the tz database each time)."""
    with _timezones_lock:
        cached_list = _timezones_cache.get("list")
        if cached_list is not None:
            return cached_list
        zones = sorted(available_timezones())
        _timezones_cache["list"] = zones
        return zones


def is_valid_timezone(tz: str) -> bool:
    """Check if timezone is a valid IANA name."""
    try:
        ZoneInfo(tz)
        return True
    except ZoneInfoNotFoundError:
        return False


def search_timezones(query: str, limit: int = 10) -> list[str]:
    """Filter available timezones by substring (Latin, case-insensitive)."""
    query = query.strip().lower()
    if not query:
        return []
    zones = fetch_available_timezones()
    matches = [z for z in zones if query in z.lower()]
    if not matches:
        tokens = [t for t in re.split(r'[\s_/]+', query) if t]
        if tokens:
            matches = [z for z in zones if all(t in z.lower() for t in tokens)]
    return matches[:limit]


def format_tz_current(tz: str) -> str:
    """Human-readable current time in timezone for settings UI."""
    tz = _normalize_tz(tz)
    now = now_in_tz(tz)
    day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    return f"{tz}\n{now.strftime('%H:%M')}, {day_names[now.weekday()]} {now.strftime('%d.%m.%Y')}"


PERIOD_NAMES = {"month": "mo", "quarter": "qtr", "year": "yr"}


def plural_days(n: int) -> str:
    """Plural noun for N days: 1 day / 2 days."""
    return "day" if abs(n) == 1 else "days"


def get_period_str(expense):
    if expense.period == 'none':
        return 'one-off'
    if expense.period == 'custom':
        return f"{expense.period_days}d" if expense.period_days else "---"
    if expense.period in PERIOD_NAMES:
        return PERIOD_NAMES[expense.period]
    return expense.period or "---"


def _add_period(d, period, period_days):
    """Advance a date by one recurrence period. Unknown period → monthly
    (matches the historical fallback in both mark-done paths)."""
    if period == 'month':
        return d + relativedelta(months=+1)
    if period == 'quarter':
        return d + relativedelta(months=+3)
    if period == 'year':
        return d + relativedelta(years=+1)
    if period == 'custom' and period_days:
        return d + timedelta(days=period_days)
    return d + relativedelta(months=+1)


def compute_next_due_date(period, period_days, current_due, today, anchor):
    """Next due date when a recurring record is marked paid/done.

    Single source of truth for both mark-done paths (bot button + /ai), which
    used to duplicate a hybrid `base = max(today, due)` rule. The behaviour is
    now driven by the record's recurrence anchor (database.RECUR_ANCHOR_*):

    - "actual":    next = today + period  — the cadence restarts from the day
      the user actually completed it (done a day early → +period from THAT
      day, not from the originally planned date).
    - "scheduled": next = current_due + period, kept on the original calendar
      grid. If the record was completed well past due, roll forward by whole
      periods until the result is strictly after today, so a long-overdue
      record never resurfaces on an already-past date.

    `current_due`/`today` are dates; returns a date. `anchor` is coerced —
    any value other than "actual" behaves as the default "scheduled".
    """
    if anchor == "actual":
        return _add_period(today, period, period_days)

    nxt = _add_period(current_due, period, period_days)
    guard = 0
    while nxt <= today and guard < 1000:
        nxt = _add_period(nxt, period, period_days)
        guard += 1
    return nxt


# =============================================================================
# Telegram message context (replies, forwards)
# =============================================================================

def _user_display(user) -> str:
    if not user:
        return "unknown"
    name = (user.first_name or "").strip()
    if getattr(user, "last_name", None):
        name = f"{name} {user.last_name}".strip()
    if getattr(user, "username", None):
        name = f"{name} (@{user.username})".strip()
    return name or f"id:{user.id}"


def _format_dt(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.strftime("%d.%m.%Y %H:%M")
    except (AttributeError, ValueError, TypeError):
        return str(dt)


def format_forward_origin(forward_origin) -> str:
    """Human-readable forward origin (MessageOrigin)."""
    if not forward_origin:
        return ""

    date_str = _format_dt(getattr(forward_origin, "date", None))
    origin_type = getattr(forward_origin, "type", None)

    if origin_type == "user":
        who = _user_display(getattr(forward_origin, "sender_user", None))
        return f"from {who}, {date_str}".strip(", ")
    if origin_type == "hidden_user":
        who = getattr(forward_origin, "sender_user_name", None) or "hidden user"
        return f"from {who}, {date_str}".strip(", ")
    if origin_type == "chat":
        chat = getattr(forward_origin, "sender_chat", None)
        title = getattr(chat, "title", None) or "chat"
        sig = getattr(forward_origin, "author_signature", None)
        extra = f", signature: {sig}" if sig else ""
        return f'from chat "{title}"{extra}, {date_str}'.strip(", ")
    if origin_type == "channel":
        chat = getattr(forward_origin, "chat", None)
        title = getattr(chat, "title", None) or "channel"
        sig = getattr(forward_origin, "author_signature", None)
        extra = f", author: {sig}" if sig else ""
        return f'from channel "{title}"{extra}, {date_str}'.strip(", ")
    return date_str


def message_body_text(message) -> str | None:
    """Extract text/caption or a short placeholder for non-text messages."""
    if not message:
        return None
    if getattr(message, "text", None):
        return message.text
    if getattr(message, "caption", None):
        return message.caption
    if getattr(message, "contact", None):
        c = message.contact
        return f"[Contact: {c.first_name} {c.last_name or ''} {c.phone_number}]".strip()
    if getattr(message, "location", None):
        loc = message.location
        return f"[Location: {loc.latitude}, {loc.longitude}]"
    if getattr(message, "document", None):
        return f"[Document: {message.document.file_name or 'unnamed'}]"
    if getattr(message, "photo", None):
        return "[Photo]"
    if getattr(message, "voice", None):
        return "[Voice message]"
    if getattr(message, "video", None):
        return "[Video]"
    if getattr(message, "sticker", None):
        emoji = getattr(message.sticker, "emoji", None) or ""
        return f"[Sticker: {emoji}]".strip()
    if getattr(message, "poll", None):
        return f"[Poll: {message.poll.question}]"
    if getattr(message, "invoice", None):
        return f"[Invoice: {message.invoice.title}]"
    return None


def _external_reply_media_body(external_reply) -> str | None:
    """Media/placeholder body for ExternalReplyInfo (no text field in Bot API)."""
    if not external_reply:
        return None
    if getattr(external_reply, "document", None):
        return f"[Document: {external_reply.document.file_name or 'unnamed'}]"
    if getattr(external_reply, "photo", None):
        return "[Photo]"
    if getattr(external_reply, "voice", None):
        return "[Voice message]"
    if getattr(external_reply, "video", None):
        return "[Video]"
    if getattr(external_reply, "audio", None):
        return "[Audio]"
    if getattr(external_reply, "sticker", None):
        return "[Sticker]"
    if getattr(external_reply, "contact", None):
        c = external_reply.contact
        return f"[Contact: {c.first_name} {c.phone_number}]"
    if getattr(external_reply, "location", None):
        loc = external_reply.location
        return f"[Location: {loc.latitude}, {loc.longitude}]"
    if getattr(external_reply, "venue", None):
        v = external_reply.venue
        return f"[Venue: {getattr(v, 'title', '')} {getattr(v, 'address', '')}]".strip()
    if getattr(external_reply, "poll", None):
        return f"[Poll: {external_reply.poll.question}]"
    if getattr(external_reply, "invoice", None):
        return f"[Invoice: {external_reply.invoice.title}]"
    if getattr(external_reply, "game", None):
        return f"[Game: {getattr(external_reply.game, 'title', '')}]"
    if getattr(external_reply, "dice", None):
        return "[Dice]"
    if getattr(external_reply, "story", None):
        return "[Story]"
    return None


# Backward-compatible alias (tests / callers may still use the old name).
def _external_reply_body(external_reply) -> str | None:
    return _external_reply_media_body(external_reply)


def _quote_text(message) -> str | None:
    """Quoted fragment of the replied-to message (TextQuote), if any."""
    quote = getattr(message, "quote", None) if message else None
    if not quote:
        return None
    text = getattr(quote, "text", None)
    if text and str(text).strip():
        return str(text).strip()
    return None


def describe_external_reply(external_reply, *, quote_text: str | None = None) -> str | None:
    """Format cross-chat reply context (ExternalReplyInfo + optional TextQuote).

    Telegram Bot API does **not** put full message text on ExternalReplyInfo —
    only origin/chat/media. The actual text usually arrives in Message.quote when
    the client sends a reply-with-quote from another chat. Without quote or media,
    we can only report origin and ask the user to forward/paste.
    """
    if not external_reply and not quote_text:
        return None

    lines = ["[Reply to a message from another chat]"]
    media = None
    if external_reply:
        origin_line = format_forward_origin(getattr(external_reply, "origin", None))
        if origin_line:
            lines.append(f"Origin: {origin_line}")
        chat = getattr(external_reply, "chat", None)
        if chat and getattr(chat, "title", None):
            lines.append(f"Chat: {chat.title}")
        mid = getattr(external_reply, "message_id", None)
        if mid is not None:
            lines.append(f"Message ID: {mid}")
        media = _external_reply_media_body(external_reply)
        if media:
            lines.append(f"Attachment: {media}")

    if quote_text:
        lines.append(f"Text: {quote_text}")
    elif not media:
        # No quote + no media: Bot API did not deliver the original text.
        lines.append(
            "The original text is not available to the bot (a Telegram API limit "
            "for replies from another chat). If the full text matters, forward the "
            "message to the bot or paste it into /ai by hand."
        )
    return "\n".join(lines)


def describe_message_for_context(message, *, heading: str) -> str | None:
    """Format a Telegram message for LLM/bot context."""
    if not message:
        return None

    body = message_body_text(message)
    forward_info = format_forward_origin(getattr(message, "forward_origin", None))

    sender = ""
    if getattr(message, "from_user", None):
        sender = _user_display(message.from_user)
    elif getattr(message, "sender_chat", None):
        sender = message.sender_chat.title or "chat"

    lines = [heading]
    if sender:
        lines.append(f"From: {sender}")
    if forward_info:
        lines.append(f"Forwarded: {forward_info}")
    if getattr(message, "date", None):
        lines.append(f"Date: {_format_dt(message.date)}")
    if body:
        lines.append(f"Text: {body}")
    elif not forward_info and not sender:
        return None
    return "\n".join(lines)


def effective_conversation_text(message) -> str:
    """Typed text, or replied/forwarded/quoted body when the user sent an empty reply."""
    typed = (getattr(message, "text", None) or "").strip()
    if typed:
        return typed
    reply = getattr(message, "reply_to_message", None)
    if reply:
        body = message_body_text(reply)
        if body:
            return body
    quote = _quote_text(message)
    if quote:
        return quote
    if getattr(message, "forward_origin", None):
        body = message_body_text(message)
        if body:
            return body
    return typed


def build_ai_user_message(message, user_text: str = "") -> str:
    """Build user prompt for /ai with reply, forward, external_reply and quote."""
    parts: list[str] = []
    quote_text = _quote_text(message)
    quote_consumed = False

    if getattr(message, "forward_origin", None):
        block = describe_message_for_context(
            message, heading="[Forwarded message from the user]"
        )
        if block:
            parts.append(block)

    reply = getattr(message, "reply_to_message", None)
    external = getattr(message, "external_reply", None)
    if reply:
        block = describe_message_for_context(reply, heading="[Reply to a message]")
        if block:
            parts.append(block)
        # Same-chat reply may still carry a partial quote of the replied message.
        if quote_text:
            parts.append(f'[Quote]: "{quote_text}"')
            quote_consumed = True
    elif external:
        # Cross-chat reply: text is almost never on ExternalReplyInfo — use quote.
        block = describe_external_reply(external, quote_text=quote_text)
        if block:
            parts.append(block)
            quote_consumed = True

    if quote_text and not quote_consumed:
        parts.append(f'[Quote]: "{quote_text}"')

    user_text = (user_text or "").strip()
    if user_text:
        parts.append(f"[User request]\n{user_text}")
    elif parts:
        parts.append(
            "[User request]\n(no text: act on the forwarded or replied context above)"
        )

    return "\n\n".join(parts)


def has_telegram_message_context(message) -> bool:
    """True if message uses reply, forward, external_reply or quote."""
    if not message:
        return False
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "reply_to_message", None)
        or getattr(message, "external_reply", None)
        or _quote_text(message)
    )
