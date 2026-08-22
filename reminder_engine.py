"""
Reminder scheduling engine (v3 — plan-based slots, docs/reminder-plan.md).

Each expense carries a reminder plan (reminder_plan.py): up to 4 pre-due
waves n0..n3, a due-day slot d and an overdue slot f. NULL plan = the v2
default preset, so pre-v3 rows keep their behavior with no data migration.

State on the expense row:
  next_reminder_at     naive UTC — the NEXT scheduled fire. Unlike v2 this
                       always points forward (possibly months ahead, e.g. a
                       n0=90d wave), so the minute-cron fire path needs no
                       widening of the hourly sweep window to support long
                       offsets — the sweep is just a safety net.
  reminder_slot        n0|n1|n2|n3|d|f — the slot of the LAST SENT episode.
                       Written only by record_reminder_sent, never by
                       recomputes: this pair is the engine's only memory that
                       a finite episode was exhausted, and if a recompute
                       advanced it to the next scheduled episode, the hourly
                       sweep would forget the exhaustion and resurrect the
                       episode (duplicate pings). Reset when the due date
                       changes (ORM listener in database.py — covers
                       mark-paid, /edit date, AI reschedule, tz recalcs).
  reminder_slot_sends  sends inside that episode.
  reminder_stage       legacy v2 mirror (3_days|…|overdue) — still written so
                       a code rollback to the v2 engine finds sane state; the
                       fire batch also orders by it (urgency == days-until).

An "episode" is one slot's calendar span: each n wave lives exactly one
local day (due - offset_days, §2.2/U9), d lives on the due day, f starts the
day after due and is open-ended. Repeats inside an episode run every
`every_hours`; when times is exhausted (or the day ends for n/d), the engine
immediately schedules the FIRST fire of the next episode instead of going
NULL and waiting for a sweep.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from database import (
    Expense, ReminderLog, User, get_db, get_user_timezone,
    get_user_reminder_hour, get_user_quiet_hours, migrate_db, init_db,
    DEFAULT_TIMEZONE,
)
from reminder_plan import (
    has_any_reminders, iter_episodes, parse_at_time, plan_from_json,
    slot_for_day, slot_times,
)
from utils import now_in_tz, to_user_tz
from config import DEFAULT_FIRE_HOUR, QUIET_START_HOUR, QUIET_END_HOUR

logger = logging.getLogger(__name__)

# Without this, an abandoned expense (user ignored it, or the account is
# inactive but not blocked) with an ∞/frequent f slot resends every 2h
# forever — an unbounded stream of sends/ReminderLog rows/send-slots that
# never self-corrects. After this many days unacknowledged, overdue backs
# off to at most once/day regardless of the configured interval (§6.5 —
# mandatory safety, applies to custom plans too).
OVERDUE_BACKOFF_DAYS = 7
OVERDUE_BACKOFF_INTERVAL_HOURS = 24

# Anti-spam for same-day adds: a d slot without an explicit time won't fire
# earlier than this long after the expense was created (matches v2).
D_DAY_CREATE_COOLDOWN_HOURS = 2

# Hourly sweep horizon: refresh rows whose fire is near (or whose due date
# is near, for d/f transitions). Far-future precomputed fires (long n
# offsets) don't need hourly touching — the fire path re-validates the slot
# at send time anyway.
SWEEP_FIRE_HORIZON_HOURS = 36
SWEEP_DUE_WINDOW_DAYS = 5

# Urgency order for the fire batch, over the reminder_stage mirror — an
# exact days-until proxy, unlike slot keys whose index depends on how many
# waves a plan has (a single "1 day before" wave lands in n0). Far pre-due
# waves (days_until > 3 → stage NULL) are the least urgent by definition.
STAGE_PRIORITY = {"overdue": 0, "d_day": 1, "1_day": 2, "2_days": 3, "3_days": 4}


def stage_from_days_until(days_until: int) -> str | None:
    """Legacy v2 stage name for the reminder_stage mirror column."""
    if days_until > 3:
        return None
    if days_until == 3:
        return "3_days"
    if days_until == 2:
        return "2_days"
    if days_until == 1:
        return "1_day"
    if days_until == 0:
        return "d_day"
    return "overdue"


def to_utc_naive(dt: datetime) -> datetime:
    """Convert aware (or naive UTC) datetime to naive UTC."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def local_datetime_on_date(tz: str, d: date, hour: int = 0, minute: int = 0) -> datetime:
    """Build timezone-aware local datetime on a calendar date."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZoneInfo(tz))


def effective_every_hours(slot_key: str, slot: dict, days_overdue: int) -> float:
    """Slot repeat interval with the mandatory f backoff (§6.5) applied."""
    every = float(slot["every_hours"])
    if slot_key == "f" and days_overdue > OVERDUE_BACKOFF_DAYS:
        every = max(every, OVERDUE_BACKOFF_INTERVAL_HOURS)
    return every


def _d_slot_at_time(expense: Expense, slot: dict) -> str | None:
    """Effective HH:MM for the d slot: explicit plan at_time wins, else the
    expense-level reminder_time parsed from the date input (U11)."""
    return slot.get("at_time") or expense.reminder_time


def _episode_earliest_local(
    slot_key: str, slot: dict, episode_day: date, expense: Expense, user_tz: str,
    default_hour: int = DEFAULT_FIRE_HOUR,
) -> datetime:
    """Earliest local datetime the episode's first fire may happen. With no
    explicit time (plan at_time / expense.reminder_time), the slot fires at
    default_hour local (the user's reminder_hour) instead of midnight — a
    "3 days before" wave should ping in the daytime, not at 00:00."""
    at_time = slot.get("at_time")
    if slot_key == "d":
        at_time = _d_slot_at_time(expense, slot)
    parsed = parse_at_time(at_time)
    if parsed:
        return local_datetime_on_date(user_tz, episode_day, *parsed)

    earliest = local_datetime_on_date(user_tz, episode_day, default_hour, 0)
    if slot_key == "d" and expense.created_at:
        created_local = to_user_tz(expense.created_at, user_tz)
        cooldown_end = created_local + timedelta(hours=D_DAY_CREATE_COOLDOWN_HOURS)
        earliest = max(earliest, cooldown_end)
    return earliest


def _shift_out_of_quiet(next_local: datetime) -> datetime:
    """Push a fire time out of the night quiet window (config.QUIET_START_HOUR
    .. QUIET_END_HOUR local) up to QUIET_END_HOUR. Monotonic-forward — only
    ever delays, never drops or duplicates. Applied only to slots without an
    explicit time; a user-set time is honored as-is."""
    h = next_local.hour
    if h >= QUIET_START_HOUR:        # late evening → tomorrow morning
        base = next_local + timedelta(days=1)
    elif h < QUIET_END_HOUR:         # small hours → this morning
        base = next_local
    else:
        return next_local           # already outside the quiet window
    return base.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)


def compute_plan_state(
    expense: Expense,
    plan: dict,
    user_tz: str,
    now_local: datetime | None = None,
    current_slot: str | None = None,
    current_sends: int = 0,
    last_sent_local: datetime | None = None,
    default_hour: int = DEFAULT_FIRE_HOUR,
    quiet: bool = False,
) -> tuple[str | None, int, datetime | None]:
    """
    Compute the next scheduled fire for an expense under a plan.

    Returns (slot_key, sends, next_at_utc) where sends is the send count that
    belongs to the returned slot (current count if it's still the same
    episode, 0 for a fresh one), or (None, 0, None) when nothing is left to
    fire (all slots off/exhausted).
    """
    if not expense.is_active:
        return None, 0, None

    if now_local is None:
        now_local = now_in_tz(user_tz)

    today = now_local.date()
    days_until = (expense.next_payment_date - today).days
    days_overdue = -days_until

    for slot_key, slot, day_delta in iter_episodes(plan, days_until):
        sends = current_sends if slot_key == current_slot else 0
        if sends >= slot_times(slot):
            continue  # exhausted → next episode

        episode_day = today + timedelta(days=day_delta)
        earliest = _episode_earliest_local(slot_key, slot, episode_day, expense, user_tz, default_hour)

        if sends == 0:
            next_local = max(earliest, now_local)
        else:
            every = effective_every_hours(slot_key, slot, days_overdue)
            base = last_sent_local if last_sent_local is not None else now_local
            next_local = max(base + timedelta(hours=every), earliest)

        # Night quiet hours — only for slots without an explicit time (an
        # explicit reminder time is a deliberate choice, honored as-is).
        if quiet:
            at_time = _d_slot_at_time(expense, slot) if slot_key == "d" else slot.get("at_time")
            if parse_at_time(at_time) is None:
                next_local = _shift_out_of_quiet(next_local)

        # Pre-due waves and the d slot repeat only within their own calendar
        # day (§2.2/U9): whatever doesn't fit is dropped, move on. (A quiet
        # shift can push a late d/n repeat to the next morning → dropped here,
        # handing over to the next episode, which is the intended behavior.)
        if slot_key != "f" and next_local.date() > episode_day:
            continue

        return slot_key, sends, to_utc_naive(next_local)

    return None, 0, None


def get_last_reminder_log(db, expense_id: int, stage: str) -> ReminderLog | None:
    return (
        db.query(ReminderLog)
        .filter(ReminderLog.expense_id == expense_id, ReminderLog.stage == stage)
        .order_by(ReminderLog.sent_at.desc())
        .first()
    )


def apply_reminder_state(
    expense: Expense,
    user_tz: str,
    db=None,
    last_sent_local: datetime | None = None,
    default_hour: int | None = None,
    quiet: bool | None = None,
) -> tuple[str | None, datetime | None]:
    """Compute and persist next_reminder_at (+ the legacy reminder_stage
    mirror) on the expense. Returns (scheduled_slot_key, next_at).

    reminder_slot / reminder_slot_sends are deliberately NOT written here:
    they record the last episode a send actually happened in (only
    record_reminder_sent advances them) and are the engine's only memory
    that a finite episode was exhausted. If a recompute moved them to the
    next scheduled episode, the hourly sweep would forget the exhaustion and
    resurrect the episode as fresh — one duplicate ping per sweep. They are
    cleared here only when the row can never fire again (inactive /
    no-reminder plan) and reset by the ORM listener in database.py when the
    due date changes.

    When the scheduled episode already has sends and no explicit last-send
    time is passed, the last matching ReminderLog row provides the repeat
    base; if the log was pruned, `now` is used (delays one repeat by
    every_hours rather than risking an immediate duplicate)."""
    if not expense.is_active:
        expense.reminder_stage = None
        expense.reminder_slot = None
        expense.reminder_slot_sends = 0
        expense.next_reminder_at = None
        return None, None

    plan = plan_from_json(expense.reminder_plan)
    if not has_any_reminders(plan):
        expense.reminder_stage = None
        expense.reminder_slot = None
        expense.reminder_slot_sends = 0
        expense.next_reminder_at = None
        return None, None

    now_local = now_in_tz(user_tz)
    days_until = (expense.next_payment_date - now_local.date()).days

    if default_hour is None:
        default_hour = get_user_reminder_hour(db, expense.user_id) if db is not None else DEFAULT_FIRE_HOUR
    if quiet is None:
        quiet = get_user_quiet_hours(db, expense.user_id) if db is not None else False

    current_slot = expense.reminder_slot
    current_sends = expense.reminder_slot_sends or 0

    if last_sent_local is None and db is not None and current_slot and current_sends > 0:
        last_log = get_last_reminder_log(db, expense.id, current_slot)
        if last_log and last_log.sent_at:
            last_sent_local = to_user_tz(last_log.sent_at, user_tz)

    slot, sends, next_at = compute_plan_state(
        expense, plan, user_tz,
        now_local=now_local,
        current_slot=current_slot,
        current_sends=current_sends,
        last_sent_local=last_sent_local,
        default_hour=default_hour,
        quiet=quiet,
    )

    expense.next_reminder_at = next_at
    expense.reminder_stage = stage_from_days_until(days_until) if next_at else None
    return slot, next_at


def slot_due_for_send(expense: Expense, user_tz: str, db=None) -> str | None:
    """Fire-path gate: which slot should a due row send for right now?

    The slot is derived from the plan and today's date, never from the
    stored reminder_slot (that pair only remembers the last SENT episode).
    An exhausted or quiet day recomputes the schedule in place (persisted
    when the caller's session commits) and returns None, so the cron stops
    re-picking the row."""
    plan = plan_from_json(expense.reminder_plan)
    now_local = now_in_tz(user_tz)
    days_until = (expense.next_payment_date - now_local.date()).days
    active = slot_for_day(plan, days_until)
    if active is None:
        apply_reminder_state(expense, user_tz, db)
        return None

    slot_key, slot_cfg = active
    sends = (expense.reminder_slot_sends or 0) if expense.reminder_slot == slot_key else 0
    if sends >= slot_times(slot_cfg):
        apply_reminder_state(expense, user_tz, db)
        return None
    return slot_key


def d_day_send_allowed(
    expense: Expense,
    user_tz: str,
    now_local: datetime | None = None,
) -> bool:
    """True if due-day constraints are satisfied.

    Explicit time (plan d.at_time or expense.reminder_time): fire at/after
    that clock time (no create cooldown). No explicit time: 2h cooldown
    after create (anti-spam for same-day adds)."""
    if now_local is None:
        now_local = now_in_tz(user_tz)

    plan = plan_from_json(expense.reminder_plan)
    at_time = _d_slot_at_time(expense, plan["d"]) if plan["d"].get("on") else expense.reminder_time
    if at_time:
        parsed = parse_at_time(at_time)
        if parsed:
            hour, minute = parsed
            return not (now_local.hour < hour or (now_local.hour == hour and now_local.minute < minute))
        logger.warning("Invalid reminder time for expense %s: %s", expense.id, at_time)

    if expense.created_at:
        created_local = to_user_tz(expense.created_at, user_tz)
        if (now_local - created_local).total_seconds() < D_DAY_CREATE_COOLDOWN_HOURS * 3600:
            return False
    return True


def recompute_for_expense(db, expense_id: int) -> tuple[str | None, datetime | None]:
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise ValueError(f"Expense {expense_id} not found")
    user_tz = get_user_timezone(db, expense.user_id)
    return apply_reminder_state(expense, user_tz, db)


def recompute_for_user(db, user_id: int) -> int:
    """Recompute all active expenses for a user. Returns count updated."""
    user_tz = get_user_timezone(db, user_id)
    default_hour = get_user_reminder_hour(db, user_id)
    quiet = get_user_quiet_hours(db, user_id)
    expenses = db.query(Expense).filter(
        Expense.user_id == user_id,
        Expense.is_active == True,
    ).all()
    for expense in expenses:
        apply_reminder_state(expense, user_tz, db, default_hour=default_hour, quiet=quiet)
    return len(expenses)


def backfill_all(db) -> dict:
    """Idempotent backfill of next_reminder_at for all active expenses."""
    expenses = db.query(Expense).filter(Expense.is_active == True).all()
    scheduled = 0
    cleared = 0
    for expense in expenses:
        user_tz = get_user_timezone(db, expense.user_id)
        slot, next_at = apply_reminder_state(expense, user_tz, db)
        if slot is None:
            cleared += 1
        else:
            scheduled += 1
    stats = {
        "total_active": len(expenses),
        "scheduled": scheduled,
        "nothing_to_fire": cleared,
    }
    logger.info("Reminder backfill: %s", stats)
    return stats


def recompute_after_mutation(
    db,
    expense_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Refresh reminder schedule after expense or user mutation."""
    if expense_id is not None:
        recompute_for_expense(db, expense_id)
    elif user_id is not None:
        recompute_for_user(db, user_id)


def record_reminder_sent(
    db,
    expense_id: int,
    slot_key: str,
    message_id: int | None,
    sent_at_utc: datetime | None = None,
) -> None:
    """Log a sent reminder, bump the episode counter and schedule the next
    fire (which may already belong to the NEXT episode if this send
    exhausted the current one)."""
    if sent_at_utc is None:
        sent_at_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense:
        if expense.reminder_slot == slot_key:
            expense.reminder_slot_sends = (expense.reminder_slot_sends or 0) + 1
        else:
            expense.reminder_slot = slot_key
            expense.reminder_slot_sends = 1
        user_tz = get_user_timezone(db, expense.user_id)
        apply_reminder_state(
            expense, user_tz, db,
            last_sent_local=to_user_tz(sent_at_utc, user_tz),
        )
    log = ReminderLog(
        expense_id=expense_id,
        stage=slot_key,
        message_id=message_id,
        sent_at=sent_at_utc,
    )
    db.add(log)


def recompute_stages_for_window(db) -> int:
    """Hourly sweep: refresh rows whose fire or due date is near.

    Scoped in SQL — near-due rows (d/f transitions) plus rows whose
    precomputed next fire is within SWEEP_FIRE_HORIZON_HOURS — instead of
    loading every active expense: far-future fires (long n offsets are
    precomputed months ahead) don't change hourly, and the fire path
    re-validates the slot at send time anyway. Timezones are batch-fetched
    once instead of one query per expense (N+1)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    due_cutoff = now_utc.date() + timedelta(days=SWEEP_DUE_WINDOW_DAYS)
    fire_horizon = now_utc + timedelta(hours=SWEEP_FIRE_HORIZON_HOURS)

    expenses = db.query(Expense).filter(
        Expense.is_active == True,
        or_(
            Expense.next_payment_date <= due_cutoff,
            Expense.next_reminder_at <= fire_horizon,
            # Custom-plan rows with no schedule: waves beyond the due window
            # would otherwise be orphaned if next_reminder_at was ever nulled
            # (e.g. a v2-code rollback's sweep clears far-future fires).
            # NULL-plan rows never need this — the default preset's earliest
            # wave (3d) is inside the due window. Exhausted custom rows
            # recompute to NULL again, which is cheap and stable.
            (Expense.next_reminder_at.is_(None)) & (Expense.reminder_plan.isnot(None)),
        ),
    ).all()

    user_ids = {e.user_id for e in expenses}
    user_rows = db.query(
        User.id, User.timezone, User.reminder_hour, User.quiet_hours,
    ).filter(User.id.in_(user_ids)).all()
    tz_map = {uid: tz for uid, tz, _, _ in user_rows}
    hour_map = {uid: hr for uid, _, hr, _ in user_rows}
    quiet_map = {uid: q for uid, _, _, q in user_rows}

    for expense in expenses:
        user_tz = tz_map.get(expense.user_id) or DEFAULT_TIMEZONE
        hour = hour_map.get(expense.user_id)
        if hour is None or not (0 <= hour <= 23):
            hour = DEFAULT_FIRE_HOUR
        quiet = quiet_map.get(expense.user_id)
        quiet = True if quiet is None else bool(quiet)
        apply_reminder_state(expense, user_tz, db, default_hour=hour, quiet=quiet)
    logger.info("Reminder window sweep: %d expenses updated", len(expenses))
    return len(expenses)


def _run_backfill_cli():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    migrate_db()
    with get_db() as db:
        stats = backfill_all(db)
    print(f"Backfill complete: {stats}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reminder engine utilities")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Compute next_reminder_at for all active expenses",
    )
    args = parser.parse_args()
    if args.backfill:
        _run_backfill_cli()
    else:
        parser.print_help()
