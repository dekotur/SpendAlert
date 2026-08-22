import os
import json
from sqlalchemy import create_engine, event, Column, Integer, String, Date, DateTime, Boolean, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.types import TypeDecorator
from datetime import datetime, date, timedelta, timezone
from contextlib import contextmanager
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config  # central configuration for paths and URLs
import crypto_utils

logger = logging.getLogger(__name__)


class EncryptedString(TypeDecorator):
    """Transparent at-rest encryption for a text column — the rest of the
    codebase reads/writes plaintext via the ORM attribute as usual; only the
    bytes stored in SQLite are ciphertext. See crypto_utils.py for what this
    does and doesn't protect against."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return crypto_utils.encrypt_str(value)

    def process_result_value(self, value, dialect):
        return crypto_utils.decrypt_str(value)


class EncryptedFloat(TypeDecorator):
    """Same as EncryptedString, for a nullable numeric column (Expense.amount)."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return crypto_utils.encrypt_str(repr(value))

    def process_result_value(self, value, dialect):
        decrypted = crypto_utils.decrypt_str(value)
        return None if decrypted is None else float(decrypted)

DEFAULT_TIMEZONE = "Europe/Moscow"

# /list ordering preference (users.list_sort). "date" = payments then tasks,
# each group soonest-due first; "id" = same groups, ordered by user_seq.
LIST_SORT_DATE = "date"
LIST_SORT_ID = "id"
LIST_SORT_MODES = frozenset({LIST_SORT_DATE, LIST_SORT_ID})
DEFAULT_LIST_SORT = LIST_SORT_DATE

# expenses.recur_anchor — how the NEXT due date is computed when a recurring
# record is marked paid/done:
#   "scheduled" — next = current due + period (calendar-fixed grid; rolls
#                 forward whole periods if the record was completed well past
#                 due, so it never lands in the past). Default.
#   "actual"    — next = completion day (today) + period (cadence resets from
#                 when the user actually did it — e.g. "changed the pillowcase
#                 a day early, so +N days from THAT day").
# Irrelevant for one-time records (period='none'). NULL is read as the default.
RECUR_ANCHOR_SCHEDULED = "scheduled"
RECUR_ANCHOR_ACTUAL = "actual"
RECUR_ANCHOR_MODES = frozenset({RECUR_ANCHOR_SCHEDULED, RECUR_ANCHOR_ACTUAL})
DEFAULT_RECUR_ANCHOR = RECUR_ANCHOR_SCHEDULED


def normalize_recur_anchor(value) -> str:
    """Coerce a stored/incoming anchor to a valid mode (NULL/unknown → default).
    Use on CREATE, where a value must always be written."""
    return value if value in RECUR_ANCHOR_MODES else DEFAULT_RECUR_ANCHOR


def apply_recur_anchor(expense, value) -> bool:
    """Single policy for CHANGING the anchor on an existing record: write it
    only when `value` is an explicit valid mode AND the record is recurring
    (one-time rows have no next occurrence to anchor). Unknown/None/omitted
    values leave the current value untouched — so all edit paths (button and
    /ai) treat a missing or garbage value identically instead of one coercing
    it down to the default while another ignores it. Returns whether it wrote."""
    if value in RECUR_ANCHOR_MODES and expense.period not in (None, "none"):
        expense.recur_anchor = value
        return True
    return False


def utcnow_naive():
    """Return current UTC time as naive datetime (for SQLite compatibility)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

Base = declarative_base()

# Use SQLite database (from central config or env override)
DATABASE_URL = os.getenv("DATABASE_URL", config.DATABASE_URL)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    """WAL lets readers proceed while a writer commits (default 'delete' journal
    locks the whole file on every write); busy_timeout retries instead of failing
    immediately when two writers collide."""
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@contextmanager
def get_db():
    """Context manager for database sessions with proper commit/rollback/close."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)  # Telegram user ID
    timezone = Column(String, default="Europe/Moscow")
    default_currency = Column(String, default="RUB")
    list_sort = Column(String, default=DEFAULT_LIST_SORT)  # "date" | "id"
    reminder_hour = Column(Integer, default=config.DEFAULT_FIRE_HOUR)  # local hour reminders fire when the slot has no explicit time
    quiet_hours = Column(Boolean, default=True)  # suppress no-explicit-time reminders 23:00–08:00 local (config.QUIET_*)
    # Permanent free-text AI assistant: when True, non-command free text → OpenRouter
    # (except mid-/add|/task|/edit|/settings). /ai toggles this. Default OFF = slash-only bot.
    ai_mode = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow_naive)

    expenses = relationship("Expense", back_populates="user")


class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user_seq = Column(Integer, nullable=True)  # per-user ID shown in /list, /edit, /delete, /ai
    expense_type = Column(String, default="payment")  # "payment" or "task"
    title = Column(EncryptedString, nullable=False)
    amount = Column(EncryptedFloat, nullable=True)  # Nullable for tasks
    currency = Column(EncryptedString, default="RUB")
    next_payment_date = Column(Date, nullable=False)
    reminder_time = Column(String, nullable=True)  # "HH:MM" or None
    period = Column(String, nullable=True)  # "month", "quarter", "year", or None for custom
    period_days = Column(Integer, nullable=True)  # Custom period in days
    recur_anchor = Column(String, default=RECUR_ANCHOR_SCHEDULED)  # "scheduled"|"actual" — next-due basis on mark-done (see RECUR_ANCHOR_*)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow_naive)
    next_reminder_at = Column(DateTime, nullable=True)  # naive UTC — when to fire next reminder
    reminder_stage = Column(String, nullable=True)  # legacy v2 mirror (3_days|…|overdue|NULL) — kept for code rollback safety, new code reads reminder_slot
    deactivated_at = Column(DateTime, nullable=True)  # naive UTC — when is_active flipped to False; drives purge_old_inactive_expenses
    # Reminder plan v3 (docs/reminder-plan.md): NULL plan = v2 default preset
    reminder_plan = Column(String, nullable=True)  # JSON (reminder_plan.plan_to_json); not PII → not encrypted
    reminder_slot = Column(String, nullable=True)  # n0|n1|n2|n3|d|f — slot of the last sent episode
    reminder_slot_sends = Column(Integer, nullable=False, default=0)  # sends within that episode

    user = relationship("User", back_populates="expenses")
    reminders = relationship("ReminderLog", back_populates="expense")


@event.listens_for(Expense.next_payment_date, "set", active_history=True)
def _reset_reminder_episode_on_due_change(target, value, oldvalue, initiator):
    """reminder_slot_sends counts sends within one slot episode, and the
    episode is implicitly keyed to the due date (docs/reminder-plan.md §6.4):
    when the date shifts, the counter must reset, otherwise a monthly
    expense whose "3 days before" wave already fired this period would silently
    skip the same-keyed wave next period. ORM-level so EVERY mutation site
    is covered at once — bot mark-paid, /edit date, AI reschedule/mark-done,
    tz recalculations, snapshot restores."""
    from sqlalchemy.orm.attributes import NO_VALUE

    if oldvalue is None or oldvalue is NO_VALUE or value == oldvalue:
        return
    target.reminder_slot = None
    target.reminder_slot_sends = 0


@event.listens_for(Expense.reminder_time, "set", active_history=True)
def _reset_reminder_episode_on_time_change(target, value, oldvalue, initiator):
    """A new explicit clock time starts a new reminder episode.

    The due date alone is not a complete episode key: a same-day move such
    as ``13:30 -> 13:45`` must fire at 13:45 even when the due-day slot has
    already sent.  Without this reset, the engine keeps the previous slot's
    repeat cadence (for the default plan, +2h from the last send) and silently
    skips the requested time.

    ``None`` is a meaningful stored value, so only skip construction-time
    ``NO_VALUE`` and no-op assignments.
    """
    from sqlalchemy.orm.attributes import NO_VALUE

    if oldvalue is NO_VALUE or value == oldvalue:
        return
    target.reminder_slot = None
    target.reminder_slot_sends = 0


@event.listens_for(Expense.reminder_plan, "set", active_history=True)
def _reset_reminder_episode_on_plan_change(target, value, oldvalue, initiator):
    """reminder_slot/reminder_slot_sends are POSITIONAL (n0..n3 by offset
    descending). Editing the plan re-sorts and re-indexes the waves, so the
    stored positional key would silently map to a DIFFERENT wave after the
    change — re-firing an already-sent wave (duplicate) or treating a kept
    wave as already-done (lost ping). Any plan mutation must therefore reset
    the episode memory, exactly like a due-date change. ORM-level so both the
    button editor (_update_reminder_plan_sync) and /ai (set_reminder_plan)
    are covered.

    NOTE: unlike the due-date listener this must NOT early-return on
    `oldvalue is None` — a NULL plan means "the default v2 preset", a real
    prior value; changing away from it (NULL→custom) is exactly the case that
    must reset. Only NO_VALUE (attribute set during row construction) is
    skipped."""
    from sqlalchemy.orm.attributes import NO_VALUE

    if oldvalue is NO_VALUE or value == oldvalue:
        return
    target.reminder_slot = None
    target.reminder_slot_sends = 0


class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    expense_id = Column(Integer, ForeignKey("expenses.id"), nullable=False)
    stage = Column(String, nullable=False)  # "3_days", "2_days", "1_day", "d_day", "overdue"
    sent_at = Column(DateTime, default=utcnow_naive)
    message_id = Column(Integer, nullable=True)  # Telegram message_id of the sent reminder (for bulk deletion)

    expense = relationship("Expense", back_populates="reminders")


class AISession(Base):
    """One row per user's /ai dialog state — replaces the old shared
    data/ai_conversation_history.json (single file for every user, full
    read+rewrite on every /ai call, no locking)."""
    __tablename__ = "ai_sessions"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    messages_json = Column(EncryptedString, nullable=False, default="[]")
    last_activity = Column(DateTime, nullable=True)  # naive UTC, like other timestamps


class UserBackup(Base):
    """Lightweight per-user JSON snapshot of a single user's own rows, taken
    before an AI-driven mutation so it can be listed/restored by that user.
    Separate from the whole-file backup_database/daily_backup safety net."""
    __tablename__ = "user_backups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)
    label = Column(String, nullable=True)
    payload_json = Column(EncryptedString, nullable=False)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    print("Database initialized.")


def migrate_db():
    """Migrate database to add new columns."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('expenses')]

    with engine.connect() as conn:
        if 'expense_type' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN expense_type TEXT DEFAULT "payment"'))
            print("Added expense_type column")

        if 'period_days' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN period_days INTEGER'))
            print("Added period_days column")

        if 'reminder_time' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN reminder_time TEXT'))
            print("Added reminder_time column")

        if 'currency' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN currency TEXT DEFAULT "RUB"'))
            print("Added currency column")

        if 'next_reminder_at' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN next_reminder_at DATETIME'))
            print("Added next_reminder_at column")

        if 'reminder_stage' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN reminder_stage TEXT'))
            print("Added reminder_stage column")

        if 'user_seq' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN user_seq INTEGER'))
            print("Added user_seq column")

        if 'deactivated_at' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN deactivated_at DATETIME'))
            # Existing soft-deleted rows predate this column, so the exact
            # deactivation time is unknown. Backfill them as already past the
            # undo window so the next purge clears accumulated cruft right
            # away, instead of keeping them forever (deactivated_at IS NULL
            # never matches purge_old_inactive_expenses' "< cutoff" filter).
            conn.execute(
                text("UPDATE expenses SET deactivated_at = :ts WHERE is_active = 0 AND deactivated_at IS NULL"),
                {"ts": utcnow_naive() - timedelta(days=config.UNDO_WINDOW_DAYS + 1)},
            )
            print("Added deactivated_at column")

        # Reminder plan v3 (NULL plan = v2 default preset, no data migration
        # or backfill needed: the fire path derives the active slot from the
        # plan + today's date, and legacy rows keep their v2 reminder_stage
        # for batch priority until the next recompute touches them)
        if 'reminder_plan' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN reminder_plan TEXT'))
            print("Added reminder_plan column")
        if 'reminder_slot' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN reminder_slot TEXT'))
            print("Added reminder_slot column")
        if 'reminder_slot_sends' not in columns:
            conn.execute(text('ALTER TABLE expenses ADD COLUMN reminder_slot_sends INTEGER NOT NULL DEFAULT 0'))
            print("Added reminder_slot_sends column")
        # Recurrence anchor (see RECUR_ANCHOR_*): DEFAULT fills existing rows
        # with the "scheduled" mode too, so no separate backfill is needed —
        # every prior record now advances on the calendar-fixed grid.
        if 'recur_anchor' not in columns:
            conn.execute(text(
                f"ALTER TABLE expenses ADD COLUMN recur_anchor TEXT DEFAULT '{RECUR_ANCHOR_SCHEDULED}'"
            ))
            print("Added recur_anchor column")

        # Fix existing tasks that may have NULL amount
        conn.execute(text("UPDATE expenses SET amount = 0 WHERE expense_type = 'task' AND amount IS NULL"))

        conn.commit()

    _backfill_user_seq()
    _migrate_users_default_currency()
    _migrate_users_list_sort()
    _migrate_users_reminder_hour()
    _migrate_users_quiet_hours()
    _migrate_users_ai_mode()
    _migrate_legacy_snoozes_to_due_dates(drop_column=True)
    _encrypt_legacy_plaintext_fields()

    # Indexes for reminder scheduler v2
    with engine.connect() as conn:
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_expenses_reminder_due '
            'ON expenses (is_active, next_reminder_at)'
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_reminder_logs_expense_stage '
            'ON reminder_logs (expense_id, stage, sent_at)'
        ))
        conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_user_seq '
            'ON expenses (user_id, user_seq)'
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_user_backups_user_created '
            'ON user_backups (user_id, created_at)'
        ))
        conn.commit()
        print("Reminder indexes ensured")

    # Re-inspect for reminder_logs (after possible previous changes)
    inspector = inspect(engine)
    reminder_columns = [col['name'] for col in inspector.get_columns('reminder_logs')]
    with engine.connect() as conn:
        if 'message_id' not in reminder_columns:
            conn.execute(text('ALTER TABLE reminder_logs ADD COLUMN message_id INTEGER'))
            print("Added message_id column to reminder_logs")
        conn.commit()

    print("Migration completed.")


def _encrypt_legacy_plaintext_fields():
    """One-time (idempotent) upgrade: encrypt any expenses.title/amount/
    currency, user_backups.payload_json, ai_sessions.messages_json rows still
    stored as plaintext from before field-level encryption was introduced.

    Runs on every startup, like the other migrations here, but is a no-op
    once everything is encrypted — each row is checked by attempting a
    decrypt; success means it's already ciphertext (skip), failure means it's
    legacy plaintext (encrypt now). Uses raw SQL, not the ORM: the ORM's
    EncryptedString/EncryptedFloat column types assume every stored value is
    already ciphertext and would raise trying to "decrypt" a legacy row.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, title, amount, currency FROM expenses")).fetchall()
        for row_id, title, amount, currency in rows:
            new_title = title
            new_amount = amount
            new_currency = currency
            changed = False

            if title is not None and crypto_utils.try_decrypt_str(str(title)) is None:
                new_title = crypto_utils.encrypt_str(str(title))
                changed = True

            if amount is not None and crypto_utils.try_decrypt_str(str(amount)) is None:
                new_amount = crypto_utils.encrypt_str(repr(float(amount)))
                changed = True

            if currency is not None and crypto_utils.try_decrypt_str(str(currency)) is None:
                new_currency = crypto_utils.encrypt_str(str(currency))
                changed = True

            if changed:
                conn.execute(
                    text("UPDATE expenses SET title=:t, amount=:a, currency=:c WHERE id=:id"),
                    {"t": new_title, "a": new_amount, "c": new_currency, "id": row_id},
                )

        backup_rows = conn.execute(text("SELECT id, payload_json FROM user_backups")).fetchall()
        for row_id, payload_json in backup_rows:
            if payload_json is not None and crypto_utils.try_decrypt_str(payload_json) is None:
                conn.execute(
                    text("UPDATE user_backups SET payload_json=:p WHERE id=:id"),
                    {"p": crypto_utils.encrypt_str(payload_json), "id": row_id},
                )

        session_rows = conn.execute(text("SELECT user_id, messages_json FROM ai_sessions")).fetchall()
        for user_id, messages_json in session_rows:
            if messages_json is not None and crypto_utils.try_decrypt_str(messages_json) is None:
                conn.execute(
                    text("UPDATE ai_sessions SET messages_json=:m WHERE user_id=:uid"),
                    {"m": crypto_utils.encrypt_str(messages_json), "uid": user_id},
                )

        conn.commit()


def _migrate_users_default_currency():
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = [col["name"] for col in inspector.get_columns("users")]
    if "default_currency" in columns:
        return

    with engine.connect() as conn:
        conn.execute(text('ALTER TABLE users ADD COLUMN default_currency TEXT DEFAULT "RUB"'))
        conn.commit()
        print("Added default_currency column to users")


def _migrate_users_list_sort():
    """Add users.list_sort (default 'date' = soonest-due first within payments/tasks)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = [col["name"] for col in inspector.get_columns("users")]
    if "list_sort" in columns:
        return

    with engine.connect() as conn:
        conn.execute(text(
            f'ALTER TABLE users ADD COLUMN list_sort TEXT DEFAULT "{DEFAULT_LIST_SORT}"'
        ))
        conn.commit()
        print("Added list_sort column to users")


def _migrate_users_reminder_hour():
    """Add users.reminder_hour (default local hour reminders fire — see
    config.DEFAULT_FIRE_HOUR)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = [col["name"] for col in inspector.get_columns("users")]
    if "reminder_hour" in columns:
        return
    with engine.connect() as conn:
        conn.execute(text(
            f'ALTER TABLE users ADD COLUMN reminder_hour INTEGER DEFAULT {config.DEFAULT_FIRE_HOUR}'
        ))
        conn.commit()
        print("Added reminder_hour column to users")


def _migrate_users_quiet_hours():
    """Add users.quiet_hours (default on — suppress no-explicit-time reminders
    at night, config.QUIET_*)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = [col["name"] for col in inspector.get_columns("users")]
    if "quiet_hours" in columns:
        return
    with engine.connect() as conn:
        conn.execute(text('ALTER TABLE users ADD COLUMN quiet_hours BOOLEAN DEFAULT 1'))
        conn.commit()
        print("Added quiet_hours column to users")


def _migrate_users_ai_mode():
    """Add users.ai_mode (default off — free-text AI only after /ai toggle ON)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = [col["name"] for col in inspector.get_columns("users")]
    if "ai_mode" in columns:
        return
    with engine.connect() as conn:
        conn.execute(text('ALTER TABLE users ADD COLUMN ai_mode BOOLEAN DEFAULT 0'))
        conn.commit()
        print("Added ai_mode column to users")


def _legacy_snooze_local(value, user_tz: str) -> datetime:
    """Convert the retired naive-UTC snooze value to a local due datetime."""
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(user_tz or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return value.astimezone(zone)


def _predue_slot_consumed(plan_json: str | None, due_date: date, user_tz: str) -> tuple[str | None, int]:
    """A manual move acknowledges a pre-due wave occurring on the move day."""
    from reminder_plan import plan_from_json, slot_for_day, slot_times

    try:
        today = datetime.now(ZoneInfo(user_tz or DEFAULT_TIMEZONE)).date()
    except ZoneInfoNotFoundError:
        today = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()
    active = slot_for_day(plan_from_json(plan_json), (due_date - today).days)
    if active and active[0].startswith("n"):
        return active[0], int(slot_times(active[1]))
    return None, 0


def _stage_for_days_until(days_until: int) -> str | None:
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


def _migrate_legacy_snoozes_to_due_dates(*, drop_column: bool) -> int:
    """Fold retired snooze state into the record's real due date/time.

    ``drop_column=False`` supports the safe first deploy: convert data while
    retaining schema compatibility with its old-code rollback. Once that
    version is healthy, ``drop_column=True`` removes the empty column too.
    """
    from sqlalchemy import inspect, text

    columns = [col["name"] for col in inspect(engine).get_columns("expenses")]
    if "snoozed_until" not in columns:
        return 0

    converted = 0
    with engine.begin() as conn:
        user_tzs = {
            row.id: (row.timezone or DEFAULT_TIMEZONE)
            for row in conn.execute(text("SELECT id, timezone FROM users"))
        }
        rows = conn.execute(text(
            "SELECT id, user_id, is_active, reminder_plan, snoozed_until "
            "FROM expenses WHERE snoozed_until IS NOT NULL"
        )).all()
        for row in rows:
            if not row.is_active:
                conn.execute(
                    text("UPDATE expenses SET snoozed_until = NULL WHERE id = :id"),
                    {"id": row.id},
                )
                continue
            tz = user_tzs.get(row.user_id, DEFAULT_TIMEZONE)
            due_local = _legacy_snooze_local(row.snoozed_until, tz)
            due_date = due_local.date()
            slot, sends = _predue_slot_consumed(row.reminder_plan, due_date, tz)
            try:
                today = datetime.now(ZoneInfo(tz)).date()
            except ZoneInfoNotFoundError:
                today = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()
            conn.execute(text(
                "UPDATE expenses SET next_payment_date = :due_date, reminder_time = :due_time, "
                "next_reminder_at = :next_at, reminder_stage = :stage, "
                "reminder_slot = :slot, reminder_slot_sends = :sends, snoozed_until = NULL "
                "WHERE id = :id"
            ), {
                "due_date": due_date,
                "due_time": due_local.strftime("%H:%M"),
                "next_at": due_local.astimezone(timezone.utc).replace(tzinfo=None),
                "stage": _stage_for_days_until((due_date - today).days),
                "slot": slot,
                "sends": sends,
                "id": row.id,
            })
            converted += 1

        if drop_column:
            conn.execute(text("ALTER TABLE expenses DROP COLUMN snoozed_until"))
            print("Dropped retired snoozed_until column")

    if converted:
        print(f"Converted {converted} legacy snooze(s) to due dates")
    return converted


def get_user_quiet_hours(db, user_id: int) -> bool:
    """Whether night-time quiet hours are enabled for this user (default True)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.quiet_hours is None:
        return True
    return bool(user.quiet_hours)


def update_user_quiet_hours(db, user_id: int, enabled: bool) -> int:
    """Toggle quiet hours and recompute the user's reminders so scheduled
    fires shift out of / back into the night. Returns expenses recomputed."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")
    user.quiet_hours = bool(enabled)
    db.flush()
    from reminder_engine import recompute_for_user
    count = recompute_for_user(db, user_id)
    logger.info("User %s quiet_hours -> %s, %d expenses recomputed", user_id, enabled, count)
    return count


def get_user_ai_mode(db, user_id: int) -> bool:
    """Permanent free-text AI assistant flag (default False = slash-only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.ai_mode is None:
        return False
    return bool(user.ai_mode)


def update_user_ai_mode(db, user_id: int, enabled: bool) -> bool:
    """Set permanent AI mode. Returns the new state. Raises if user missing."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")
    user.ai_mode = bool(enabled)
    db.flush()
    logger.info("User %s ai_mode -> %s", user_id, user.ai_mode)
    return bool(user.ai_mode)


def toggle_user_ai_mode(db, user_id: int) -> bool:
    """Flip permanent AI mode. Returns the new state."""
    return update_user_ai_mode(db, user_id, not get_user_ai_mode(db, user_id))


def get_user_reminder_hour(db, user_id: int) -> int:
    """Local hour (0..23) the user's reminders fire when the slot has no
    explicit time. Falls back to config.DEFAULT_FIRE_HOUR for missing user or
    out-of-range value."""
    user = db.query(User).filter(User.id == user_id).first()
    hour = user.reminder_hour if user else None
    if hour is None or not (0 <= hour <= 23):
        return config.DEFAULT_FIRE_HOUR
    return hour


def update_user_reminder_hour(db, user_id: int, hour: int) -> tuple[int, int]:
    """Set reminder_hour and recompute the user's reminders so scheduled fires
    shift to the new hour. Returns (old_hour, expenses_recomputed). Raises
    ValueError on bad input/missing user."""
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        raise ValueError(f"Hour out of range 0-23: {hour}")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")
    old = get_user_reminder_hour(db, user_id)
    user.reminder_hour = hour
    db.flush()
    from reminder_engine import recompute_for_user
    count = recompute_for_user(db, user_id)
    logger.info("User %s reminder_hour %s -> %s, %d expenses recomputed", user_id, old, hour, count)
    return old, count


def _backfill_user_seq():
    """Assign per-user sequential IDs (1,2,3…) ordered by global id, for any
    row that doesn't have one yet.

    Runs on every process start (migrate_db() is called from every deploy AND
    every crash-restart under systemd Restart=always). The old version
    unconditionally re-scanned and re-numbered EVERY expense row every single
    time regardless of whether anything needed fixing — at 10k users x tens
    of expenses each (hundreds of thousands of rows), that's hundreds of
    thousands of individual UPDATE statements on every single restart, not
    just the one-time migration it was meant to be — a data-volume-triggered
    crash-loop risk if it ever runs long enough to hit systemd's start
    timeout. Now: skip entirely once nothing has user_seq=NULL, and assign
    to what's left in one batched statement instead of a per-row Python loop.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('expenses')]
    if 'user_seq' not in columns:
        return

    with engine.connect() as conn:
        need = conn.execute(text('SELECT COUNT(*) FROM expenses WHERE user_seq IS NULL')).scalar()
        if not need:
            return

        conn.execute(text("""
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn
                FROM expenses
            )
            UPDATE expenses SET user_seq = (SELECT rn FROM ranked WHERE ranked.id = expenses.id)
            WHERE user_seq IS NULL
        """))
        conn.commit()
        print(f"Backfilled user_seq for {need} expense row(s)")


def next_user_seq(db, user_id: int) -> int:
    """Next per-user expense number (1-based, gaps allowed after delete)."""
    current = db.query(func.coalesce(func.max(Expense.user_seq), 0)).filter(
        Expense.user_id == user_id,
    ).scalar()
    return int(current) + 1


def add_expense(db, *, user_id: int, **fields) -> Expense:
    """Create expense with auto-assigned per-user user_seq."""
    expense = Expense(user_id=user_id, user_seq=next_user_seq(db, user_id), **fields)
    db.add(expense)
    db.flush()
    return expense


def get_expense_by_user_seq(db, user_id: int, user_seq: int) -> Expense | None:
    """Resolve user-facing ID to expense (scoped to owner)."""
    return db.query(Expense).filter(
        Expense.user_id == user_id,
        Expense.user_seq == user_seq,
    ).first()


def get_user_list_sort(db, user_id: int) -> str:
    """Return user's /list sort mode: 'date' (default) or 'id'."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return DEFAULT_LIST_SORT
    mode = (user.list_sort or DEFAULT_LIST_SORT).lower()
    if mode not in LIST_SORT_MODES:
        logger.warning("Invalid list_sort %s for user %s, using %s", mode, user_id, DEFAULT_LIST_SORT)
        return DEFAULT_LIST_SORT
    return mode


def update_user_list_sort(db, user_id: int, mode: str) -> str:
    """Set list_sort; returns previous mode. Raises ValueError if invalid/missing user."""
    mode = (mode or "").lower()
    if mode not in LIST_SORT_MODES:
        raise ValueError(f"Invalid list_sort mode: {mode}")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")
    old = get_user_list_sort(db, user_id)
    user.list_sort = mode
    db.flush()
    return old


def get_user_default_currency(db, user_id: int) -> str:
    """Return user's preferred currency for defaults and totals."""
    from currency import SETTINGS_CURRENCIES

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.default_currency:
        return "RUB"
    cur = user.default_currency.upper()
    if cur in SETTINGS_CURRENCIES:
        return cur
    logger.warning("Invalid default_currency %s for user %s, using RUB", cur, user_id)
    return "RUB"


def convert_user_payments_to_currency(db, user_id: int, target_currency: str, rates: dict) -> int:
    """Convert all active payment amounts to target currency via USD cross-rate."""
    from currency import convert_amount

    target_currency = target_currency.upper()
    expenses = db.query(Expense).filter(
        Expense.user_id == user_id,
        Expense.is_active == True,
        Expense.expense_type == "payment",
    ).all()

    updated = 0
    for expense in expenses:
        if expense.amount is None:
            continue
        old_currency = (expense.currency or target_currency).upper()
        if old_currency != target_currency:
            expense.amount = round(convert_amount(expense.amount, old_currency, target_currency, rates), 2)
        expense.currency = target_currency
        updated += 1
    return updated


def update_user_default_currency(db, user_id: int, new_currency: str, rates: dict) -> tuple[str, int]:
    """Save local currency and convert all active payments via USD cross-rate."""
    from currency import SETTINGS_CURRENCIES

    new_currency = new_currency.upper()
    if new_currency not in SETTINGS_CURRENCIES:
        raise ValueError(f"Unsupported currency: {new_currency}")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")

    old_currency = get_user_default_currency(db, user_id)
    if old_currency == new_currency:
        return old_currency, 0

    updated = convert_user_payments_to_currency(db, user_id, new_currency, rates)
    user.default_currency = new_currency
    logger.info(
        "User %s default currency changed %s -> %s, %d payments converted",
        user_id, old_currency, new_currency, updated,
    )
    return old_currency, updated


def snapshot_user_data(db, user_id: int, label: str | None = None, protect_id: int | None = None) -> int:
    """Save a lightweight JSON snapshot of one user's own rows (settings +
    ALL expenses, active and inactive, since a rollback must be able to undo
    deletions and completions too). Scoped to this user only — unlike
    backup_database, this does not touch any other user's data.

    Prunes old snapshots for this user beyond config.MAX_USER_BACKUPS_PER_USER
    OR older than config.UNDO_WINDOW_DAYS, whichever is more restrictive — a
    user who rarely uses /ai keeps a week of history capped at the count
    limit; a heavy /ai user never keeps more than a week regardless of count.
    If protect_id is given, that backup row is kept even if it would
    otherwise fall outside the retention window (e.g. it is the specific
    backup a restore is about to look up — pruning must never delete out
    from under it).
    Returns the id of the newly created UserBackup row.
    """
    user = db.query(User).filter(User.id == user_id).first()
    expenses = db.query(Expense).filter(Expense.user_id == user_id).all()

    payload = {
        "user": (
            {
                "timezone": user.timezone,
                "default_currency": user.default_currency,
                "list_sort": user.list_sort or DEFAULT_LIST_SORT,
                "reminder_hour": user.reminder_hour,
                "quiet_hours": user.quiet_hours,
                "ai_mode": bool(user.ai_mode) if user.ai_mode is not None else False,
            }
            if user else None
        ),
        "expenses": [
            {
                "user_seq": exp.user_seq,
                "expense_type": exp.expense_type,
                "title": exp.title,
                "amount": exp.amount,
                "currency": exp.currency,
                "next_payment_date": exp.next_payment_date.isoformat() if exp.next_payment_date else None,
                "reminder_time": exp.reminder_time,
                "period": exp.period,
                "period_days": exp.period_days,
                "recur_anchor": exp.recur_anchor,
                "is_active": exp.is_active,
                "created_at": exp.created_at.isoformat() if exp.created_at else None,
                "next_reminder_at": exp.next_reminder_at.isoformat() if exp.next_reminder_at else None,
                "reminder_stage": exp.reminder_stage,
                "reminder_plan": exp.reminder_plan,
                "reminder_slot": exp.reminder_slot,
                "reminder_slot_sends": exp.reminder_slot_sends or 0,
                "deactivated_at": exp.deactivated_at.isoformat() if exp.deactivated_at else None,
            }
            for exp in expenses
        ],
    }

    backup = UserBackup(
        user_id=user_id,
        label=label,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(backup)
    db.flush()
    new_id = backup.id

    rows = (
        db.query(UserBackup.id, UserBackup.created_at)
        .filter(UserBackup.user_id == user_id)
        .order_by(UserBackup.created_at.desc())
        .all()
    )
    cutoff = utcnow_naive() - timedelta(days=config.UNDO_WINDOW_DAYS)
    stale_ids = [
        row.id for i, row in enumerate(rows)
        if row.id != protect_id and (i >= config.MAX_USER_BACKUPS_PER_USER or row.created_at < cutoff)
    ]
    if stale_ids:
        db.query(UserBackup).filter(UserBackup.id.in_(stale_ids)).delete(synchronize_session=False)

    return new_id


def purge_old_inactive_expenses(max_age_days: int = config.UNDO_WINDOW_DAYS) -> int:
    """Hard-delete expenses that have been soft-deleted (is_active=False) for
    more than max_age_days, plus their reminder logs. Without this, every
    user_backups snapshot re-embeds a user's ENTIRE deletion history forever
    (snapshot_user_data includes all expenses, active and inactive, so a
    rollback can undo deletions/completions too). Hard-delete inactive rows
    outside the configured undo window so snapshots cannot grow forever.
    Standalone (opens its own session) to match the scheduler job pattern in
    tz_rollback.cleanup_old_tz_rollback_snapshots. Returns count removed.
    """
    cutoff = utcnow_naive() - timedelta(days=max_age_days)
    with get_db() as db:
        stale_ids = [
            row[0] for row in
            db.query(Expense.id)
            .filter(Expense.is_active == False, Expense.deactivated_at < cutoff)
            .all()
        ]
        if not stale_ids:
            return 0
        db.query(ReminderLog).filter(ReminderLog.expense_id.in_(stale_ids)).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.id.in_(stale_ids)).delete(synchronize_session=False)

    logger.info("Purged %d expense(s) soft-deleted more than %d days ago", len(stale_ids), max_age_days)
    return len(stale_ids)


def list_user_backups(db, user_id: int, limit: int = 20) -> list[dict]:
    """List this user's snapshots, newest first, for the AI/user to review."""
    rows = (
        db.query(UserBackup)
        .filter(UserBackup.user_id == user_id)
        .order_by(UserBackup.created_at.desc())
        .limit(limit)
        .all()
    )

    result = []
    for row in rows:
        try:
            item_count = len(json.loads(row.payload_json).get("expenses", []))
        except Exception:
            item_count = 0
        result.append({
            "id": row.id,
            "created_at": row.created_at.strftime("%d.%m.%Y %H:%M") if row.created_at else "",
            "label": row.label,
            "item_count": item_count,
        })
    return result


def restore_user_backup(db, user_id: int, backup_id: int) -> dict | None:
    """Restore this user's expenses and settings from a previously saved
    snapshot. Ownership is enforced in the same filter as the id lookup so a
    missing id and someone else's id are indistinguishable from the outside.
    Returns None if no such backup belongs to this user.
    """
    backup = db.query(UserBackup).filter(
        UserBackup.id == backup_id,
        UserBackup.user_id == user_id,
    ).first()
    if not backup:
        return None

    payload = json.loads(backup.payload_json)

    current_ids = [
        row[0] for row in
        db.query(Expense.id).filter(Expense.user_id == user_id).all()
    ]
    if current_ids:
        # synchronize_session='fetch' (not False) — the expenses being deleted
        # here were likely loaded as ORM objects earlier in this same session
        # (e.g. by add_expense elsewhere in the request), so a plain
        # synchronize_session=False delete leaves them stale in the identity
        # map; when SQLite then reuses one of their freed autoincrement ids for
        # a recreated row below, SQLAlchemy raises a spurious SAWarning
        # ("identity map already had an identity ... replacing it"). 'fetch'
        # properly expunges the matching objects from the identity map first.
        db.query(ReminderLog).filter(ReminderLog.expense_id.in_(current_ids)).delete(synchronize_session='fetch')
        db.query(Expense).filter(Expense.id.in_(current_ids)).delete(synchronize_session='fetch')

    user_payload = payload.get("user") or {}
    existing_user = db.query(User).filter(User.id == user_id).first()
    restore_tz = user_payload.get("timezone") or (
        existing_user.timezone if existing_user else DEFAULT_TIMEZONE
    )

    for entry in payload.get("expenses", []):
        next_payment_date = (
            date.fromisoformat(entry["next_payment_date"])
            if entry.get("next_payment_date") else None
        )
        reminder_time = entry.get("reminder_time")
        next_reminder_at = (
            datetime.fromisoformat(entry["next_reminder_at"])
            if entry.get("next_reminder_at") else None
        )
        reminder_slot = entry.get("reminder_slot")
        reminder_slot_sends = entry.get("reminder_slot_sends") or 0

        # Backups made before snooze was retired are upgraded on restore:
        # their deferred moment becomes the record's actual due date/time.
        if entry.get("snoozed_until"):
            legacy_utc = datetime.fromisoformat(entry["snoozed_until"])
            due_local = _legacy_snooze_local(legacy_utc, restore_tz)
            next_payment_date = due_local.date()
            reminder_time = due_local.strftime("%H:%M")
            next_reminder_at = legacy_utc
            reminder_slot, reminder_slot_sends = _predue_slot_consumed(
                entry.get("reminder_plan"), next_payment_date, restore_tz,
            )

        new_expense = Expense(
            user_id=user_id,
            user_seq=entry.get("user_seq"),
            expense_type=entry.get("expense_type"),
            title=entry.get("title"),
            amount=entry.get("amount"),
            currency=entry.get("currency"),
            next_payment_date=next_payment_date,
            reminder_time=reminder_time,
            period=entry.get("period"),
            period_days=entry.get("period_days"),
            recur_anchor=normalize_recur_anchor(entry.get("recur_anchor")),
            is_active=entry.get("is_active"),
            created_at=datetime.fromisoformat(entry["created_at"]) if entry.get("created_at") else utcnow_naive(),
            next_reminder_at=next_reminder_at,
            reminder_stage=entry.get("reminder_stage"),
            reminder_plan=entry.get("reminder_plan"),
            reminder_slot=reminder_slot,
            reminder_slot_sends=reminder_slot_sends,
            deactivated_at=datetime.fromisoformat(entry["deactivated_at"]) if entry.get("deactivated_at") else None,
        )
        db.add(new_expense)

    if (
        user_payload.get("timezone")
        or user_payload.get("default_currency")
        or user_payload.get("list_sort")
    ):
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            if user_payload.get("timezone"):
                user.timezone = user_payload["timezone"]
            if user_payload.get("default_currency"):
                user.default_currency = user_payload["default_currency"]
            if user_payload.get("list_sort") in LIST_SORT_MODES:
                user.list_sort = user_payload["list_sort"]
            rh = user_payload.get("reminder_hour")
            if isinstance(rh, int) and 0 <= rh <= 23:
                user.reminder_hour = rh
            if "quiet_hours" in user_payload and user_payload["quiet_hours"] is not None:
                user.quiet_hours = bool(user_payload["quiet_hours"])
            if "ai_mode" in user_payload and user_payload["ai_mode"] is not None:
                user.ai_mode = bool(user_payload["ai_mode"])

    db.flush()

    from reminder_engine import recompute_for_user
    recompute_for_user(db, user_id)

    return {
        "created_at": backup.created_at.strftime("%d.%m.%Y %H:%M") if backup.created_at else "",
        "item_count": len(payload.get("expenses", [])),
    }


def get_user_timezone(db, user_id: int) -> str:
    """Return user's IANA timezone or default."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.timezone:
        return DEFAULT_TIMEZONE
    tz = user.timezone
    try:
        ZoneInfo(tz)
        return tz
    except ZoneInfoNotFoundError:
        logger.warning("Invalid timezone %s for user %s, using %s", tz, user_id, DEFAULT_TIMEZONE)
        return DEFAULT_TIMEZONE


def recalculate_expense_dates_for_tz_change(db, user_id: int, old_tz: str, new_tz: str) -> int:
    """Shift next_payment_date to preserve days_until when timezone changes."""
    from utils import today_in_tz

    old_today = today_in_tz(old_tz)
    new_today = today_in_tz(new_tz)
    expenses = db.query(Expense).filter(
        Expense.user_id == user_id,
        Expense.is_active == True,
    ).all()

    updated = 0
    for expense in expenses:
        days_until = (expense.next_payment_date - old_today).days
        expense.next_payment_date = new_today + timedelta(days=days_until)
        updated += 1
    return updated


def update_user_timezone(db, user_id: int, new_tz: str) -> tuple[str, int]:
    """Validate, save timezone, and recalculate active expense dates."""
    from utils import is_valid_timezone

    if not is_valid_timezone(new_tz):
        raise ValueError(f"Unknown time zone: {new_tz}")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")

    old_tz = user.timezone or DEFAULT_TIMEZONE
    if old_tz == new_tz:
        return old_tz, 0

    updated = recalculate_expense_dates_for_tz_change(db, user_id, old_tz, new_tz)
    user.timezone = new_tz
    from reminder_engine import recompute_for_user
    recompute_for_user(db, user_id)
    logger.info(
        "User %s timezone changed %s -> %s, %d expenses recalculated",
        user_id, old_tz, new_tz, updated,
    )
    return old_tz, updated
