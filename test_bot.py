#!/usr/bin/env python3
"""
Test script for the SpendAlert bot.
Run this to verify database and basic functionality without Telegram.
"""

import contextlib
import io
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Keep the standalone test runner usable in Windows terminals whose legacy
# code page cannot encode every symbol used in assertions and summaries.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="backslashreplace")

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point at a throwaway file DB BEFORE importing `database` (which binds its
# engine at import time from config.DATABASE_URL). Without this, running
# this script writes fixture rows straight into the real local
# spendalert.db — or, if ever pointed at a copy of the live database,
# litters real user/expense tables and competes for SQLite file locks with
# real traffic. A tempfile (not sqlite:///:memory:) because :memory: isn't
# shared across the separate SessionLocal()/get_db() connections this file
# opens throughout the tests — each would see its own empty database.
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db", prefix="spendalert_test_")
os.close(_TEST_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"

# Field-level encryption (crypto_utils.py) needs a key before database.py's
# models can be imported; a throwaway one is fine since this is a throwaway
# test DB.
if not os.environ.get("DB_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["DB_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from database import (
    init_db, engine, SessionLocal, User, Expense, update_user_timezone, get_user_default_currency, update_user_default_currency,
    get_user_list_sort, update_user_list_sort,
    get_user_reminder_hour, update_user_reminder_hour,
    get_user_quiet_hours, update_user_quiet_hours,
    get_user_ai_mode, update_user_ai_mode, toggle_user_ai_mode,
    LIST_SORT_DATE, LIST_SORT_ID, DEFAULT_LIST_SORT,
    RECUR_ANCHOR_SCHEDULED, RECUR_ANCHOR_ACTUAL, DEFAULT_RECUR_ANCHOR,
    normalize_recur_anchor, apply_recur_anchor,
    add_expense, get_expense_by_user_seq, migrate_db,
    UserBackup, snapshot_user_data, list_user_backups, restore_user_backup,
    purge_old_inactive_expenses, utcnow_naive, _migrate_legacy_snoozes_to_due_dates,
)
from currency import convert_amount, currency_symbol, SETTINGS_CURRENCIES
from bot import (
    detect_currency, _add_ics_events_sync, _sort_expenses_for_list,
    _load_plan_for_edit_sync, _update_reminder_plan_sync,
    _mark_as_paid_sync, _reschedule_from_reminder_sync,
    should_route_free_text_to_ai, _get_ai_mode_sync, _toggle_ai_mode_sync,
    free_text_ai_handler, HELP_TEXT, AI_MODE_ON_TEXT, AI_MODE_OFF_TEXT, BOT_COMMANDS,
)
import ics_import
from dateutil.relativedelta import relativedelta
from utils import (
    search_timezones, parse_date, today_in_tz, clear_tz_cache,
    is_valid_timezone, POPULAR_TIMEZONES, compute_next_due_date,
    build_ai_user_message, effective_conversation_text, has_telegram_message_context,
    message_body_text, format_forward_origin, to_user_tz,
)
from ai_handler import (
    _model_content, _last_tool_user_message, _format_ai_display_text,
    _aggregate_modify_tool_results,
    _ACTION_INTENT_RE, execute_create_task, execute_function,
    transcribe_audio, voice_audio_format, MAX_VOICE_FILE_BYTES,
    OPENROUTER_TRANSCRIBE_URL, OPENROUTER_STT_MODEL,
    OPENROUTER_STT_TIMEOUT_SECONDS, REPLY_LANGUAGE_CONTEXT,
)
import config
from tz_rollback import save_tz_change_snapshot, restore_tz_change, has_tz_rollback, get_tz_rollback_info
from reminder_engine import (
    stage_from_days_until,
    compute_plan_state,
    apply_reminder_state,
    backfill_all,
    to_utc_naive,
    local_datetime_on_date,
    recompute_after_mutation,
    d_day_send_allowed,
    record_reminder_sent,
    recompute_stages_for_window,
    slot_due_for_send,
    OVERDUE_BACKOFF_DAYS,
)
from sqlalchemy import inspect, text
import reminder_plan as rp
import scheduler as scheduler_module
from scheduler import _list_immediate_send_allowed, _reminder_reply_markup
from database import ReminderLog
from utils import now_in_tz
from zoneinfo import ZoneInfo


def _tz_for_local_hour(target_hour: int = 12) -> str:
    """Fixed-offset IANA zone whose current local hour ≈ target_hour, so
    date-boundary math in the reminder tests never flakes around midnight."""
    utc_now = datetime.now(timezone.utc)
    shift = (target_hour - utc_now.hour) % 24
    if shift > 12:
        shift -= 24
    if shift == 0:
        return "UTC"
    # POSIX Etc/GMT signs are inverted: Etc/GMT-3 == UTC+3
    return f"Etc/GMT{'-' if shift > 0 else '+'}{abs(shift)}"


def test_database():
    """Test database creation and basic operations."""
    print("Testing database...")

    # Initialize database
    init_db()
    migrate_db()
    print("[OK] Database initialized")

    # Create session
    db = SessionLocal()

    try:
        # Test user creation
        test_user_id = 123456789
        user = db.query(User).filter(User.id == test_user_id).first()
        if not user:
            user = User(id=test_user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()
            print("[OK] Test user created")
        else:
            print("[OK] Test user already exists")

        # Test expense creation
        future_date = datetime.now().date() + timedelta(days=10)
        expense = add_expense(
            db,
            user_id=user.id,
            title="Test Internet",
            amount=750.0,
            next_payment_date=future_date,
            period="month",
        )
        db.commit()
        print(f"[OK] Test expense created (user_seq: {expense.user_seq})")

        # Test querying
        expenses = db.query(Expense).filter(Expense.user_id == user.id).all()
        print(f"[OK] Found {len(expenses)} expense(s) for user")

        # Test date calculation
        test_date = datetime.now().date()
        if expense.period == 'month':
            next_date = test_date + relativedelta(months=+1)
            print(f"[OK] Next payment date calculation works: {next_date}")

        # Cleanup
        db.delete(expense)
        db.commit()
        print("[OK] Test cleanup completed")

        print("\n[SUCCESS] All database tests passed!")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_scheduler_logic():
    """Test scheduler logic without actually scheduling."""
    print("\nTesting scheduler logic...")

    test_cases = [
        (5, None),  # 5 days - no reminder
        (3, "3_days"),  # 3 days - first reminder
        (2, "2_days"),  # 2 days - second stage
        (1, "1_day"),  # 1 day - third stage
        (0, "d_day"),  # Same day - fourth stage
        (-1, "overdue"),  # Overdue - final stage
    ]

    for days_until, expected_stage in test_cases:
        if days_until > 3:
            stage = None
        elif days_until == 3:
            stage = "3_days"
        elif days_until == 2:
            stage = "2_days"
        elif days_until == 1:
            stage = "1_day"
        elif days_until == 0:
            stage = "d_day"
        else:
            stage = "overdue"

        if stage == expected_stage:
            print(f"[OK] {days_until} days -> {stage}")
        else:
            print(f"[ERROR] {days_until} days -> {stage} (expected {expected_stage})")

    print("\n[OK] Scheduler logic tests completed!")


def test_timezone_utils():
    """Test per-user timezone helpers."""
    print("\nTesting timezone utils...")
    clear_tz_cache()

    moscow_hits = search_timezones("moscow")
    assert "Europe/Moscow" in moscow_hits, moscow_hits
    print("[OK] search_timezones('moscow') -> Europe/Moscow")

    ny_hits = search_timezones("new york")
    assert "America/New_York" in ny_hits, ny_hits
    print("[OK] search_timezones('new york') -> America/New_York")

    assert is_valid_timezone("Europe/Moscow"), "Europe/Moscow should be valid"
    print("[OK] is_valid_timezone('Europe/Moscow')")

    tz = "Europe/Moscow"
    today_api = today_in_tz(tz)
    parsed, _ = parse_date("today", tz)
    assert parsed == today_api, (parsed, today_api)
    print("[OK] parse_date('today') matches today_in_tz")

    for ptz in POPULAR_TIMEZONES[:3]:
        assert is_valid_timezone(ptz), ptz
    print("[OK] Popular timezones are valid")

    assert is_valid_timezone("America/Los_Angeles"), "America/Los_Angeles should be valid via ZoneInfo"
    print("[OK] is_valid_timezone('America/Los_Angeles')")


def test_timezone_recalculation():
    """Test expense date shift when timezone changes."""
    print("\nTesting timezone recalculation...")
    db = SessionLocal()
    try:
        user_id = 987654321
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()

        old_tz = user.timezone
        future = today_in_tz(old_tz) + timedelta(days=5)
        expense = add_expense(
            db,
            user_id=user_id,
            title="TZ Test",
            amount=100.0,
            next_payment_date=future,
            period="none",
        )
        db.commit()

        days_before = (expense.next_payment_date - today_in_tz(old_tz)).days
        update_user_timezone(db, user_id, "UTC")
        db.commit()
        db.refresh(expense)
        days_after = (expense.next_payment_date - today_in_tz("UTC")).days
        assert days_before == days_after, (days_before, days_after)
        print(f"[OK] days_until preserved: {days_before}d")

        db.delete(expense)
        update_user_timezone(db, user_id, old_tz)
        db.commit()
        print("[OK] Timezone recalculation cleanup done")
    except Exception as e:
        print(f"[ERROR] Timezone recalculation test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_tz_rollback():
    """Test snapshot save/restore around timezone change."""
    print("\nTesting TZ rollback...")
    db = SessionLocal()
    try:
        user_id = 987654322
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()

        future = today_in_tz("Europe/Moscow") + timedelta(days=7)
        expense = add_expense(
            db,
            user_id=user_id,
            title="Rollback Test",
            amount=50.0,
            next_payment_date=future,
            period="none",
        )
        db.commit()
        original_date = expense.next_payment_date

        apply_reminder_state(expense, "Europe/Moscow", db)
        db.commit()
        db.refresh(expense)

        save_tz_change_snapshot(db, user_id, "test_backup.db")
        assert has_tz_rollback(user_id)
        info = get_tz_rollback_info(user_id)
        assert info["timezone"] == "Europe/Moscow"
        snap_exp = info["expenses"][0]
        assert "next_reminder_at" in snap_exp
        assert "reminder_stage" in snap_exp
        print("[OK] Snapshot saved with reminder fields")

        days_before = (original_date - today_in_tz("Europe/Moscow")).days
        update_user_timezone(db, user_id, "UTC")
        db.commit()
        db.refresh(expense)
        days_after = (expense.next_payment_date - today_in_tz("UTC")).days
        assert days_before == days_after, (days_before, days_after)

        result = restore_tz_change(db, user_id)
        db.commit()
        db.refresh(expense)
        assert expense.next_payment_date == original_date, (expense.next_payment_date, original_date)
        assert result["timezone"] == "Europe/Moscow"
        assert not has_tz_rollback(user_id)
        print("[OK] Rollback restored timezone and dates")

        db.delete(expense)
        db.commit()
    except Exception as e:
        print(f"[ERROR] TZ rollback test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_reminder_engine_stages():
    """Test stage mapping matches legacy scheduler."""
    print("\nTesting reminder_engine stages...")
    assert stage_from_days_until(5) is None
    assert stage_from_days_until(3) == "3_days"
    assert stage_from_days_until(0) == "d_day"
    assert stage_from_days_until(-2) == "overdue"
    print("[OK] stage_from_days_until")


def test_reminder_plan_model():
    """Plan model: defaults, JSON round-trip, validation bounds."""
    print("\nTesting reminder_plan model...")
    # NULL column ≡ default preset; default preset stored back as NULL
    assert rp.plan_from_json(None) == rp.default_plan()
    assert rp.plan_to_json(rp.default_plan()) is None
    print("[OK] NULL <-> default preset")

    plan = rp.default_plan()
    plan["f"] = {"on": False}
    js = rp.plan_to_json(plan)
    assert js is not None
    assert rp.plan_from_json(js)["f"] == {"on": False}
    print("[OK] custom plan JSON round-trip")

    assert rp.plan_from_json("{broken json") == rp.default_plan()
    print("[OK] malformed column value falls back to default")

    bad = rp.default_plan()
    bad["n"][1]["offset_days"] = 3  # duplicate of n0
    try:
        rp.normalize_plan(bad)
        print("[ERROR] duplicate offsets must be rejected")
    except rp.PlanError:
        print("[OK] duplicate offsets rejected")

    bad2 = rp.default_plan()
    bad2["n"][0]["offset_days"] = 999
    try:
        rp.normalize_plan(bad2)
        print("[ERROR] offset out of bounds must be rejected")
    except rp.PlanError:
        print("[OK] offset bounds enforced")

    bad3 = rp.default_plan()
    bad3["n"][0]["times"] = None  # infinity is not allowed for pre-due waves
    try:
        rp.normalize_plan(bad3)
        print("[ERROR] infinite times for n must be rejected")
    except rp.PlanError:
        print("[OK] infinite times rejected for n waves")

    p = rp.normalize_plan({
        "n": [
            {"on": True, "offset_days": 2, "times": 1, "every_hours": 24},
            {"on": True, "offset_days": 9, "times": 1, "every_hours": 24},
        ],
        "d": {"on": False},
        "f": {"on": False},
    })
    assert p["n"][0]["offset_days"] == 9 and p["n"][1]["offset_days"] == 2
    assert p["n"][2] == {"on": False} and p["n"][3] == {"on": False}
    print("[OK] n waves normalized: sorted by offset desc, padded to 4")


def test_reminder_plan_slots():
    """Slot selection per day (§6.2) and chronological episodes."""
    print("\nTesting reminder_plan slot math...")
    plan = rp.default_plan()
    assert rp.slot_for_day(plan, 5) is None
    assert rp.slot_for_day(plan, 3)[0] == "n0"
    assert rp.slot_for_day(plan, 2)[0] == "n1"
    assert rp.slot_for_day(plan, 1)[0] == "n2"
    assert rp.slot_for_day(plan, 0)[0] == "d"
    assert rp.slot_for_day(plan, -4)[0] == "f"
    day_only = rp.day_only_plan()
    assert rp.slot_for_day(day_only, 2) is None
    assert rp.slot_for_day(day_only, 0)[0] == "d"
    assert rp.slot_for_day(rp.all_off_plan(), 0) is None
    print("[OK] slot_for_day")

    eps = [(k, d) for k, _, d in rp.iter_episodes(plan, 10)]
    assert eps == [("n0", 7), ("n1", 8), ("n2", 9), ("d", 10), ("f", 11)], eps
    eps0 = [(k, d) for k, _, d in rp.iter_episodes(plan, 0)]
    assert eps0 == [("d", 0), ("f", 1)], eps0
    eps_od = [(k, d) for k, _, d in rp.iter_episodes(plan, -3)]
    assert eps_od == [("f", -2)], eps_od
    # waves further out than the due date are skipped entirely
    eps2 = [(k, d) for k, _, d in rp.iter_episodes(plan, 2)]
    assert eps2 == [("n1", 0), ("n2", 1), ("d", 2), ("f", 3)], eps2
    print("[OK] iter_episodes chronological + offset>days_until skipped")


def test_reminder_plan_ui():
    """Create-screen UI model: presets, tap cycles, §3.4 truth table, summary."""
    print("\nTesting reminder_plan UI mapping...")
    assert rp.plan_from_ui(rp.ui_default()) == rp.default_plan()
    assert rp.plan_from_ui(rp.ui_day_only()) == rp.day_only_plan()
    assert rp.plan_from_ui(rp.ui_all_off()) == rp.all_off_plan()
    print("[OK] presets map to canonical plans")

    ui = rp.ui_default()
    rp.cycle_ui(ui, "n_3"); assert ui["n_3"] == "2x"
    rp.cycle_ui(ui, "n_3"); assert ui["n_3"] == "off"
    rp.cycle_ui(ui, "n_3"); assert ui["n_3"] == "1x"
    rp.cycle_ui(ui, "d"); assert ui["d"] == "off"    # often → off
    rp.cycle_ui(ui, "d"); assert ui["d"] == "1x"     # off → 1x
    rp.cycle_ui(ui, "f"); assert ui["f"] == "off"    # often → off
    rp.cycle_ui(ui, "f"); assert ui["f"] == "daily"  # off → daily
    print("[OK] tap cycles (§3.3 hybrid)")

    ui2 = rp.ui_default()
    ui2["n_3"] = "2x"
    ui2["extra"] = {"offset": 14, "mode": "1x"}
    plan2 = rp.plan_from_ui(ui2)
    assert plan2["n"][0]["offset_days"] == 14  # extra wave sorted to front
    n3 = next(s for s in plan2["n"] if s.get("offset_days") == 3)
    assert n3["times"] == 2 and n3["every_hours"] == 12.0  # "2x" -> twice, ~half a day apart
    p3 = rp.plan_from_ui(rp.ui_day_only())
    assert p3["d"] == {"on": True, "times": 1, "every_hours": 24.0, "at_time": None}
    assert p3["f"] == {"on": True, "times": None, "every_hours": 24.0}
    print("[OK] §3.4 truth table (2x=2×@12h, d 1x, f daily)")

    s = rp.ui_summary(rp.ui_default())
    assert "3d before" in s and "due day (often)" in s and "overdue (often)" in s, s
    assert rp.ui_summary(rp.ui_all_off()) == "no reminders"
    ui3 = rp.ui_day_only()
    assert rp.ui_summary(ui3) == "due day · overdue (daily)", rp.ui_summary(ui3)
    print("[OK] human summary")


def test_plan_engine_custom_plan_db():
    """End-to-end: custom plan through the DB — precomputed far fire,
    episode handover on send, fire-path self-heal, snapshot/restore."""
    print("\nTesting custom plan end-to-end (DB)...")
    db = SessionLocal()
    try:
        user_id = 121212555
        tz = _tz_for_local_hour(12)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone=tz)
            db.add(user)
            db.commit()
        else:
            user.timezone = tz
            db.commit()

        today = today_in_tz(tz)
        H = config.DEFAULT_FIRE_HOUR  # waves with no explicit time fire at this local hour
        # "once, a week ahead; due day off; overdue once a day, 3 times"
        plan = {
            "n": [{"on": True, "offset_days": 7, "times": 1, "every_hours": 24}],
            "d": {"on": False},
            "f": {"on": True, "times": 3, "every_hours": 24},
        }
        js = rp.plan_to_json(plan)
        exp = add_expense(
            db, user_id=user_id, title="Custom plan", amount=1.0,
            next_payment_date=today + timedelta(days=30), period="month",
            is_active=True, reminder_plan=js,
        )
        db.commit()
        recompute_after_mutation(db, expense_id=exp.id)
        db.commit()
        db.refresh(exp)
        # slot stays NULL until a send actually happens (exhaustion memory)
        assert exp.reminder_slot is None, exp.reminder_slot
        expected = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=23), H))
        assert exp.next_reminder_at == expected, (exp.next_reminder_at, expected)
        print("[OK] n0=7d fire precomputed 23 days ahead at reminder hour")

        # quiet day: nothing to send now, schedule untouched
        slot = slot_due_for_send(exp, tz, db)
        assert slot is None
        db.commit()
        db.refresh(exp)
        assert exp.next_reminder_at == expected
        print("[OK] slot_due_for_send quiet day -> None, schedule stable")

        # send in n0 exhausts it (times=1) → schedule moves to f (d is off),
        # while slot/sends REMEMBER the exhausted episode
        record_reminder_sent(db, exp.id, "n0", 777, exp.next_reminder_at)
        db.commit()
        db.refresh(exp)
        assert exp.reminder_slot == "n0" and exp.reminder_slot_sends == 1, (
            exp.reminder_slot, exp.reminder_slot_sends)
        expected_f = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=31), H))
        assert exp.next_reminder_at == expected_f, (exp.next_reminder_at, expected_f)
        print("[OK] exhausted wave hands over to f (d off) at day-after-due")

        # REGRESSION (review finding): hourly sweeps must NOT resurrect the
        # exhausted episode — recomputes leave the sends memory alone
        for _ in range(3):
            apply_reminder_state(exp, tz, db)
        db.commit()
        db.refresh(exp)
        assert exp.reminder_slot == "n0" and exp.reminder_slot_sends == 1
        assert exp.next_reminder_at == expected_f, exp.next_reminder_at
        print("[OK] repeated recomputes don't resurrect an exhausted wave")

        # REGRESSION (§6.4): shifting the due date resets the episode memory
        exp.next_payment_date = exp.next_payment_date + timedelta(days=1)
        db.commit()
        db.refresh(exp)
        assert exp.reminder_slot is None and exp.reminder_slot_sends == 0
        recompute_after_mutation(db, expense_id=exp.id)
        db.commit()
        db.refresh(exp)
        expected2 = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=24), H))
        assert exp.next_reminder_at == expected2, (exp.next_reminder_at, expected2)
        print("[OK] due-date change resets episode memory (ORM listener)")

        # snapshot → wipe plan → restore brings the plan back
        backup_id = snapshot_user_data(db, user_id, label="plan_test")
        db.commit()
        exp.reminder_plan = None
        db.commit()
        assert restore_user_backup(db, user_id, backup_id) is not None
        db.commit()
        restored = db.query(Expense).filter(Expense.user_id == user_id).first()
        assert restored.reminder_plan == js, restored.reminder_plan
        print("[OK] reminder_plan survives user_backups snapshot/restore")

        # cleanup
        ids = [row[0] for row in db.query(Expense.id).filter(Expense.user_id == user_id).all()]
        if ids:
            db.query(ReminderLog).filter(ReminderLog.expense_id.in_(ids)).delete(synchronize_session=False)
            db.query(Expense).filter(Expense.id.in_(ids)).delete(synchronize_session=False)
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        print(f"[ERROR] custom plan end-to-end failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_reminder_engine_compute():
    """Plan engine core: default (NULL) plan reproduces v2 preset semantics."""
    print("\nTesting reminder_engine compute (plan v3)...")
    tz = _tz_for_local_hour(12)
    now_local = now_in_tz(tz)
    today = now_local.date()
    plan = rp.default_plan()

    H = config.DEFAULT_FIRE_HOUR  # waves with no explicit time fire at this local hour, not midnight
    # due in 10 days → first fire PRECOMPUTED at due-3d reminder-hour (n0), not NULL
    exp_far = Expense(
        user_id=1, title="Far", amount=1.0,
        next_payment_date=today + timedelta(days=10), is_active=True,
    )
    slot, sends, next_at = compute_plan_state(exp_far, plan, tz, now_local=now_local)
    assert slot == "n0" and sends == 0, (slot, sends)
    expected = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=7), H))
    assert next_at == expected, (next_at, expected)
    print("[OK] future first fire precomputed at due-3d reminder hour")

    # due day with reminder_time later today → fire at that time
    exp_dday = Expense(
        user_id=1, title="Today 14:00", amount=1.0,
        next_payment_date=today, reminder_time="14:00", is_active=True,
        created_at=to_utc_naive(now_local - timedelta(hours=5)),
    )
    slot, sends, next_at = compute_plan_state(exp_dday, plan, tz, now_local=now_local)
    assert slot == "d", slot
    expected = to_utc_naive(local_datetime_on_date(tz, today, 14, 0))
    assert next_at == expected, (next_at, expected)
    assert not d_day_send_allowed(exp_dday, tz, now_local)  # before 14:00
    print("[OK] d slot honors reminder_time")

    # no reminder_time + created < 2h ago — create cooldown applies
    created_utc = to_utc_naive(now_local - timedelta(hours=1))
    exp_recent = Expense(
        user_id=1, title="Recent", amount=1.0,
        next_payment_date=today, is_active=True,
        created_at=created_utc,
    )
    slot, sends, next_at = compute_plan_state(exp_recent, plan, tz, now_local=now_local)
    assert slot == "d"
    assert next_at == created_utc + timedelta(hours=2), next_at
    assert not d_day_send_allowed(exp_recent, tz, now_local)
    print("[OK] no reminder_time: 2h create cooldown")

    # overdue: f fires now; repeats from last send + every_hours
    exp_od = Expense(
        user_id=1, title="Overdue", amount=1.0,
        next_payment_date=today - timedelta(days=1), is_active=True,
    )
    slot, sends, next_at = compute_plan_state(exp_od, plan, tz, now_local=now_local)
    assert slot == "f" and next_at == to_utc_naive(now_local)
    last_sent = now_local - timedelta(hours=3)
    slot, sends, next_at = compute_plan_state(
        exp_od, plan, tz, now_local=now_local,
        current_slot="f", current_sends=2, last_sent_local=last_sent,
    )
    assert slot == "f" and sends == 2
    assert next_at == to_utc_naive(last_sent + timedelta(hours=2)), next_at
    print("[OK] overdue repeats last_sent + 2h")

    # f backoff: overdue > 7 days → interval stretches to 24h
    exp_ob = Expense(
        user_id=1, title="Old overdue", amount=1.0,
        next_payment_date=today - timedelta(days=OVERDUE_BACKOFF_DAYS + 2), is_active=True,
    )
    slot, sends, next_at = compute_plan_state(
        exp_ob, plan, tz, now_local=now_local,
        current_slot="f", current_sends=5, last_sent_local=last_sent,
    )
    assert next_at == to_utc_naive(last_sent + timedelta(hours=24)), next_at
    print("[OK] f backoff after 7 days overdue")

    # exhaustion → immediately schedules the NEXT episode (n2 done → d day)
    exp_tr = Expense(
        user_id=1, title="Transition", amount=1.0,
        next_payment_date=today + timedelta(days=1), is_active=True,
        created_at=to_utc_naive(now_local - timedelta(days=2)),
    )
    slot, sends, next_at = compute_plan_state(exp_tr, plan, tz, now_local=now_local)
    assert slot == "n2" and next_at == to_utc_naive(now_local), (slot, next_at)
    slot, sends, next_at = compute_plan_state(
        exp_tr, plan, tz, now_local=now_local, current_slot="n2", current_sends=1,
    )
    assert slot == "d" and sends == 0, (slot, sends)
    expected = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=1), H))
    assert next_at == expected, (next_at, expected)
    print("[OK] exhausted wave hands over to the due-day episode")

    # all-off plan → complete silence
    slot, sends, next_at = compute_plan_state(exp_tr, rp.all_off_plan(), tz, now_local=now_local)
    assert slot is None and next_at is None
    print("[OK] all-off plan -> no fires")


def test_reminder_engine_backfill():
    """Test backfill writes next_reminder_at to DB."""
    print("\nTesting reminder_engine backfill...")
    migrate_db()
    db = SessionLocal()
    try:
        user_id = 111222333
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()

        due_soon = today_in_tz("Europe/Moscow") + timedelta(days=2)
        due_far = today_in_tz("Europe/Moscow") + timedelta(days=30)
        exp_in = add_expense(
            db, user_id=user_id, title="In window", amount=10.0,
            next_payment_date=due_soon, period="month", is_active=True,
        )
        exp_out = add_expense(
            db, user_id=user_id, title="Out window", amount=10.0,
            next_payment_date=due_far, period="month", is_active=True,
        )
        db.commit()
        db.refresh(exp_in)
        db.refresh(exp_out)

        apply_reminder_state(exp_in, "Europe/Moscow", db)
        apply_reminder_state(exp_out, "Europe/Moscow", db)
        db.commit()
        db.refresh(exp_in)
        db.refresh(exp_out)

        days_in = (exp_in.next_payment_date - today_in_tz("Europe/Moscow")).days
        assert exp_in.reminder_stage == stage_from_days_until(days_in), exp_in.reminder_stage
        assert exp_in.next_reminder_at is not None
        # v3: a far-future expense gets its FIRST wave precomputed (due-3d for
        # the default plan) instead of staying NULL until the sweep window;
        # reminder_slot stays NULL until a send actually happens
        assert exp_out.next_reminder_at is not None
        assert exp_out.reminder_slot is None, exp_out.reminder_slot
        assert exp_out.reminder_stage is None, exp_out.reminder_stage  # legacy mirror only inside v2 window

        stats = backfill_all(db)
        assert stats["total_active"] >= 2
        print(f"[OK] apply_reminder_state + backfill stats: {stats}")

        db.delete(exp_in)
        db.delete(exp_out)
        db.commit()
    except Exception as e:
        print(f"[ERROR] backfill test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_recompute_hooks():
    """Test recompute_after_mutation clears inactive and sets active."""
    print("\nTesting recompute hooks...")
    db = SessionLocal()
    try:
        user_id = 444555666
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()

        due_soon = today_in_tz("Europe/Moscow") + timedelta(days=1)
        expense = add_expense(
            db, user_id=user_id, title="Hook test", amount=5.0,
            next_payment_date=due_soon, period="none", is_active=True,
        )
        db.commit()
        db.refresh(expense)

        recompute_after_mutation(db, expense_id=expense.id)
        db.commit()
        db.refresh(expense)
        assert expense.reminder_stage == "1_day"
        # slot/sends belong to the last SENT episode — untouched by recomputes
        assert expense.reminder_slot is None
        assert expense.next_reminder_at is not None

        expense.is_active = False
        recompute_after_mutation(db, expense_id=expense.id)
        db.commit()
        db.refresh(expense)
        assert expense.reminder_stage is None
        assert expense.reminder_slot is None
        assert expense.reminder_slot_sends == 0
        assert expense.next_reminder_at is None
        print("[OK] recompute_after_mutation active/inactive")

        db.delete(expense)
        db.commit()
    except Exception as e:
        print(f"[ERROR] recompute hooks test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_same_day_ai_reschedule_rearms_exact_time():
    """A +15m /ai move must re-arm d even after its first send."""
    print("\nTesting same-day AI reschedule exact-time re-arm...")
    db = SessionLocal()
    uid = 700000025
    try:
        tz = _tz_for_local_hour(12)
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz, reminder_hour=10))
        db.commit()

        now_local = now_in_tz(tz)
        target_local = (now_local + timedelta(minutes=15)).replace(second=0, microsecond=0)
        expense = add_expense(
            db,
            user_id=uid,
            expense_type="task",
            title="Same-day +15m",
            amount=0,
            currency="RUB",
            next_payment_date=target_local.date(),
            reminder_time=now_local.strftime("%H:%M"),
            period="none",
            is_active=True,
            reminder_slot="d",
            reminder_slot_sends=1,
            next_reminder_at=to_utc_naive(now_local + timedelta(hours=2)),
        )
        db.commit()
        seq = expense.user_seq

        result = execute_function(
            uid,
            "reschedule_expense",
            {"id": seq, "new_date": target_local.strftime("%d.%m.%y %H:%M")},
            tz,
        )
        assert result["success"], result
        assert target_local.strftime("%H:%M") in result["message"], result

        db.expire_all()
        moved = get_expense_by_user_seq(db, uid, seq)
        assert moved.reminder_time == target_local.strftime("%H:%M")
        assert moved.reminder_slot is None, moved.reminder_slot
        assert moved.reminder_slot_sends == 0, moved.reminder_slot_sends
        assert moved.next_reminder_at == to_utc_naive(target_local), (
            moved.next_reminder_at, to_utc_naive(target_local)
        )
        print("[OK] same-day +15m move re-arms d at the requested minute")

        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.commit()
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] same-day AI reschedule test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_d_day_send_allowed():
    """Test d_day gating matches legacy rules."""
    print("\nTesting d_day_send_allowed...")
    tz = "Europe/Moscow"
    due = datetime.now(ZoneInfo(tz)).date()
    now_early = datetime(due.year, due.month, due.day, 8, 0, tzinfo=ZoneInfo(tz))
    exp = Expense(
        user_id=1, title="Gate", amount=1.0,
        next_payment_date=due, reminder_time="09:00", is_active=True,
        created_at=to_utc_naive(now_early - timedelta(hours=5)),
    )
    assert not d_day_send_allowed(exp, tz, now_early)
    now_late = datetime(due.year, due.month, due.day, 10, 0, tzinfo=ZoneInfo(tz))
    assert d_day_send_allowed(exp, tz, now_late)
    print("[OK] d_day_send_allowed reminder_time gate")


def test_list_immediate_exhausted_due_day():
    """An explicit /list re-shows today's enabled reminder after exhaustion,
    while an exhausted overdue slot remains silent."""
    print("\nTesting /list immediate exhausted due-day policy...")
    once = {"on": True, "times": 1, "every_hours": 24}
    assert _list_immediate_send_allowed("d", once, 1)
    assert _list_immediate_send_allowed("d", once, 99)
    assert _list_immediate_send_allowed("f", once, 0)
    assert not _list_immediate_send_allowed("f", once, 1)

    uid = 700000023
    tz = _tz_for_local_hour(12)
    today = today_in_tz(tz)
    plan_json = rp.plan_to_json({"n": [], "d": once, "f": once})
    db = SessionLocal()
    try:
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz))
        db.commit()
        due = add_expense(
            db, user_id=uid, expense_type="task", title="Due today", amount=0,
            currency="RUB", next_payment_date=today, reminder_time="00:00",
            period="none", is_active=True, reminder_plan=plan_json,
            reminder_slot="d", reminder_slot_sends=1,
        )
        overdue = add_expense(
            db, user_id=uid, expense_type="task", title="Overdue", amount=0,
            currency="RUB", next_payment_date=today - timedelta(days=1),
            period="none", is_active=True, reminder_plan=plan_json,
            reminder_slot="f", reminder_slot_sends=1,
        )
        db.commit()
        due_id, overdue_id = due.id, overdue.id
    finally:
        db.close()

    sent = []
    original_send = scheduler_module.send_reminder

    async def fake_send(_application, expense, slot, days_until):
        sent.append((expense.id, slot, days_until))
        return "ok", 9000 + len(sent)

    scheduler_module.send_reminder = fake_send
    try:
        asyncio.run(scheduler_module.send_list_immediate_reminders(object(), uid))
        assert sent == [(due_id, "d", 0)], sent

        db = SessionLocal()
        try:
            due = db.query(Expense).filter(Expense.id == due_id).first()
            overdue = db.query(Expense).filter(Expense.id == overdue_id).first()
            assert due.reminder_slot_sends == 2, due.reminder_slot_sends
            assert overdue.reminder_slot_sends == 1, overdue.reminder_slot_sends
        finally:
            db.close()
    finally:
        scheduler_module.send_reminder = original_send
        db = SessionLocal()
        try:
            expense_ids = [row[0] for row in db.query(Expense.id).filter(Expense.user_id == uid).all()]
            if expense_ids:
                db.query(ReminderLog).filter(ReminderLog.expense_id.in_(expense_ids)).delete(
                    synchronize_session=False)
            db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
            db.query(User).filter(User.id == uid).delete()
            db.commit()
        finally:
            db.close()

    print("[OK] /list re-shows exhausted d; exhausted f stays silent")


def test_task_reminder_buttons_hidden_for_future():
    """Future tasks have no actions; due and overdue tasks do."""
    print("\nTesting task reminder button future-only suppression policy...")

    due_today = _reminder_reply_markup(True, 17, 0)
    assert due_today is not None
    assert [button.callback_data for row in due_today.inline_keyboard for button in row] == [
        "pay_17", "move_17_3h", "move_17_1d",
    ]

    assert _reminder_reply_markup(True, 17, 1) is None
    assert _reminder_reply_markup(True, 17, 30) is None

    overdue = _reminder_reply_markup(True, 17, -1)
    assert overdue is not None
    assert [button.callback_data for row in overdue.inline_keyboard for button in row] == [
        "pay_17", "move_17_3h", "move_17_1d",
    ]

    future_payment = _reminder_reply_markup(False, 18, 30)
    assert future_payment is not None
    assert [button.callback_data for row in future_payment.inline_keyboard for button in row] == [
        "pay_18", "move_18_3h", "move_18_1d",
    ]

    print("[OK] task actions are hidden only before the due date")


def test_record_reminder_sent():
    """Test record_reminder_sent bumps the slot counter and advances the fire."""
    print("\nTesting record_reminder_sent...")
    db = SessionLocal()
    try:
        user_id = 777888999
        tz = _tz_for_local_hour(12)  # away from midnight: sent+2h must stay on the due day
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone=tz)
            db.add(user)
            db.commit()
        else:
            user.timezone = tz
            db.commit()

        due = today_in_tz(tz)
        expense = add_expense(
            db, user_id=user_id, title="Record", amount=1.0,
            next_payment_date=due, period="none", is_active=True,
            reminder_slot="d", reminder_slot_sends=0,
            next_reminder_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.commit()
        db.refresh(expense)

        sent = datetime.now(timezone.utc).replace(tzinfo=None)
        record_reminder_sent(db, expense.id, "d", 12345, sent)
        db.commit()
        db.refresh(expense)
        # default plan d: ∞×@2h → same episode continues 2h after the send
        assert expense.reminder_slot == "d"
        assert expense.reminder_slot_sends == 1
        assert expense.next_reminder_at == sent + timedelta(hours=2), expense.next_reminder_at
        logs = db.query(ReminderLog).filter(ReminderLog.expense_id == expense.id).all()
        assert len(logs) == 1
        assert logs[0].stage == "d"
        assert logs[0].message_id == 12345
        print("[OK] record_reminder_sent (slot counter + next fire)")

        db.query(ReminderLog).filter(ReminderLog.expense_id == expense.id).delete()
        db.delete(expense)
        db.commit()
    except Exception as e:
        print(f"[ERROR] record_reminder_sent test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_recompute_stages_for_window():
    """Test hourly sweep updates near-window expenses."""
    print("\nTesting recompute_stages_for_window...")
    db = SessionLocal()
    try:
        user_id = 333222111
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow")
            db.add(user)
            db.commit()

        due = today_in_tz("Europe/Moscow") + timedelta(days=3)
        expense = add_expense(
            db, user_id=user_id, title="Sweep", amount=1.0,
            next_payment_date=due, period="month", is_active=True,
        )
        db.commit()
        db.refresh(expense)

        count = recompute_stages_for_window(db)
        db.commit()
        db.refresh(expense)
        assert count >= 1
        assert expense.reminder_stage == "3_days"
        print(f"[OK] recompute_stages_for_window updated {count}")

        db.delete(expense)
        db.commit()
    except Exception as e:
        print(f"[ERROR] window sweep test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_seq_per_user():
    """Per-user IDs are independent; lookup scoped by user_id."""
    print("\nTesting per-user user_seq...")
    db = SessionLocal()
    try:
        u1, u2 = 111111111, 222222222
        for uid in (u1, u2):
            if not db.query(User).filter(User.id == uid).first():
                db.add(User(id=uid, timezone="Europe/Moscow"))
        db.commit()

        e1 = add_expense(db, user_id=u1, title="A", amount=1.0,
                         next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        e2 = add_expense(db, user_id=u2, title="B", amount=2.0,
                         next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        e3 = add_expense(db, user_id=u1, title="C", amount=3.0,
                         next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        db.commit()
        assert e3.user_seq > e1.user_seq
        assert get_expense_by_user_seq(db, u1, e1.user_seq).title == "A"
        assert get_expense_by_user_seq(db, u2, e2.user_seq).title == "B"
        # Both users can have user_seq=1; isolation is by user_id
        cross = db.query(Expense).filter(Expense.id == e1.id, Expense.user_id == u2).first()
        assert cross is None
        print(f"[OK] user1 seq {e1.user_seq},{e3.user_seq}; user2 seq {e2.user_seq}")

        for exp in (e1, e2, e3):
            db.delete(exp)
        db.commit()
    except Exception as e:
        print(f"[ERROR] user_seq test failed: {e}")
        db.rollback()
    finally:
        db.close()


class _FakeUser:
    def __init__(self, first_name="Ivan", last_name=None, username=None, id=1):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.id = id


class _FakeOrigin:
    def __init__(self, type, date=None, **kwargs):
        self.type = type
        self.date = date
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMessage:
    def __init__(self, text=None, caption=None, **kwargs):
        self.text = text
        self.caption = caption
        self.from_user = kwargs.get("from_user")
        self.sender_chat = kwargs.get("sender_chat")
        self.date = kwargs.get("date")
        self.forward_origin = kwargs.get("forward_origin")
        self.reply_to_message = kwargs.get("reply_to_message")
        self.external_reply = kwargs.get("external_reply")
        self.quote = kwargs.get("quote")


def test_ai_create_task_returns_user_seq():
    """create_task must not touch detached Expense after session closes."""
    print("\nTesting AI create_task detached session fix...")
    db = SessionLocal()
    try:
        user_id = 424242424
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()

        due = today_in_tz("Europe/Moscow").strftime("%d.%m.%y")
        result = execute_create_task(
            user_id,
            {
                "title": "Water the plants at https://example.com/care",
                "date": f"{due} 18:05",
                "period": "none",
            },
            "Europe/Moscow",
        )
        assert result["success"], result
        assert "ID:" in result["message"]
        assert "DetachedInstanceError" not in result.get("message", "")

        expense = db.query(Expense).filter(
            Expense.user_id == user_id,
            Expense.title.like("%plants%"),
            Expense.is_active == True,
        ).first()
        if expense:
            db.delete(expense)
            db.commit()
        print("[OK] AI create_task returns user_seq without DetachedInstanceError")
    except Exception as e:
        print(f"[ERROR] AI create_task test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_ai_multiline_title_note():
    """Multi-line titles via /ai: literal \\n from the model is normalized to a
    real newline, list_expenses keeps one row per record and surfaces the note
    as "note: ...", edit_expense round-trips the note."""
    print("\nTesting AI multi-line title note...")
    from ai_handler import execute_create_task, execute_list_expenses, execute_edit_expense
    db = SessionLocal()
    try:
        user_id = 434343434
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()

        due = today_in_tz("Europe/Moscow").strftime("%d.%m.%y")
        # the model emitted a literal backslash-n instead of a newline
        result = execute_create_task(
            user_id,
            {"title": "Meeting\\nhttps://meet.example.com/xyz", "date": due, "period": "none"},
            "Europe/Moscow",
        )
        assert result["success"], result
        assert "(+ note)" in result["message"], result["message"]

        exp = db.query(Expense).filter(
            Expense.user_id == user_id, Expense.is_active == True,
        ).first()
        assert exp.title == "Meeting\nhttps://meet.example.com/xyz", repr(exp.title)
        seq = exp.user_seq

        listing = execute_list_expenses(user_id, "Europe/Moscow")
        rows = [l for l in listing["message"].split("\n") if l.startswith(f"ID {seq} ")]
        assert len(rows) == 1, listing["message"]  # note must not break the row format
        assert "note: https://meet.example.com/xyz" in rows[0], rows[0]

        result = execute_edit_expense(
            user_id, {"id": seq, "title": "Sync\\nhttps://meet.example.com/xyz"},
        )
        assert result["success"], result
        db.expire_all()
        exp = db.query(Expense).filter(
            Expense.user_id == user_id, Expense.is_active == True,
        ).first()
        assert exp.title == "Sync\nhttps://meet.example.com/xyz", repr(exp.title)

        db.delete(exp)
        db.commit()
        print("[OK] multi-line title: \\n normalized, note listed, edit preserves it")
    except Exception as e:
        print(f"[ERROR] AI multi-line title test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_action_intent_not_triggered_by_forward_context():
    """Forwarded receipt text must not trip action-intent guard on informational queries."""
    print("\nTesting action-intent vs forward context...")
    forwarded = _FakeMessage(text="Internet payment 750 RUB")
    msg = _FakeMessage(
        text="/ai",
        reply_to_message=forwarded,
    )
    prompt = build_ai_user_message(msg, "what does this mean?")
    intent_text = "what does this mean?"
    assert _ACTION_INTENT_RE.search(prompt), "sanity: full prompt contains payment words"
    assert not _ACTION_INTENT_RE.search(intent_text), "intent_text alone must not match"
    print("[OK] action-intent scoped to typed /ai args")


def test_ai_response_fallbacks():
    """Empty model content and missing final replies still produce user text."""
    print("\nTesting AI response fallbacks...")
    assert _model_content({"content": None}) is None
    assert _model_content({"content": "  "}) is None
    assert _model_content({"content": "Done"}) == "Done"
    assert _format_ai_display_text("") == "✅ Done!"
    assert _format_ai_display_text(None) == "✅ Done!"

    messages = [
        {"role": "tool", "content": '{"success": true, "message": "✅ Task created"}'},
    ]
    assert _last_tool_user_message(messages) == "✅ Task created"

    # A read-only tool call (list_expenses/list_backups) must never leak its
    # raw AI-only dump as the user-facing fallback reply.
    messages_with_readonly_tail = [
        {"role": "tool", "tool_call_id": "modify_1", "content": '{"success": true, "message": "✅ Task created"}'},
        {"role": "tool", "tool_call_id": "readonly_1", "content": '{"success": true, "message": "ID 1 | TASK | ..."}'},
    ]
    assert _last_tool_user_message(messages_with_readonly_tail, {"readonly_1"}) == "✅ Task created"
    assert _last_tool_user_message(messages_with_readonly_tail) == "ID 1 | TASK | ..."
    print("[OK] AI response fallbacks")


def test_ai_mixed_modify_tool_aggregation():
    """Multi-tool /ai turn: mixed / all-fail / all-success aggregation contract.

    Mirrors the runtime path after modify_tool_results are collected
    (call_openrouter → _aggregate_modify_tool_results). Regression for the
    bug where any single failure hid sibling successes and wiped expense_ids
    / cleanup (the user saw only "no record with ID 70" after a successful mark/edit).
    """
    print("\nTesting AI mixed modify-tool aggregation...")

    success_msg = "✅ Payment 'Grok subscription' marked. Next due: 03.09.26"
    fail_msg = "❌ No record with ID 70."
    success_result = {
        "success": True,
        "message": success_msg,
        "expense_id": 42,
    }
    fail_result = {
        "success": False,
        "message": fail_msg,
    }
    expense_ids = [42]
    cleanup = [(1001, 42)]

    # (a) mixed: first succeeds, second fails — both texts, success true, ids kept
    mixed = _aggregate_modify_tool_results(
        [success_result, fail_result],
        expense_ids=expense_ids,
        cleanup=cleanup,
    )
    assert mixed["success"] is True, "any-success must not report total failure"
    assert success_msg in mixed["raw_final"]
    assert fail_msg in mixed["raw_final"]
    assert success_msg in mixed["message"]
    assert fail_msg in mixed["message"]
    # success line before failure line (order preserved)
    assert mixed["raw_final"].index(success_msg) < mixed["raw_final"].index(fail_msg)
    assert mixed["expense_ids"] == [42]
    assert mixed["cleanup"] == [(1001, 42)]
    print("[OK] mixed success+failure keeps both messages and metadata")

    # reverse order still keeps both messages
    mixed_rev = _aggregate_modify_tool_results(
        [fail_result, success_result],
        expense_ids=expense_ids,
        cleanup=cleanup,
    )
    assert mixed_rev["success"] is True
    assert fail_msg in mixed_rev["raw_final"] and success_msg in mixed_rev["raw_final"]
    assert mixed_rev["expense_ids"] == [42]
    print("[OK] mixed failure-then-success still overall success")

    # (b) all failures → overall failure, no success whitewash, no ids
    fail2 = {"success": False, "message": "❌ No record with ID 99."}
    all_fail = _aggregate_modify_tool_results(
        [fail_result, fail2],
        expense_ids=[],
        cleanup=[],
    )
    assert all_fail["success"] is False
    assert fail_msg in all_fail["raw_final"]
    assert "ID 99" in all_fail["raw_final"]
    assert all_fail["expense_ids"] == []
    assert all_fail["cleanup"] == []
    # must not invent a success framing
    assert "✅" not in all_fail["raw_final"]
    print("[OK] all-fail stays failure with every failure message")

    # (c) all successes → combined messages, success true, ids/cleanup pass through
    s2_msg = "✅ 'Grok subscription' updated: amount, currency."
    all_ok = _aggregate_modify_tool_results(
        [
            success_result,
            {"success": True, "message": s2_msg, "expense_id": 42},
        ],
        expense_ids=[42, 42],
        cleanup=cleanup,
    )
    assert all_ok["success"] is True
    assert success_msg in all_ok["raw_final"]
    assert s2_msg in all_ok["raw_final"]
    assert all_ok["expense_ids"] == [42, 42]
    assert all_ok["cleanup"] == cleanup
    print("[OK] all-success combines messages and keeps metadata")

    # call_openrouter still routes through this helper (structural wire-up)
    import inspect
    import ai_handler as _ai
    src = inspect.getsource(_ai.call_openrouter)
    assert "_aggregate_modify_tool_results" in src, (
        "call_openrouter must use _aggregate_modify_tool_results for modify results"
    )
    # old early-return that only kept failures[-1] must be gone
    assert "failures[-1]" not in src
    print("[OK] call_openrouter wired to pure aggregation helper")


def test_telegram_message_context():
    """Reply/forward context is passed to AI and conversation helpers."""
    print("\nTesting Telegram message context...")
    from datetime import datetime

    dt = datetime(2026, 6, 25, 14, 30)
    origin = _FakeOrigin("user", date=dt, sender_user=_FakeUser("Anna", username="anna"))
    forwarded = _FakeMessage(
        text="Internet invoice 750 RUB",
        from_user=_FakeUser("Bot"),
        forward_origin=origin,
        date=dt,
    )
    msg = _FakeMessage(
        text="/ai create a payment",
        from_user=_FakeUser("User"),
        reply_to_message=forwarded,
    )

    assert message_body_text(forwarded) == "Internet invoice 750 RUB"
    assert "Anna" in format_forward_origin(origin)
    assert has_telegram_message_context(msg)
    assert not has_telegram_message_context(_FakeMessage(text="plain"))

    ai_prompt = build_ai_user_message(msg, "create a payment from this")
    assert "[Reply to a message]" in ai_prompt
    assert "Internet invoice 750 RUB" in ai_prompt
    assert "[User request]" in ai_prompt
    assert "create a payment from this" in ai_prompt

    reply_only = _FakeMessage(
        text="/ai",
        reply_to_message=_FakeMessage(text="Renew the subscription tomorrow"),
    )
    ai_reply_only = build_ai_user_message(reply_only, "")
    assert "Renew the subscription tomorrow" in ai_reply_only
    assert "no text" in ai_reply_only

    empty_reply = _FakeMessage(text="", reply_to_message=_FakeMessage(text="Forwarded title"))
    assert effective_conversation_text(empty_reply) == "Forwarded title"

    # Cross-chat reply: Telegram puts ExternalReplyInfo (no text) + optional TextQuote.
    class _FakeQuote:
        def __init__(self, text):
            self.text = text

    class _FakeExt:
        def __init__(self, **kwargs):
            for name in (
                "document", "photo", "voice", "video", "audio", "sticker",
                "contact", "location", "venue", "poll", "invoice", "game",
                "dice", "story", "message_id", "origin", "chat",
            ):
                setattr(self, name, None)
            for k, v in kwargs.items():
                setattr(self, k, v)

    ext_origin = _FakeOrigin(
        "chat", date=dt, sender_chat=type("C", (), {"title": "Work chat"})(),
    )
    ext = _FakeExt(origin=ext_origin, chat=type("C", (), {"title": "Work chat"})(), message_id=42)
    cross = _FakeMessage(
        text="/ai create a payment",
        external_reply=ext,
        quote=_FakeQuote("Invoice 1200 RUB for hosting"),
    )
    assert has_telegram_message_context(cross)
    cross_prompt = build_ai_user_message(cross, "create a payment")
    assert "[Reply to a message from another chat]" in cross_prompt
    assert "Invoice 1200 RUB for hosting" in cross_prompt
    assert "Work chat" in cross_prompt

    # External reply without quote/media: must not silently drop — tell to forward.
    bare_ext = _FakeMessage(
        text="/ai",
        external_reply=_FakeExt(origin=ext_origin, chat=type("C", (), {"title": "Ops"})()),
    )
    bare_prompt = build_ai_user_message(bare_ext, "")
    assert "not available" in bare_prompt or "forward" in bare_prompt.lower()
    assert "Ops" in bare_prompt
    print("[OK] Telegram message context")


def test_currency_conversion():
    """USD cross-rate conversion between supported currencies."""
    print("\nTesting currency conversion...")
    rates = {"USD": 1.0, "RUB": 90.0, "EUR": 0.9, "GBP": 0.8, "CNY": 7.2}

    assert abs(convert_amount(90, "RUB", "USD", rates) - 1.0) < 0.01
    assert abs(convert_amount(10, "USD", "RUB", rates) - 900.0) < 0.01
    assert abs(convert_amount(900, "RUB", "EUR", rates) - 9.0) < 0.01
    assert currency_symbol("EUR") == "€"
    assert currency_symbol("USD") == "$"
    assert currency_symbol("RUB") == "₽"
    assert len(SETTINGS_CURRENCIES) == 5
    print("[OK] Currency conversion")


def test_detect_currency_default():
    """detect_currency falls back to user default."""
    print("\nTesting detect_currency default...")
    assert detect_currency("750", default="EUR") == "EUR"
    assert detect_currency("750", default="RUB") == "RUB"
    assert detect_currency("$15") == "USD"
    assert detect_currency("$15", default="EUR") == "USD"
    print("[OK] detect_currency default")


def test_list_sort_preference_and_order():
    """users.list_sort defaults to date; id mode sorts by user_seq; date by due date."""
    print("\nTesting list_sort preference + _sort_expenses_for_list...")
    init_db()
    migrate_db()
    db = SessionLocal()
    try:
        user_id = 88001
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow", default_currency="RUB"))
            db.commit()

        assert get_user_list_sort(db, user_id) == DEFAULT_LIST_SORT == LIST_SORT_DATE

        old = update_user_list_sort(db, user_id, LIST_SORT_ID)
        db.commit()
        assert old == LIST_SORT_DATE
        assert get_user_list_sort(db, user_id) == LIST_SORT_ID

        update_user_list_sort(db, user_id, LIST_SORT_DATE)
        db.commit()
        assert get_user_list_sort(db, user_id) == LIST_SORT_DATE

        try:
            update_user_list_sort(db, user_id, "bogus")
            assert False, "expected ValueError for invalid mode"
        except ValueError:
            pass

        today = today_in_tz("Europe/Moscow")
        # Fake expense-like objects with the fields the sorter uses
        class _E:
            def __init__(self, user_seq, next_payment_date, reminder_time=None):
                self.user_seq = user_seq
                self.next_payment_date = next_payment_date
                self.reminder_time = reminder_time

        items = [
            _E(3, today + timedelta(days=5)),
            _E(1, today + timedelta(days=1)),
            _E(2, today + timedelta(days=1), "09:00"),
            _E(4, today - timedelta(days=2)),
        ]
        # overdue first, then same-day with empty time before "09:00", then +5d
        by_date = _sort_expenses_for_list(items, LIST_SORT_DATE)
        assert [e.user_seq for e in by_date] == [4, 1, 2, 3], [e.user_seq for e in by_date]

        by_id = _sort_expenses_for_list(items, LIST_SORT_ID)
        assert [e.user_seq for e in by_id] == [1, 2, 3, 4], [e.user_seq for e in by_id]

        # Snapshot / restore round-trips list_sort
        update_user_list_sort(db, user_id, LIST_SORT_ID)
        db.commit()
        bid = snapshot_user_data(db, user_id, label="list_sort_test")
        db.commit()
        update_user_list_sort(db, user_id, LIST_SORT_DATE)
        db.commit()
        assert get_user_list_sort(db, user_id) == LIST_SORT_DATE
        restore_user_backup(db, user_id, bid)
        db.commit()
        assert get_user_list_sort(db, user_id) == LIST_SORT_ID

        print("[OK] list_sort default/date/id + snapshot restore")
    except Exception as e:
        print(f"[ERROR] list_sort test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_default_currency_migration():
    """User default currency is stored and payments are converted."""
    print("\nTesting user default currency...")
    init_db()
    migrate_db()

    db = SessionLocal()
    user_id = 987654323
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow", default_currency="RUB")
            db.add(user)
        else:
            user.default_currency = "RUB"
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        assert get_user_default_currency(db, user_id) == "RUB"

        future_date = datetime.now().date() + timedelta(days=5)
        expense = add_expense(
            db,
            user_id=user_id,
            title="USD sub",
            amount=10.0,
            currency="USD",
            next_payment_date=future_date,
            period="month",
        )
        db.commit()
        expense_id = expense.id

        rates = {"USD": 1.0, "RUB": 90.0, "EUR": 0.9, "GBP": 0.8, "CNY": 7.2}
        old, updated = update_user_default_currency(db, user_id, "EUR", rates)
        db.commit()

        assert old == "RUB"
        assert updated == 1
        assert get_user_default_currency(db, user_id) == "EUR"

        converted = db.query(Expense).filter(Expense.id == expense_id).first()
        assert converted.currency == "EUR"
        assert abs(converted.amount - 9.0) < 0.01
        print("[OK] User default currency")
    finally:
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.default_currency = "RUB"
            db.commit()
        db.close()


def _build_utc_ics(summary, dt_utc, rrule_line=None, location=None):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//test//EN",
        "BEGIN:VEVENT",
        f"UID:test-{dt_utc.strftime('%Y%m%dT%H%M%S')}@example.com",
        f"DTSTAMP:{dt_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{dt_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if rrule_line:
        lines.append(rrule_line)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines).encode()


def _build_outlook_tz_ics(summary, local_dt, tzid="(UTC+03:00) Moscow, St. Petersburg", offset="+0300", location=None):
    """Mirrors a real Outlook/Exchange invite: DTSTART references a custom
    (non-IANA) TZID resolved via an embedded VTIMEZONE block."""
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//test//EN",
        "BEGIN:VTIMEZONE", f"TZID:{tzid}",
        "BEGIN:STANDARD", "DTSTART:16010101T000000",
        f"TZOFFSETFROM:{offset}", f"TZOFFSETTO:{offset}", "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        f"UID:test-outlook-{local_dt.strftime('%Y%m%dT%H%M%S')}@example.com",
        f"DTSTAMP:{local_dt.strftime('%Y%m%dT%H%M%S')}Z",
        f'DTSTART;TZID="{tzid}":{local_dt.strftime("%Y%m%dT%H%M%S")}',
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines).encode()


def test_ics_parse_basic():
    """Basic .ics parsing: title, UTC start, RRULE -> period mapping."""
    print("\nTesting .ics parsing (basic)...")
    try:
        future = datetime.now(timezone.utc) + timedelta(days=30)
        data = _build_utc_ics("Team sync", future, rrule_line="RRULE:FREQ=WEEKLY;INTERVAL=1")
        events = ics_import.parse_ics_events(data)
        assert len(events) == 1
        ev = events[0]
        assert ev["title"] == "Team sync"
        assert ev["start"].tzinfo is not None
        assert ev["period"] == "custom" and ev["period_days"] == 7
        print("[OK] basic parse + weekly RRULE -> custom/7")
    except Exception as e:
        print(f"[ERROR] ics basic parse test failed: {e}")


def test_ics_parse_outlook_custom_tzid():
    """Outlook-style custom TZID (non-IANA name) resolves via embedded
    VTIMEZONE to the right UTC offset — this is the real-world case that
    motivated using the icalendar library instead of hand-rolled parsing."""
    print("\nTesting .ics parsing (Outlook custom TZID)...")
    try:
        future_local = datetime.now() + timedelta(days=30)
        future_local = future_local.replace(hour=14, minute=30, second=0, microsecond=0)
        data = _build_outlook_tz_ics("PT sync", future_local, location="https://call.example.com/x")
        events = ics_import.parse_ics_events(data)
        assert len(events) == 1
        ev = events[0]
        assert ev["start"].utcoffset().total_seconds() == 3 * 3600
        local_date, local_time = ics_import.event_local_date_and_time(ev, "Europe/Moscow")
        assert local_date == future_local.date()
        assert local_time == "14:30"
        # A different user timezone shifts the wall-clock time correctly
        ny_date, ny_time = ics_import.event_local_date_and_time(ev, "America/New_York")
        assert (ny_date, ny_time) != (local_date, local_time)
        print(f"[OK] custom TZID resolved +03:00, Moscow={local_time} NY={ny_time}")
    except Exception as e:
        print(f"[ERROR] ics outlook TZID test failed: {e}")


def test_ics_rrule_period_mapping():
    """RRULE -> period mapping: simple cases map, complex ones fall back to
    one-time rather than guessing wrong."""
    print("\nTesting .ics RRULE period mapping...")
    try:
        future = datetime.now(timezone.utc) + timedelta(days=30)
        monthly = ics_import.parse_ics_events(
            _build_utc_ics("M", future, rrule_line="RRULE:FREQ=MONTHLY;INTERVAL=1")
        )[0]
        yearly = ics_import.parse_ics_events(
            _build_utc_ics("Y", future, rrule_line="RRULE:FREQ=YEARLY;INTERVAL=1")
        )[0]
        no_rrule = ics_import.parse_ics_events(_build_utc_ics("N", future))[0]
        complex_rrule = ics_import.parse_ics_events(
            _build_utc_ics("C", future, rrule_line="RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=10")
        )[0]
        assert monthly["period"] == "month"
        assert yearly["period"] == "year"
        assert no_rrule["period"] == "none"
        assert complex_rrule["period"] == "none"  # unmapped pattern -> safe fallback
        print("[OK] RRULE period mapping (month/year/none/complex-fallback)")
    except Exception as e:
        print(f"[ERROR] ics RRULE mapping test failed: {e}")


def test_add_ics_events_sync():
    """bot._add_ics_events_sync creates a real task from a parsed event, and
    rejects a past-dated one without touching the DB."""
    print("\nTesting _add_ics_events_sync (creates task)...")
    user_id = 987654324
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            user = User(id=user_id, timezone="Europe/Moscow", default_currency="RUB")
            db.add(user)
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        future = datetime.now(timezone.utc) + timedelta(days=10)
        future_event = ics_import.parse_ics_events(
            _build_utc_ics("Future meeting", future, location="Room 42")
        )[0]
        past = datetime.now(timezone.utc) - timedelta(days=10)
        past_event = ics_import.parse_ics_events(_build_utc_ics("Old meeting", past))[0]

        results = _add_ics_events_sync(user_id, [future_event, past_event], "Europe/Moscow")
        assert len(results) == 2
        assert results[0]["success"] is True and results[0]["user_seq"] is not None
        assert results[1]["success"] is False and "passed" in results[1]["error"]

        created = db.query(Expense).filter(Expense.user_id == user_id).all()
        assert len(created) == 1  # only the future one was actually created
        assert created[0].expense_type == "task"
        # Hidden-line convention: line 1 is the short /list-visible title,
        # location goes on line 2 (hidden in /list, shown in full in the
        # reminder — see title_first_line = exp.title.split('\n')[0] in
        # bot.py's /list handlers vs send_reminder's unsplit title_html).
        title_lines = created[0].title.split("\n")
        assert title_lines[0] == "Future meeting"
        assert title_lines[1] == "Room 42"
        assert results[0]["title"] == "Future meeting"  # confirmation reply shows only line 1
        print(f"[OK] created task user_seq={results[0]['user_seq']}, hidden location line, past event rejected")
    except Exception as e:
        print(f"[ERROR] add_ics_events_sync test failed: {e}")
    finally:
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
        db.close()


def test_user_backup_snapshot_and_list():
    """snapshot_user_data then list_user_backups reports one entry with the right item_count."""
    print("\nTesting user backup snapshot + list...")
    db = SessionLocal()
    try:
        user_id = 700000001
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        add_expense(db, user_id=user_id, title="A", amount=1.0,
                    next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        add_expense(db, user_id=user_id, title="B", amount=2.0,
                    next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        db.commit()

        backup_id = snapshot_user_data(db, user_id)
        db.commit()

        backups = list_user_backups(db, user_id)
        assert len(backups) == 1, backups
        assert backups[0]["id"] == backup_id, backups[0]
        assert backups[0]["item_count"] == 2, backups[0]
        print(f"[OK] snapshot+list: backup #{backup_id} with {backups[0]['item_count']} items")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] user backup snapshot/list test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_backup_restore_happy_path():
    """Restoring an earlier snapshot brings back the ORIGINAL rows (user_seq included),
    undoing deletions/additions/edits made after the snapshot was taken."""
    print("\nTesting user backup restore happy path...")
    db = SessionLocal()
    try:
        user_id = 700000002
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        date_a = today_in_tz("Europe/Moscow")
        date_b = today_in_tz("Europe/Moscow") + timedelta(days=10)
        a = add_expense(db, user_id=user_id, title="A", amount=10.0, currency="RUB",
                         next_payment_date=date_a, period="none")
        b = add_expense(db, user_id=user_id, title="B", amount=20.0, currency="RUB",
                         next_payment_date=date_b, period="none")
        db.commit()
        a_seq, b_seq, a_amount = a.user_seq, b.user_seq, a.amount

        backup_id = snapshot_user_data(db, user_id)
        db.commit()

        # Mutate state: delete B, add a new C, change a field on A.
        db.delete(b)
        db.commit()
        add_expense(db, user_id=user_id, title="C", amount=30.0, currency="RUB",
                    next_payment_date=date_a, period="none")
        a.title = "A-mutated"
        a.amount = 999.0
        db.commit()

        result = restore_user_backup(db, user_id, backup_id)
        db.commit()
        assert result is not None, "restore should succeed for the owner's own backup"

        remaining = db.query(Expense).filter(Expense.user_id == user_id).all()
        assert len(remaining) == 2, [(e.title, e.user_seq) for e in remaining]
        titles = {e.title for e in remaining}
        assert titles == {"A", "B"}, titles

        restored_a = next(e for e in remaining if e.title == "A")
        restored_b = next(e for e in remaining if e.title == "B")
        assert restored_a.amount == a_amount, restored_a.amount
        assert restored_a.user_seq == a_seq, (restored_a.user_seq, a_seq)
        assert restored_b.user_seq == b_seq, (restored_b.user_seq, b_seq)
        print(f"[OK] restore happy path: user_seq preserved {restored_a.user_seq},{restored_b.user_seq}")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] user backup restore happy-path test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_backup_restore_ownership_isolation():
    """restore_user_backup must not let one user restore another user's backup id."""
    print("\nTesting user backup restore ownership isolation...")
    db = SessionLocal()
    try:
        u1, u2 = 700000003, 700000004
        for uid in (u1, u2):
            if not db.query(User).filter(User.id == uid).first():
                db.add(User(id=uid, timezone="Europe/Moscow"))
        db.commit()
        db.query(UserBackup).filter(UserBackup.user_id.in_([u1, u2])).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.user_id.in_([u1, u2])).delete(synchronize_session=False)
        db.commit()

        add_expense(db, user_id=u1, title="U1-A", amount=5.0,
                    next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        db.commit()
        backup_id = snapshot_user_data(db, u1)
        db.commit()

        add_expense(db, user_id=u2, title="U2-A", amount=7.0,
                    next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        db.commit()
        u2_before = sorted((e.title, e.amount) for e in db.query(Expense).filter(Expense.user_id == u2).all())

        result = restore_user_backup(db, u2, backup_id)
        db.commit()
        assert result is None, result

        u2_after = sorted((e.title, e.amount) for e in db.query(Expense).filter(Expense.user_id == u2).all())
        assert u2_after == u2_before, (u2_before, u2_after)
        print("[OK] cross-user restore rejected, target user's data untouched")

        db.query(UserBackup).filter(UserBackup.user_id.in_([u1, u2])).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.user_id.in_([u1, u2])).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        print(f"[ERROR] user backup ownership isolation test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_backup_restore_unknown_id():
    """restore_user_backup returns None (no raise) for a backup_id that doesn't exist at all."""
    print("\nTesting user backup restore unknown id...")
    db = SessionLocal()
    try:
        user_id = 700000005
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()

        nonexistent_id = 999999999
        assert db.query(UserBackup).filter(UserBackup.id == nonexistent_id).first() is None

        result = restore_user_backup(db, user_id, nonexistent_id)
        db.commit()
        assert result is None, result
        print("[OK] restore with unknown backup_id returns None")
    except Exception as e:
        print(f"[ERROR] user backup unknown-id test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_backup_pruning():
    """snapshot_user_data prunes old backups beyond MAX_USER_BACKUPS_PER_USER,
    keeping the most recently created ones rather than an arbitrary subset."""
    print("\nTesting user backup pruning...")
    db = SessionLocal()
    try:
        user_id = 700000006
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.commit()

        total = config.MAX_USER_BACKUPS_PER_USER + 3
        created_ids = []
        for i in range(total):
            bid = snapshot_user_data(db, user_id, label=f"snap-{i}")
            db.commit()
            created_ids.append(bid)

        rows = db.query(UserBackup).filter(UserBackup.user_id == user_id).all()
        assert len(rows) == config.MAX_USER_BACKUPS_PER_USER, len(rows)

        kept_ids = {row.id for row in rows}
        expected_kept = set(created_ids[-config.MAX_USER_BACKUPS_PER_USER:])
        assert kept_ids == expected_kept, (kept_ids, expected_kept)
        print(f"[OK] pruning kept the {len(rows)} newest of {total} snapshots")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] user backup pruning test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_user_backup_pruning_by_age():
    """snapshot_user_data prunes backups older than config.UNDO_WINDOW_DAYS
    even when well under MAX_USER_BACKUPS_PER_USER — a low-frequency user
    still only keeps a week of rollback history, not an unbounded one."""
    print("\nTesting user backup pruning by age...")
    db = SessionLocal()
    try:
        user_id = 700000009
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.commit()

        old_id = snapshot_user_data(db, user_id, label="old")
        db.commit()
        db.query(UserBackup).filter(UserBackup.id == old_id).update(
            {"created_at": utcnow_naive() - timedelta(days=config.UNDO_WINDOW_DAYS + 1)}
        )
        db.commit()

        # Triggers another prune pass — well under MAX_USER_BACKUPS_PER_USER (2 total).
        new_id = snapshot_user_data(db, user_id, label="new")
        db.commit()

        remaining_ids = {row.id for row in db.query(UserBackup).filter(UserBackup.user_id == user_id).all()}
        assert remaining_ids == {new_id}, (remaining_ids, old_id, new_id)
        print("[OK] backup older than UNDO_WINDOW_DAYS pruned despite low count")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] user backup pruning by age test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_purge_old_inactive_expenses():
    """purge_old_inactive_expenses hard-deletes soft-deleted expenses (+ their
    reminder logs) past config.UNDO_WINDOW_DAYS, and leaves active rows and
    recently-deactivated rows alone."""
    print("\nTesting purge_old_inactive_expenses...")
    db = SessionLocal()
    try:
        user_id = 700000010
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        today = today_in_tz("Europe/Moscow")
        old_deleted = add_expense(
            db, user_id=user_id, title="Old deleted", amount=1.0,
            next_payment_date=today, period="none", is_active=False,
        )
        old_deleted.deactivated_at = utcnow_naive() - timedelta(days=config.UNDO_WINDOW_DAYS + 1)
        recent_deleted = add_expense(
            db, user_id=user_id, title="Recently deleted", amount=1.0,
            next_payment_date=today, period="none", is_active=False,
        )
        recent_deleted.deactivated_at = utcnow_naive() - timedelta(days=1)
        still_active = add_expense(
            db, user_id=user_id, title="Still active", amount=1.0,
            next_payment_date=today, period="none", is_active=True,
        )
        db.commit()
        old_deleted_id, recent_deleted_id, still_active_id = old_deleted.id, recent_deleted.id, still_active.id
        log = ReminderLog(expense_id=old_deleted_id, stage="overdue", sent_at=utcnow_naive())
        db.add(log)
        db.commit()

        removed = purge_old_inactive_expenses()
        assert removed == 1, removed

        remaining_ids = {row[0] for row in db.query(Expense.id).filter(Expense.user_id == user_id).all()}
        assert remaining_ids == {recent_deleted_id, still_active_id}, remaining_ids
        assert db.query(ReminderLog).filter(ReminderLog.expense_id == old_deleted_id).count() == 0
        print("[OK] purge removed only the deactivated-over-a-week-ago row, plus its reminder log")

        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] purge_old_inactive_expenses test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_ai_handler_backup_dispatch():
    """execute_function dispatches 'list_backups'/'restore_backup' by name to
    execute_list_backups/execute_restore_backup without crashing."""
    print("\nTesting ai_handler backup dispatch...")
    db = SessionLocal()
    try:
        user_id = 700000007
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        add_expense(db, user_id=user_id, title="Dispatch fixture", amount=1.0,
                    next_payment_date=today_in_tz("Europe/Moscow"), period="none")
        db.commit()

        backup_id = snapshot_user_data(db, user_id)
        db.commit()

        list_result = execute_function(user_id, "list_backups", {}, user_tz="Europe/Moscow")
        assert list_result["success"], list_result
        assert str(backup_id) in list_result["message"], list_result

        restore_result = execute_function(
            user_id, "restore_backup", {"backup_id": backup_id}, user_tz="Europe/Moscow",
        )
        assert restore_result["success"], restore_result
        print("[OK] execute_function dispatches list_backups/restore_backup")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] ai_handler backup dispatch test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_ai_set_reminder_plan():
    """execute_function 'set_reminder_plan': partial NL update of an expense's
    reminder plan, persisted as JSON, reschedules without touching the date."""
    print("\nTesting ai set_reminder_plan...")
    from ai_handler import execute_function
    db = SessionLocal()
    try:
        user_id = 700000011
        tz = _tz_for_local_hour(12)
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            db.add(User(id=user_id, timezone=tz)); db.commit()
        else:
            u.timezone = tz; db.commit()
        db.query(Expense).filter(Expense.user_id == user_id).delete(); db.commit()

        today = today_in_tz(tz)
        exp = add_expense(db, user_id=user_id, title="AI plan", amount=5.0,
                          next_payment_date=today + timedelta(days=40), period="month")
        db.commit()
        seq, due = exp.user_seq, exp.next_payment_date
        assert exp.reminder_plan is None  # starts on the default (v2) preset

        # "remind me a week and a day before; then once a day after it is due"
        r = execute_function(user_id, "set_reminder_plan",
                             {"id": seq, "pre_due_days": [7, 1], "overdue": "daily"}, tz)
        assert r["success"], r
        db.expire_all()
        exp = get_expense_by_user_seq(db, user_id, seq)
        assert exp.reminder_plan is not None
        plan = rp.plan_from_json(exp.reminder_plan)
        assert [s["offset_days"] for s in plan["n"] if s["on"]] == [7, 1]
        assert plan["f"]["every_hours"] == 24
        assert exp.next_payment_date == due  # date untouched
        # first fire precomputed at the 7-day wave, at the user's reminder hour
        assert exp.next_reminder_at == to_utc_naive(
            local_datetime_on_date(tz, due - timedelta(days=7), config.DEFAULT_FIRE_HOUR))
        print(f"[OK] set_reminder_plan -> {rp.plan_summary(plan)}")

        # "do not remind me" -> all off, next_reminder_at cleared
        r = execute_function(user_id, "set_reminder_plan",
                             {"id": seq, "pre_due_days": [], "due_day": "off", "overdue": "off"}, tz)
        assert r["success"], r
        db.expire_all()
        exp = get_expense_by_user_seq(db, user_id, seq)
        assert not rp.has_any_reminders(rp.plan_from_json(exp.reminder_plan))
        assert exp.next_reminder_at is None and exp.reminder_stage is None
        print("[OK] set_reminder_plan off -> silent")

        # bad input surfaces a friendly error, doesn't crash or change state
        r = execute_function(user_id, "set_reminder_plan", {"id": seq, "pre_due_days": [3, 3]}, tz)
        assert not r["success"] and "❌" in r["message"], r
        print("[OK] set_reminder_plan duplicate day rejected cleanly")

        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        print(f"[ERROR] ai set_reminder_plan test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_reminder_hour_setting():
    """users.reminder_hour: default, changing it shifts scheduled fires,
    out-of-range rejected, survives snapshot/restore."""
    print("\nTesting reminder_hour setting...")
    db = SessionLocal()
    try:
        uid = 700000021
        tz = _tz_for_local_hour(12)
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz))
        db.commit()
        assert get_user_reminder_hour(db, uid) == config.DEFAULT_FIRE_HOUR

        today = today_in_tz(tz)
        exp = add_expense(db, user_id=uid, title="Hour", amount=1.0,
                          next_payment_date=today + timedelta(days=10), period="month")
        db.commit()
        recompute_after_mutation(db, expense_id=exp.id)
        db.commit(); db.refresh(exp)
        # default plan n0 (3 days before) fires at the reminder hour, not midnight
        want = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=7), config.DEFAULT_FIRE_HOUR))
        assert exp.next_reminder_at == want, (exp.next_reminder_at, want)

        old, cnt = update_user_reminder_hour(db, uid, 18)
        db.commit(); db.refresh(exp)
        assert old == config.DEFAULT_FIRE_HOUR and cnt >= 1
        want18 = to_utc_naive(local_datetime_on_date(tz, today + timedelta(days=7), 18))
        assert exp.next_reminder_at == want18, (exp.next_reminder_at, want18)
        print("[OK] reminder_hour shifts scheduled fires")

        try:
            update_user_reminder_hour(db, uid, 25)
            print("[ERROR] out-of-range hour must be rejected")
        except ValueError:
            print("[OK] out-of-range hour rejected")

        bid = snapshot_user_data(db, uid)
        db.commit()
        db.query(User).filter(User.id == uid).update({"reminder_hour": 9})
        db.commit()
        restore_user_backup(db, uid, bid)
        db.commit()
        assert get_user_reminder_hour(db, uid) == 18
        print("[OK] reminder_hour survives snapshot/restore")

        ids = [r[0] for r in db.query(Expense.id).filter(Expense.user_id == uid).all()]
        if ids:
            db.query(ReminderLog).filter(ReminderLog.expense_id.in_(ids)).delete(synchronize_session=False)
            db.query(Expense).filter(Expense.id.in_(ids)).delete(synchronize_session=False)
        db.query(UserBackup).filter(UserBackup.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] reminder_hour test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_quiet_hours():
    """Quiet hours push a no-explicit-time night fire to 08:00; explicit-time
    slots are exempt; the /settings toggle recomputes; survives snapshot."""
    print("\nTesting quiet hours...")
    from zoneinfo import ZoneInfo
    from reminder_engine import compute_plan_state
    import reminder_plan as _rp

    tz = "Etc/GMT-3"  # fixed +03:00, no DST — deterministic
    z = ZoneInfo(tz)
    now_local = datetime(2026, 7, 15, 23, 30, tzinfo=z)   # inside the quiet window
    last_sent = datetime(2026, 7, 15, 23, 0, tzinfo=z)
    plan = _rp.default_plan()
    exp = Expense(user_id=1, title="Overdue", amount=1.0,
                  next_payment_date=now_local.date() - timedelta(days=2), is_active=True)

    # f every 2h → next would be 01:00; quiet off keeps it, quiet on → 08:00
    slot, sends, off = compute_plan_state(exp, plan, tz, now_local=now_local,
                                          current_slot="f", current_sends=1,
                                          last_sent_local=last_sent, quiet=False)
    slot, sends, on = compute_plan_state(exp, plan, tz, now_local=now_local,
                                         current_slot="f", current_sends=1,
                                         last_sent_local=last_sent, quiet=True)
    assert off == to_utc_naive(datetime(2026, 7, 16, 1, 0, tzinfo=z)), off
    assert on == to_utc_naive(datetime(2026, 7, 16, 8, 0, tzinfo=z)), on
    print("[OK] night f-repeat 01:00 → 08:00 under quiet hours")

    # an explicit slot time inside the quiet window is EXEMPT (deliberate
    # choice): a wave with at_time=07:00 on a future day stays 07:00, not 08:00
    plan_expl = _rp.normalize_plan({
        "n": [{"on": True, "offset_days": 3, "times": 1, "every_hours": 24, "at_time": "07:00"}],
        "d": {"on": False}, "f": {"on": False},
    })
    exp_t = Expense(user_id=1, title="Timed", amount=1.0,
                    next_payment_date=now_local.date() + timedelta(days=4), is_active=True)
    slot, sends, at = compute_plan_state(exp_t, plan_expl, tz, now_local=now_local, quiet=True)
    assert at == to_utc_naive(datetime(2026, 7, 16, 7, 0, tzinfo=z)), at  # 07:00, not 08:00
    print("[OK] explicit slot time exempt from quiet hours")

    # DB toggle + snapshot/restore
    db = SessionLocal()
    try:
        uid = 700000024
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone="Europe/Moscow"))
        db.commit()
        assert get_user_quiet_hours(db, uid) is True  # default on
        update_user_quiet_hours(db, uid, False)
        db.commit()
        assert get_user_quiet_hours(db, uid) is False
        bid = snapshot_user_data(db, uid)
        db.commit()
        db.query(User).filter(User.id == uid).update({"quiet_hours": True})
        db.commit()
        restore_user_backup(db, uid, bid)
        db.commit()
        assert get_user_quiet_hours(db, uid) is False  # restored
        print("[OK] quiet_hours toggle + snapshot/restore")
        db.query(UserBackup).filter(UserBackup.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] quiet_hours DB test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_reminder_reschedule_buttons():
    """Reminder actions rewrite the actual due date and never create snooze state."""
    print("\nTesting reminder date-reschedule actions...")
    db = SessionLocal()
    try:
        uid = 700000022
        tz = _tz_for_local_hour(12)
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz, reminder_hour=10))
        db.commit()
        today = today_in_tz(tz)

        tomorrow_task = add_expense(
            db, user_id=uid, expense_type="task", title="Move tomorrow", amount=0,
            next_payment_date=today, period="none",
        )
        plus_three_task = add_expense(
            db, user_id=uid, expense_type="task", title="Move three hours", amount=0,
            next_payment_date=today, period="none",
        )
        db.commit()
        tomorrow_seq, plus_three_seq = tomorrow_task.user_seq, plus_three_task.user_seq

        result = _reschedule_from_reminder_sync(uid, tomorrow_seq, "1d")
        assert result["status"] == "ok"
        db.expire_all()
        moved = get_expense_by_user_seq(db, uid, tomorrow_seq)
        assert moved.next_payment_date == today + timedelta(days=1)
        assert moved.reminder_time == "10:00"
        assert to_user_tz(moved.next_reminder_at, tz).strftime("%Y-%m-%d %H:%M") == \
            f"{moved.next_payment_date.isoformat()} 10:00"
        assert moved.reminder_slot and moved.reminder_slot.startswith("n")
        assert moved.reminder_slot_sends == 1
        print("[OK] Tomorrow rewrites due date/time and consumes today's pre-due wave")

        before = now_in_tz(tz).replace(second=0, microsecond=0)
        result = _reschedule_from_reminder_sync(uid, plus_three_seq, "3h")
        after = now_in_tz(tz).replace(second=0, microsecond=0)
        assert result["status"] == "ok"
        assert before + timedelta(hours=3) <= result["due_local"] <= after + timedelta(hours=3)
        db.expire_all()
        moved_3h = get_expense_by_user_seq(db, uid, plus_three_seq)
        assert moved_3h.next_payment_date == result["due_local"].date()
        assert moved_3h.reminder_time == result["due_local"].strftime("%H:%M")
        assert to_user_tz(moved_3h.next_reminder_at, tz).strftime("%Y-%m-%d %H:%M") == \
            result["due_local"].strftime("%Y-%m-%d %H:%M")
        assert _reschedule_from_reminder_sync(uid, plus_three_seq, "bad")["status"] == "bad_code"
        print("[OK] +3 hours rewrites due date/time")

        ids = [r[0] for r in db.query(Expense.id).filter(Expense.user_id == uid).all()]
        if ids:
            db.query(ReminderLog).filter(ReminderLog.expense_id.in_(ids)).delete(synchronize_session=False)
            db.query(Expense).filter(Expense.id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] reminder reschedule test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_legacy_snooze_migration():
    """Old snoozed_until rows are folded into normal due date/time fields."""
    print("\nTesting legacy snooze migration...")
    uid = 700000024
    tz = "Europe/Moscow"
    db = SessionLocal()
    try:
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz, reminder_hour=10))
        db.commit()
        exp = add_expense(
            db, user_id=uid, expense_type="task", title="Legacy snooze", amount=0,
            next_payment_date=today_in_tz(tz), period="none",
        )
        db.commit()
        eid = exp.id

        if "snoozed_until" not in [c["name"] for c in inspect(engine).get_columns("expenses")]:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN snoozed_until DATETIME"))
        target_local = (now_in_tz(tz) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0,
        )
        target_utc = target_local.astimezone(timezone.utc).replace(tzinfo=None)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE expenses SET snoozed_until = :value WHERE id = :id"),
                {"value": target_utc, "id": eid},
            )

        assert _migrate_legacy_snoozes_to_due_dates(drop_column=True) == 1
        db.expire_all()
        migrated = db.query(Expense).filter(Expense.id == eid).first()
        assert migrated.next_payment_date == target_local.date()
        assert migrated.reminder_time == "10:00"
        assert "snoozed_until" not in [c["name"] for c in inspect(engine).get_columns("expenses")]
        print("[OK] legacy snooze converted and physical column dropped")

        db.query(Expense).filter(Expense.id == eid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] legacy snooze migration test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_edit_plan_buttons():
    """ui_from_plan round-trips simple plans / rejects complex ones, and the
    /edit plan save helpers update an existing record's plan."""
    print("\nTesting edit-plan buttons...")
    # ui_from_plan: presets and simple plans round-trip; complex → None
    for ui in (rp.ui_default(), rp.ui_day_only(), rp.ui_all_off()):
        plan = rp.plan_from_ui(ui)
        back = rp.ui_from_plan(plan)
        assert back is not None and rp.plan_from_ui(back) == plan
    fits = rp.apply_ai_plan_changes(rp.default_plan(), pre_due_days=[5, 3, 1])  # one extra wave
    assert rp.ui_from_plan(fits) is not None
    complex_plan = rp.apply_ai_plan_changes(rp.default_plan(), pre_due_days=[30, 14, 7])
    assert rp.ui_from_plan(complex_plan) is None  # 3 non-3/2/1 waves — not representable
    print("[OK] ui_from_plan round-trip + complex rejection")

    db = SessionLocal()
    try:
        uid = 700000023
        tz = _tz_for_local_hour(12)
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz))
        db.commit()
        exp = add_expense(db, user_id=uid, title="EditPlan", amount=1.0,
                          next_payment_date=today_in_tz(tz) + timedelta(days=20), period="month")
        db.commit()
        eid, seq = exp.id, exp.user_seq

        loaded = _load_plan_for_edit_sync(uid, eid)
        assert loaded is not None and loaded["plan"] == rp.default_plan()

        new_plan = rp.apply_ai_plan_changes(rp.default_plan(), pre_due_days=[1], due_day="off", overdue="daily")
        assert _update_reminder_plan_sync(uid, eid, rp.plan_to_json(new_plan)) is True
        reloaded = _load_plan_for_edit_sync(uid, eid)
        assert reloaded["plan"] == new_plan
        # wrong owner can't load or update
        assert _load_plan_for_edit_sync(uid + 1, eid) is None
        assert _update_reminder_plan_sync(uid + 1, eid, None) is False
        print("[OK] _load/_update reminder plan (ownership enforced)")

        # REGRESSION (review CRITICAL): a plan change must reset the POSITIONAL
        # episode memory — reminder_slot/reminder_slot_sends are n0..n3 by
        # offset, so re-sorting waves would otherwise remap a sent counter onto
        # a different wave (lost/duplicate ping). ORM listener on reminder_plan.
        db.query(Expense).filter(Expense.id == eid).update(
            {"reminder_slot": "n0", "reminder_slot_sends": 1})
        db.commit()
        remapped = rp.apply_ai_plan_changes(rp.default_plan(), pre_due_days=[2, 1])
        assert _update_reminder_plan_sync(uid, eid, rp.plan_to_json(remapped)) is True
        db.expire_all()
        row = get_expense_by_user_seq(db, uid, seq)
        assert row.reminder_slot is None and row.reminder_slot_sends == 0
        print("[OK] plan change resets positional episode memory")

        db.query(ReminderLog).filter(ReminderLog.expense_id == eid).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.id == eid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] edit-plan buttons test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_edit_menu_delete_option():
    """The /edit menu offers 🗑 Delete, and it runs the /delete mechanics.

    The option hands the record to the same confirm_delete / cancel_delete
    callbacks /delete uses, and it must end the /edit conversation while doing
    so: EDIT_FIELD's CallbackQueryHandler carries no pattern, so a still-live
    conversation would swallow the ✅ tap and answer with the "✏️ Value:"
    prompt instead of deleting anything.
    """
    print("\nTesting the Delete option in the /edit menu...")
    from telegram.ext import ConversationHandler as _CH
    import bot as bot_module

    sent = []  # one (text, [callback_data, ...]) per screen the bot showed

    def _screen(text, **kwargs):
        markup = kwargs.get("reply_markup")
        rows = markup.inline_keyboard if markup else ()
        sent.append((text, [b.callback_data for row in rows for b in row]))

    class _Message:
        async def reply_text(self, text, **kwargs):
            _screen(text, **kwargs)

    class _Query:
        def __init__(self, uid, data):
            self.data = data
            self.from_user = type("U", (), {"id": uid})()

        async def answer(self):
            pass

        async def edit_message_text(self, text, **kwargs):
            _screen(text, **kwargs)

    class _Update:
        def __init__(self, uid, query=None):
            self.effective_user = type("U", (), {"id": uid})()
            self.message = None if query else _Message()
            self.callback_query = query

    class _Ctx:
        def __init__(self, args):
            self.args = args
            self.user_data = {}

    init_db()
    migrate_db()
    uid = 700000041
    tz = _tz_for_local_hour(12)
    db = SessionLocal()
    try:
        db.query(Expense).filter(Expense.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.add(User(id=uid, timezone=tz))
        db.commit()
        exp = add_expense(db, user_id=uid, title="EditDelete", amount=7.0,
                          next_payment_date=today_in_tz(tz) + timedelta(days=15), period="month")
        db.commit()
        eid, seq = exp.id, exp.user_seq

        ctx = _Ctx([str(seq)])
        state = asyncio.run(bot_module.edit_expense(_Update(uid), ctx))
        assert state == bot_module.EDIT_FIELD, state
        assert "edit_delete" in sent[-1][1], sent[-1][1]
        assert ctx.user_data["edit_user_seq"] == seq, ctx.user_data
        print("[OK] the /edit menu offers Delete")

        ended = asyncio.run(bot_module.edit_field_selected(
            _Update(uid, _Query(uid, "edit_delete")), ctx))
        assert ended == _CH.END, ended
        assert ctx.user_data == {"delete_expense_id": eid}, ctx.user_data
        text, taps = sent[-1]
        assert taps == ["confirm_delete", "cancel_delete"], taps
        assert "Delete this payment?" in text, text
        print("[OK] the tap shows the /delete confirmation and ends /edit")

        asyncio.run(bot_module.button_callback(_Update(uid, _Query(uid, "cancel_delete")), ctx))
        db.expire_all()
        assert get_expense_by_user_seq(db, uid, seq).is_active, "cancel must keep the record"
        print("[OK] cancel keeps the record")

        asyncio.run(bot_module.edit_expense(_Update(uid), ctx))
        asyncio.run(bot_module.edit_field_selected(
            _Update(uid, _Query(uid, "edit_delete")), ctx))
        assert bot_module._confirm_delete_sync(eid, uid + 1) is None  # ownership
        asyncio.run(bot_module.button_callback(_Update(uid, _Query(uid, "confirm_delete")), ctx))
        db.expire_all()
        row = get_expense_by_user_seq(db, uid, seq)
        assert not row.is_active and row.deactivated_at is not None
        assert "Deleted" in sent[-1][0], sent[-1][0]
        print("[OK] confirm soft-deletes the record; another owner cannot")

        db.query(ReminderLog).filter(ReminderLog.expense_id == eid).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.id == eid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] /edit menu delete option: {e}")
        db.rollback()
    finally:
        db.close()


def test_ai_mode_toggle_and_routing():
    """Permanent AI flag: default OFF, toggle persists, free-text routing gates,
    snapshot/restore round-trips ai_mode. Drives shipped DB + bot helpers."""
    print("\nTesting AI mode toggle + free-text routing decision...")
    init_db()
    migrate_db()
    db = SessionLocal()
    user_id = 990_001
    try:
        existing = db.query(User).filter(User.id == user_id).first()
        if existing:
            db.delete(existing)
            db.commit()
        db.add(User(id=user_id))
        db.commit()

        # Default OFF (slash-only)
        assert get_user_ai_mode(db, user_id) is False
        assert _get_ai_mode_sync(user_id) is False
        assert should_route_free_text_to_ai(
            ai_mode_on=False, in_active_conversation=False,
        ) is False
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=False,
        ) is True
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=True,
        ) is False
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=False, is_command=True,
        ) is False

        # Toggle ON via shipped DB API (same as /ai command path)
        new = toggle_user_ai_mode(db, user_id)
        db.commit()
        assert new is True
        assert get_user_ai_mode(db, user_id) is True
        assert _get_ai_mode_sync(user_id) is True

        # Toggle via bot._toggle_ai_mode_sync (ai_command uses this)
        flipped = _toggle_ai_mode_sync(user_id)
        assert flipped is False
        assert _get_ai_mode_sync(user_id) is False
        flipped = _toggle_ai_mode_sync(user_id)
        assert flipped is True

        # Explicit set
        update_user_ai_mode(db, user_id, False)
        db.commit()
        assert get_user_ai_mode(db, user_id) is False

        # Snapshot / restore preserves ai_mode
        update_user_ai_mode(db, user_id, True)
        db.commit()
        bid = snapshot_user_data(db, user_id, label="ai_mode_test")
        db.commit()
        update_user_ai_mode(db, user_id, False)
        db.commit()
        assert get_user_ai_mode(db, user_id) is False
        restore_user_backup(db, user_id, bid)
        db.commit()
        assert get_user_ai_mode(db, user_id) is True

        # free_text_ai_handler: OFF → never calls OpenRouter entry
        update_user_ai_mode(db, user_id, False)
        db.commit()
        calls = {"n": 0}

        async def _fake_run(update, context, user_text):
            calls["n"] += 1

        class _Msg:
            text = "netflix 999 every month"

        class _Upd:
            message = _Msg()
            effective_user = type("U", (), {"id": user_id})()

        import bot as bot_mod
        real_run = bot_mod.run_ai_from_message
        bot_mod.run_ai_from_message = _fake_run
        try:
            asyncio.run(free_text_ai_handler(_Upd(), None))
            assert calls["n"] == 0, "OFF free-text must not enter AI path"

            update_user_ai_mode(db, user_id, True)
            db.commit()
            asyncio.run(free_text_ai_handler(_Upd(), None))
            assert calls["n"] == 1, "ON free-text must enter shipped AI entry"
        finally:
            bot_mod.run_ai_from_message = real_run

        # User-facing copy teaches toggle, not /ai <phrase>
        assert "turns it on and off" in HELP_TEXT or "on or off" in HELP_TEXT.lower()
        assert "/ai netflix" not in HELP_TEXT
        assert "is off" in AI_MODE_OFF_TEXT.lower()
        assert "is on" in AI_MODE_ON_TEXT.lower()

        # main() registers free-text handler + /ai still a CommandHandler
        src = open(os.path.join(os.path.dirname(__file__), "bot.py"), encoding="utf-8").read()
        assert "free_text_ai_handler" in src
        assert "MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_ai_handler)" in src
        assert 'CommandHandler("ai", ai_command)' in src
        # ai_command is toggle, not context.args prompt
        ai_chunk = src[src.find("async def ai_command"): src.find("async def run_ai_from_message")]
        assert "_toggle_ai_mode" in ai_chunk
        assert "context.args" not in ai_chunk

        # Live surfaces must not teach one-shot "/ai <text>" (toggle-only /ai)
        forbidden_oneshot = [
            "/ai netflix",
            "/ai remind",
            "/ai - describe",
            "Next /ai starts a new context",
            "<code>/ai ",
        ]
        for bad in forbidden_oneshot:
            assert bad not in src, f"leftover one-shot copy in bot.py: {bad!r}"
        assert "describe the payment or task in free form" not in src
        # Empty /list + complex plan editor + dialog 6/6 footer use toggle wording
        assert "Or /ai to turn on the assistant, then write as in a normal chat." in src
        assert "Turn on the AI assistant (/ai)" in src
        assert "Your next message starts a fresh context" in src
        assert "remind me about ID" in src and "/ai remind me about ID" not in src

        print("[OK] ai_mode default OFF, toggle, routing, free_text entry, snapshot")
    except Exception as e:
        print(f"[ERROR] AI mode toggle/routing test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_ai_on_slash_commands_isolated_from_free_text():
    """Independent group: with permanent AI ON, slash flows keep free-text until
    the conversation ends; free-text AI only after command work is finished.

    Drives shipped should_route_free_text_to_ai + free_text_ai_handler (spies
    run_ai_from_message / _user_in_active_conversation). Does not reimplement
    routing. Also checks main() registration order and BOT_COMMANDS surface.
    """
    print("\nTesting AI-ON: slash isolation + free-text only after command flow...")
    init_db()
    migrate_db()
    db = SessionLocal()
    user_id = 990_002
    import bot as bot_mod

    class _Msg:
        text = "netflix 999"

    class _Upd:
        def __init__(self):
            self.message = _Msg()
            self.effective_user = type("U", (), {"id": user_id})()

    try:
        existing = db.query(User).filter(User.id == user_id).first()
        if existing:
            db.delete(existing)
            db.commit()
        db.add(User(id=user_id))
        db.commit()

        # --- AI mode ON via shipped DB helpers ---
        assert get_user_ai_mode(db, user_id) is False
        update_user_ai_mode(db, user_id, True)
        db.commit()
        assert get_user_ai_mode(db, user_id) is True
        assert _get_ai_mode_sync(user_id) is True

        # --- Pure routing matrix (shipped predicate only) ---
        # AI ON + idle free text → route
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=False, is_command=False,
        ) is True
        # Mid /add|/task|/edit|/settings (or any tracked conv) → never AI
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=True, is_command=False,
        ) is False
        # Slash command surface never routes through free-text AI path
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=False, is_command=True,
        ) is False
        assert should_route_free_text_to_ai(
            ai_mode_on=True, in_active_conversation=True, is_command=True,
        ) is False
        # OFF always blocks free text even when idle
        assert should_route_free_text_to_ai(
            ai_mode_on=False, in_active_conversation=False, is_command=False,
        ) is False

        # --- free_text_ai_handler: mid-conversation → no AI entry ---
        ai_calls = {"n": 0, "texts": []}
        conv_checks = {"n": 0}

        async def _fake_run(update, context, user_text):
            ai_calls["n"] += 1
            ai_calls["texts"].append(user_text)

        def _fake_in_conv_true(update):
            conv_checks["n"] += 1
            return True

        def _fake_in_conv_false(update):
            conv_checks["n"] += 1
            return False

        real_run = bot_mod.run_ai_from_message
        real_in_conv = bot_mod._user_in_active_conversation
        bot_mod.run_ai_from_message = _fake_run
        try:
            bot_mod._user_in_active_conversation = _fake_in_conv_true
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert conv_checks["n"] >= 1, "must consult active-conversation gate"
            assert ai_calls["n"] == 0, (
                "AI ON + mid-conversation free text must NOT call run_ai_from_message"
            )

            # --- conversation finished (idle) → AI entry once ---
            bot_mod._user_in_active_conversation = _fake_in_conv_false
            before = ai_calls["n"]
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert ai_calls["n"] == before + 1, (
                "AI ON + idle free text must enter shipped AI path after command ends"
            )
            assert ai_calls["texts"][-1] == "netflix 999"

            # --- still ON: second idle message still routes (assistant stays on) ---
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert ai_calls["n"] == before + 2

            # --- toggle OFF while idle → no further AI ---
            update_user_ai_mode(db, user_id, False)
            db.commit()
            assert _get_ai_mode_sync(user_id) is False
            bot_mod._user_in_active_conversation = _fake_in_conv_false
            frozen = ai_calls["n"]
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert ai_calls["n"] == frozen, "AI OFF free text must not call AI entry"

            # --- re-enable: after command-like mid-flow still blocked, then idle OK ---
            update_user_ai_mode(db, user_id, True)
            db.commit()
            bot_mod._user_in_active_conversation = _fake_in_conv_true
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert ai_calls["n"] == frozen
            bot_mod._user_in_active_conversation = _fake_in_conv_false
            asyncio.run(bot_mod.free_text_ai_handler(_Upd(), None))
            assert ai_calls["n"] == frozen + 1
        finally:
            bot_mod.run_ai_from_message = real_run
            bot_mod._user_in_active_conversation = real_in_conv

        # --- Structural: slash commands + handler order in shipped main() ---
        src_path = os.path.join(os.path.dirname(__file__), "bot.py")
        src = open(src_path, encoding="utf-8").read()
        # Free-text filter must exclude commands so /list etc. never hit free_text_ai
        assert "MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_ai_handler)" in src
        # Free-text registration must come AFTER ConversationHandlers
        idx_conv_add = src.find("application.add_handler(conv_add)")
        idx_conv_task = src.find("application.add_handler(conv_task)")
        idx_conv_edit = src.find("application.add_handler(conv_edit)")
        idx_conv_settings = src.find("application.add_handler(conv_settings)")
        idx_free = src.find(
            "MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_ai_handler)"
        )
        assert min(idx_conv_add, idx_conv_task, idx_conv_edit, idx_conv_settings) > 0
        assert idx_free > idx_conv_add
        assert idx_free > idx_conv_task
        assert idx_free > idx_conv_edit
        assert idx_free > idx_conv_settings
        # CommandHandlers for slash surface still present (not replaced by free-text AI)
        for cmd in (
            "start", "help", "privacy", "list", "ai", "delete", "cancel",
        ):
            assert f'CommandHandler("{cmd}"' in src, f"missing CommandHandler({cmd})"
        for entry in (
            'CommandHandler("add", add_expense_command)',
            'CommandHandler("task", add_task)',
            'CommandHandler("edit", edit_expense)',
            'CommandHandler("settings", settings_command)',
        ):
            assert entry in src, f"missing conversation entry: {entry}"
            assert src.find(entry) < idx_free

        # Menu still exposes slash commands while AI mode feature exists
        menu = dict(BOT_COMMANDS)
        for name in (
            "ai", "add", "task", "list", "edit", "delete",
            "settings", "cancel", "privacy", "help",
        ):
            assert name in menu, f"BOT_COMMANDS missing {name}"
        assert "ai_mode" in src or "get_user_ai_mode" in src
        assert "should_route_free_text_to_ai" in src
        assert "_user_in_active_conversation" in src

        # Tracked conversations are the four slash flows that own free text mid-step
        assert "_TRACKED_CONVERSATION_HANDLERS.extend" in src
        assert "conv_settings" in src and "conv_add" in src
        assert "conv_task" in src and "conv_edit" in src

        print("[OK] AI-ON: mid-command free text blocked; idle routes; slash order intact")
    except Exception as e:
        print(f"[ERROR] AI-ON slash isolation test failed: {e}")
        db.rollback()
    finally:
        db.close()


def test_bot_command_menu():
    """BOT_COMMANDS obey Telegram's constraints and reference real handlers."""
    print("\nTesting bot command menu...")
    import re as _re
    from bot import BOT_COMMANDS, BOT_SHORT_DESCRIPTION
    seen = set()
    for name, desc in BOT_COMMANDS:
        assert _re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 1 <= len(desc) <= 256, (name, len(desc))
        assert name not in seen, f"duplicate command {name}"
        seen.add(name)
    # every menu command must have a registered handler (typo guard)
    for name in ("ai", "add", "task", "list", "edit", "delete", "settings", "cancel", "privacy", "help"):
        assert name in seen, f"expected {name} in menu"
    assert len(BOT_SHORT_DESCRIPTION) <= 120, len(BOT_SHORT_DESCRIPTION)  # Telegram short-desc cap
    # Menu describes toggle, not one-shot prompt
    ai_desc = dict(BOT_COMMANDS)["ai"].lower()
    assert "on or off" in ai_desc or "assistant" in ai_desc
    print(f"[OK] {len(BOT_COMMANDS)} menu commands valid")


def test_recur_anchor():
    """Recurrence anchor: pure compute_next_due_date math (scheduled grid,
    actual-day, catch-up, default coercion) + wiring through both mark-done
    paths (bot button + /ai) + snapshot/restore round-trip."""
    print("\nTesting recurrence anchor (scheduled vs actual)...")
    from datetime import date

    # --- pure function ---------------------------------------------------
    due = date(2026, 3, 10)
    # scheduled keeps the fixed grid regardless of when it's completed
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 9), 'scheduled') == date(2026, 3, 14)  # early
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 10), 'scheduled') == date(2026, 3, 14)  # on time
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 12), 'scheduled') == date(2026, 3, 14)  # slightly late
    # very late → roll forward whole periods until strictly after today
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 20), 'scheduled') == date(2026, 3, 22)
    print("[OK] scheduled: fixed grid + catch-up when long overdue")

    # actual restarts the cadence from the completion day
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 9), 'actual') == date(2026, 3, 13)
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 20), 'actual') == date(2026, 3, 24)
    print("[OK] actual: next = completion day + period")

    # monthly keeps the calendar (relativedelta month-end clamp)
    assert compute_next_due_date('month', None, date(2026, 1, 31), date(2026, 1, 31), 'scheduled') == date(2026, 2, 28)
    # NULL / unknown anchor behaves as the default (scheduled)
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 9), None) == date(2026, 3, 14)
    assert compute_next_due_date('custom', 4, due, date(2026, 3, 9), 'garbage') == date(2026, 3, 14)
    assert normalize_recur_anchor(None) == DEFAULT_RECUR_ANCHOR == RECUR_ANCHOR_SCHEDULED
    assert normalize_recur_anchor('actual') == RECUR_ANCHOR_ACTUAL
    print("[OK] default coercion (NULL/unknown → scheduled)")

    # apply_recur_anchor: shared edit-path policy — write only a valid mode on
    # a recurring record; ignore unknown/None/one-time (both edit paths agree).
    class _E:
        def __init__(self, period, anchor):
            self.period, self.recur_anchor = period, anchor
    e = _E('custom', RECUR_ANCHOR_SCHEDULED)
    assert apply_recur_anchor(e, RECUR_ANCHOR_ACTUAL) is True and e.recur_anchor == RECUR_ANCHOR_ACTUAL
    assert apply_recur_anchor(e, 'garbage') is False and e.recur_anchor == RECUR_ANCHOR_ACTUAL  # unknown left alone
    assert apply_recur_anchor(e, None) is False and e.recur_anchor == RECUR_ANCHOR_ACTUAL       # omitted left alone
    one = _E('none', RECUR_ANCHOR_SCHEDULED)
    assert apply_recur_anchor(one, RECUR_ANCHOR_ACTUAL) is False and one.recur_anchor == RECUR_ANCHOR_SCHEDULED  # one-time gated
    print("[OK] apply_recur_anchor edit policy (valid+recurring only)")

    # --- wiring through mark-done paths ----------------------------------
    db = SessionLocal()
    try:
        user_id = 700000021
        if not db.query(User).filter(User.id == user_id).first():
            db.add(User(id=user_id, timezone="Europe/Moscow"))
            db.commit()
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()

        today = today_in_tz("Europe/Moscow")
        due_future = today + timedelta(days=2)  # completed 2 days early
        sched = add_expense(
            db, user_id=user_id, expense_type='task', title="Sched", amount=0,
            next_payment_date=due_future, period='custom', period_days=4,
            recur_anchor=RECUR_ANCHOR_SCHEDULED,
        )
        actual = add_expense(
            db, user_id=user_id, expense_type='task', title="Actual", amount=0,
            next_payment_date=due_future, period='custom', period_days=4,
            recur_anchor=RECUR_ANCHOR_ACTUAL,
        )
        db.commit()
        sched_seq, actual_seq = sched.user_seq, actual.user_seq

        # /ai path (execute_mark_done) — message carries the computed next date
        r_sched = execute_function(user_id, "mark_expense_done", {"id": sched_seq}, user_tz="Europe/Moscow")
        r_actual = execute_function(user_id, "mark_expense_done", {"id": actual_seq}, user_tz="Europe/Moscow")
        assert r_sched["success"] and r_actual["success"], (r_sched, r_actual)
        assert (due_future + timedelta(days=4)).strftime('%d.%m.%y') in r_sched["message"], r_sched
        assert (today + timedelta(days=4)).strftime('%d.%m.%y') in r_actual["message"], r_actual
        print("[OK] /ai mark-done honours anchor (scheduled from due, actual from today)")

        # bot button path (_mark_as_paid_sync) — re-read the persisted date
        res = _mark_as_paid_sync(user_id, sched_seq)
        assert res["status"] == "done_recurring", res
        db2 = SessionLocal()
        try:
            reread = get_expense_by_user_seq(db2, user_id, sched_seq)
            # already advanced once to due_future+4 by the /ai call; scheduled
            # grid advances that by another period
            assert reread.next_payment_date == due_future + timedelta(days=8), reread.next_payment_date
        finally:
            db2.close()
        print("[OK] bot mark-paid honours scheduled anchor")

        # --- snapshot / restore round-trip -------------------------------
        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.commit()
        keeper = add_expense(
            db, user_id=user_id, expense_type='task', title="Keeper", amount=0,
            next_payment_date=today, period='custom', period_days=4,
            recur_anchor=RECUR_ANCHOR_ACTUAL,
        )
        db.commit()
        keeper_seq = keeper.user_seq
        backup_id = snapshot_user_data(db, user_id)
        db.commit()
        keeper.recur_anchor = RECUR_ANCHOR_SCHEDULED
        db.commit()
        restore_user_backup(db, user_id, backup_id)
        db.commit()
        restored = get_expense_by_user_seq(db, user_id, keeper_seq)
        assert restored.recur_anchor == RECUR_ANCHOR_ACTUAL, restored.recur_anchor
        print("[OK] recur_anchor survives snapshot/restore")

        db.query(Expense).filter(Expense.user_id == user_id).delete()
        db.query(UserBackup).filter(UserBackup.user_id == user_id).delete()
        db.commit()
    except Exception as e:
        print(f"[ERROR] recur anchor test failed: {e}")
        db.rollback()
    finally:
        db.close()


# =============================================================================
# Voice notes -> speech-to-text -> AI-mode free text
# =============================================================================

class _FakeSTTResponse:
    """Minimal httpx-like response. Tests mock only this HTTP boundary."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSTTClient:
    def __init__(self):
        self.calls = []
        self.is_closed = False
        self.response = _FakeSTTResponse(200, {"text": "netflix 999"})
        self.side_effect = None
        self.hang_seconds = 0

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.hang_seconds:
            await asyncio.sleep(self.hang_seconds)
        if self.side_effect is not None:
            raise self.side_effect
        return self.response


def _stt_posted_model(kwargs: dict):
    """Model string from the multipart form or the JSON body, whichever the
    shipped call uses."""
    data = kwargs.get("data") or {}
    if isinstance(data, dict) and data.get("model"):
        return data["model"]
    body = kwargs.get("json") or {}
    if isinstance(body, dict) and body.get("model"):
        return body["model"]
    return None


def _stt_auth_header(kwargs: dict):
    headers = kwargs.get("headers") or {}
    return headers.get("Authorization") or headers.get("authorization")


def test_voice_transcription_http_contract():
    """Shipped transcribe_audio: the /audio/transcriptions contract.

    Mocks only client.post (the HTTP boundary). Asserts the URL, the model,
    the Bearer header, a 200 transcript, and the typed empty/non-200/exception
    failures, plus the shared client + semaphore and wait_for (not
    asyncio.timeout, which needs Python 3.11).
    """
    print("\nTesting speech-to-text HTTP contract...")
    import inspect
    import ai_handler as ah

    try:
        assert OPENROUTER_TRANSCRIBE_URL.endswith("/audio/transcriptions")
        assert OPENROUTER_STT_MODEL
        assert OPENROUTER_STT_TIMEOUT_SECONDS > 0
        assert MAX_VOICE_FILE_BYTES >= 256 * 1024
        assert voice_audio_format("audio/ogg") == "ogg"
        assert voice_audio_format(None) == "ogg"
        assert voice_audio_format("audio/mpeg") == "mp3"
        assert voice_audio_format("audio/ogg; codecs=opus") == "ogg"
        assert voice_audio_format("application/nonsense") == "ogg"

        fake = _FakeSTTClient()
        real_get = ah._get_or_client
        ah._get_or_client = lambda: fake
        try:
            # Empty bytes: no HTTP at all, typed empty failure
            empty = asyncio.run(transcribe_audio(b"", "ogg"))
            assert empty.text is None and empty.error == "empty"
            assert fake.calls == []

            # Happy 200 JSON text
            fake.response = _FakeSTTResponse(200, {"text": "netflix 999 every month"})
            ok = asyncio.run(transcribe_audio(b"OggS-fake-opus", "ogg"))
            assert ok.error is None, ok
            assert ok.text == "netflix 999 every month"
            assert len(fake.calls) == 1
            url, kwargs = fake.calls[0]
            assert url == OPENROUTER_TRANSCRIBE_URL
            assert _stt_posted_model(kwargs) == OPENROUTER_STT_MODEL
            assert _stt_auth_header(kwargs) == f"Bearer {ah.OPENROUTER_API_KEY}"
            # The multipart filename carries the container, so Opus-in-Ogg from
            # Telegram is not sniffed as wav.
            files = kwargs.get("files") or {}
            file_tuple = files.get("file")
            assert file_tuple, "multipart must send a file field"
            assert file_tuple[0].endswith(".ogg"), file_tuple[0]
            assert file_tuple[1] == b"OggS-fake-opus"
            # The shared client's default timeout is the shorter chat-round
            # budget, so this request must state its own.
            assert kwargs.get("timeout") == OPENROUTER_STT_TIMEOUT_SECONDS
            print("[OK] STT POST url/model/auth/timeout + 200 transcript")

            # A whitespace-only transcript is a failure, not a silent success
            fake.calls.clear()
            fake.response = _FakeSTTResponse(200, {"text": "   \n"})
            blank = asyncio.run(transcribe_audio(b"OggS-blank", "ogg"))
            assert blank.text is None and blank.error == "empty"
            assert fake.calls, "HTTP still happens; empty is the result"
            print("[OK] blank transcript is a typed failure")

            # Non-200
            fake.calls.clear()
            fake.response = _FakeSTTResponse(400, text="bad request")
            http_err = asyncio.run(transcribe_audio(b"OggS-bad", "ogg"))
            assert http_err.text is None and http_err.error == "http"

            # Non-JSON body
            fake.calls.clear()
            fake.response = _FakeSTTResponse(200, payload=None, text="<html>")
            not_json = asyncio.run(transcribe_audio(b"OggS-html", "ogg"))
            assert not_json.text is None and not_json.error == "http"
            print("[OK] non-200 and non-JSON are typed http failures")

            # Transport exception
            fake.calls.clear()
            fake.side_effect = RuntimeError("network down")
            exc = asyncio.run(transcribe_audio(b"OggS-exc", "ogg"))
            assert exc.text is None and exc.error == "exception"
            fake.side_effect = None
            print("[OK] transport exception is a typed failure")

            # wait_for actually bounds a hung POST
            fake.calls.clear()
            saved_timeout = ah.OPENROUTER_STT_TIMEOUT_SECONDS
            ah.OPENROUTER_STT_TIMEOUT_SECONDS = 0.05
            fake.hang_seconds = 1.0
            try:
                hung = asyncio.run(transcribe_audio(b"OggS-hang", "ogg"))
            finally:
                ah.OPENROUTER_STT_TIMEOUT_SECONDS = saved_timeout
                fake.hang_seconds = 0
            assert hung.text is None and hung.error == "timeout"
            print("[OK] a hung STT call is bounded by asyncio.wait_for")
        finally:
            ah._get_or_client = real_get

        stt_src = inspect.getsource(ah.transcribe_audio)
        assert "_get_or_client" in stt_src
        assert "_OR_SEMAPHORE" in stt_src
        assert "asyncio.wait_for" in stt_src
        # Call form only - the docstring names asyncio.timeout as the 3.11 API
        # this project deliberately does not use.
        assert "asyncio.timeout(" not in stt_src
        assert "time.sleep(" not in stt_src
        assert "requests." not in stt_src
        chat_src = inspect.getsource(ah._post_openrouter)
        assert "_get_or_client" in chat_src and "_OR_SEMAPHORE" in chat_src
        print("[OK] STT reuses the shared client + semaphore")
    except Exception as e:
        print(f"[ERROR] STT HTTP contract test failed: {e}")


def test_voice_ai_handler_routing():
    """Voice handler: the same AI-mode / conversation / rate-limit gates as
    typed text; transcription only after those gates; the transcript feeds
    run_ai_from_message.

    Mocks Telegram's get_file and the STT client. Drives the shipped
    voice_ai_handler and transcribe_audio, not copies.
    """
    print("\nTesting voice handler routing + rate-limit-before-transcription...")
    init_db()
    migrate_db()
    db = SessionLocal()
    user_id = 990_101
    import inspect
    import time as _time
    import bot as bot_mod
    import ai_handler as ah
    from collections import deque as _deque

    class _Voice:
        def __init__(self, file_size=2048):
            self.file_id = "AwVOICEFAKE"
            self.file_size = file_size
            self.mime_type = "audio/ogg"
            self.duration = 2

    class _Chat:
        async def send_action(self, action=None, **kwargs):
            return None

    class _Msg:
        def __init__(self, file_size=2048):
            self.voice = _Voice(file_size=file_size)
            self.text = None
            self.document = None
            self.reply_to_message = None
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)

    class _Upd:
        def __init__(self, file_size=2048):
            self.message = _Msg(file_size=file_size)
            self.effective_user = type("U", (), {"id": user_id})()
            self.effective_chat = _Chat()

    class _TgFile:
        async def download_as_bytearray(self):
            downloads.append(1)
            return bytearray(b"OggS-voice-bytes")

    class _Bot:
        async def get_file(self, file_id):
            get_file_ids.append(file_id)
            return _TgFile()

    class _Ctx:
        def __init__(self):
            self.bot = _Bot()
            self.application = None

    get_file_ids = []
    downloads = []
    ai_calls = {"n": 0, "texts": [], "kwargs": []}

    async def _fake_run(update, context, user_text, already_rate_limited=False, **kwargs):
        ai_calls["n"] += 1
        ai_calls["texts"].append(user_text)
        ai_calls["kwargs"].append({"already_rate_limited": already_rate_limited, **kwargs})

    fake_client = _FakeSTTClient()

    real_run = bot_mod.run_ai_from_message
    real_in_conv = bot_mod._user_in_active_conversation
    real_get_client = ah._get_or_client
    saved_times = {k: _deque(v) for k, v in bot_mod._ai_call_times.items()}

    def _reset_quota():
        bot_mod._ai_call_times.pop(user_id, None)

    try:
        existing = db.query(User).filter(User.id == user_id).first()
        if existing:
            db.delete(existing)
            db.commit()
        db.add(User(id=user_id))
        db.commit()

        bot_mod.run_ai_from_message = _fake_run
        # bot.transcribe_audio is the same function object; it looks
        # _get_or_client up on the ai_handler module, which is patched here.
        ah._get_or_client = lambda: fake_client

        # --- AI OFF: no download, no transcription, no AI entry ---
        update_user_ai_mode(db, user_id, False)
        db.commit()
        bot_mod._user_in_active_conversation = lambda update: False
        _reset_quota()
        asyncio.run(bot_mod.voice_ai_handler(_Upd(), _Ctx()))
        assert get_file_ids == [], "AI OFF must not download a voice note"
        assert fake_client.calls == [], "AI OFF must not transcribe"
        assert ai_calls["n"] == 0, "AI OFF must not call run_ai_from_message"
        print("[OK] AI OFF ignores a voice note")

        # --- Mid conversation, AI ON: a notice, no transcription ---
        update_user_ai_mode(db, user_id, True)
        db.commit()
        bot_mod._user_in_active_conversation = lambda update: True
        blocked = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(blocked, _Ctx()))
        assert get_file_ids == []
        assert fake_client.calls == []
        assert ai_calls["n"] == 0
        assert blocked.message.replies, "mid-conversation voice must not go silent"
        assert "/cancel" in blocked.message.replies[0]
        print("[OK] mid-conversation voice blocked with a /cancel notice")

        # --- Per-minute quota is charged BEFORE transcription ---
        bot_mod._user_in_active_conversation = lambda update: False
        _reset_quota()
        now = _time.monotonic()
        bot_mod._ai_call_times[user_id] = _deque([now] * bot_mod.AI_RATE_LIMIT_PER_MINUTE)
        limited = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(limited, _Ctx()))
        assert get_file_ids == [], "over-quota must not getFile"
        assert fake_client.calls == [], "over-quota must not transcribe"
        assert ai_calls["n"] == 0
        assert limited.message.replies
        assert "wait a minute" in limited.message.replies[0].lower()
        print("[OK] the per-minute quota blocks transcription")

        # --- Per-day quota ---
        _reset_quota()
        bot_mod._ai_call_times[user_id] = _deque(
            [_time.monotonic() - 120] * bot_mod.AI_RATE_LIMIT_PER_DAY
        )
        day_hit = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(day_hit, _Ctx()))
        assert get_file_ids == []
        assert fake_client.calls == []
        assert ai_calls["n"] == 0
        assert day_hit.message.replies
        assert "daily ai limit" in day_hit.message.replies[0].lower()
        print("[OK] the per-day quota blocks transcription")

        # --- Size cap is enforced before the download ---
        _reset_quota()
        huge = _Upd(file_size=MAX_VOICE_FILE_BYTES + 1)
        asyncio.run(bot_mod.voice_ai_handler(huge, _Ctx()))
        assert get_file_ids == []
        assert fake_client.calls == []
        assert ai_calls["n"] == 0
        assert huge.message.replies
        assert "too large" in huge.message.replies[0].lower()
        print("[OK] an oversized voice note is rejected before getFile")

        # --- Happy path: the transcript is passed through verbatim ---
        _reset_quota()
        fake_client.calls.clear()
        fake_client.response = _FakeSTTResponse(200, {"text": "netflix 999 every month"})
        happy = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(happy, _Ctx()))
        assert get_file_ids == ["AwVOICEFAKE"], get_file_ids
        assert downloads == [1]
        assert fake_client.calls, "the happy path must actually transcribe"
        url, kwargs = fake_client.calls[-1]
        assert url == OPENROUTER_TRANSCRIBE_URL
        assert _stt_posted_model(kwargs) == OPENROUTER_STT_MODEL
        assert _stt_auth_header(kwargs) == f"Bearer {ah.OPENROUTER_API_KEY}"
        assert ai_calls["n"] == 1, ai_calls
        assert ai_calls["texts"][-1] == "netflix 999 every month"
        assert ai_calls["kwargs"][-1]["already_rate_limited"] is True
        print("[OK] AI ON idle voice transcribes and enters run_ai_from_message")

        # --- An empty transcript is an error, not an AI call ---
        _reset_quota()
        fake_client.calls.clear()
        fake_client.response = _FakeSTTResponse(200, {"text": ""})
        before = ai_calls["n"]
        empty_upd = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(empty_upd, _Ctx()))
        assert ai_calls["n"] == before
        assert empty_upd.message.replies
        assert "no speech" in empty_upd.message.replies[0].lower()
        print("[OK] an empty transcript does not call run_ai_from_message")

        # --- An STT HTTP error is an error, not an AI call ---
        _reset_quota()
        fake_client.response = _FakeSTTResponse(400, text="nope")
        err_upd = _Upd()
        asyncio.run(bot_mod.voice_ai_handler(err_upd, _Ctx()))
        assert ai_calls["n"] == before
        assert err_upd.message.replies
        assert "transcribe" in err_upd.message.replies[0].lower()
        print("[OK] an STT HTTP error does not call run_ai_from_message")

        # --- Double-charge guard: at 4/5 used, a voice note must still run ---
        bot_mod.run_ai_from_message = real_run
        or_calls = []

        async def _fake_or(user_message, uid, intent_text=None):
            or_calls.append(intent_text)
            return {
                "success": True,
                "message": "ok",
                "dialog_count": 1,
                "expense_ids": [],
                "cleanup": [],
            }

        async def _fake_postproc(app, result):
            return

        real_or = bot_mod.call_openrouter
        real_postproc = bot_mod._run_ai_post_processing
        bot_mod.call_openrouter = _fake_or
        bot_mod._run_ai_post_processing = _fake_postproc
        fake_client.response = _FakeSTTResponse(200, {"text": "task doctor tomorrow"})
        fake_client.calls.clear()
        try:
            _reset_quota()
            bot_mod._ai_call_times[user_id] = _deque(
                [_time.monotonic()] * (bot_mod.AI_RATE_LIMIT_PER_MINUTE - 1)
            )
            quota_upd = _Upd()
            asyncio.run(bot_mod.voice_ai_handler(quota_upd, _Ctx()))
            assert or_calls == ["task doctor tomorrow"], (
                "at 4/5 quota a voice note must still enter call_openrouter once; "
                "charging the rate limit twice would block the chat path"
            )
            assert not any("wait a minute" in r.lower() for r in quota_upd.message.replies)
            print("[OK] the voice rate limit is charged once, before transcription")
        finally:
            bot_mod.call_openrouter = real_or
            bot_mod._run_ai_post_processing = real_postproc

        # --- Structural: registration, order, no blocking calls ---
        src_path = os.path.join(os.path.dirname(__file__), "bot.py")
        bot_src = open(src_path, encoding="utf-8").read()
        assert "voice_ai_handler" in bot_src
        assert "MessageHandler(filters.VOICE, voice_ai_handler)" in bot_src
        idx_conv_settings = bot_src.find("application.add_handler(conv_settings)")
        idx_voice = bot_src.find("MessageHandler(filters.VOICE, voice_ai_handler)")
        assert idx_conv_settings > 0 and idx_voice > idx_conv_settings
        idx_free = bot_src.find(
            "MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_ai_handler)"
        )
        assert idx_voice > idx_free, "the voice handler sits with free text, after conversations"
        handler_src = inspect.getsource(bot_mod.voice_ai_handler)
        assert handler_src.find("_ai_rate_limit_check") < handler_src.find("get_file")
        assert handler_src.find("_ai_rate_limit_check") < handler_src.find("transcribe_audio")
        assert "already_rate_limited=True" in handler_src
        assert "time.sleep" not in handler_src
        assert "requests." not in handler_src
        assert bot_mod.transcribe_audio is ah.transcribe_audio
        print("[OK] main() registers filters.VOICE after the ConversationHandlers")
    except Exception as e:
        print(f"[ERROR] voice handler routing test failed: {e}")
        db.rollback()
    finally:
        bot_mod.run_ai_from_message = real_run
        bot_mod._user_in_active_conversation = real_in_conv
        ah._get_or_client = real_get_client
        bot_mod._ai_call_times.clear()
        bot_mod._ai_call_times.update(saved_times)
        db.close()


def test_reply_language_pinned_to_interface():
    """Every AI call restates the interface language.

    The static prompt already asks for English, but the input can arrive in
    another language - a forward, a quote, or a transcribed voice note - and
    models mirror their input. The live block is what keeps the reply in the
    bot's own language, so it must be attached to the system content of every
    call, not only mentioned in the prompt.
    """
    print("\nTesting the reply-language block...")
    import inspect
    import ai_handler as ah

    try:
        assert "USER INTERFACE LANGUAGE" in REPLY_LANGUAGE_CONTEXT
        assert "English" in REPLY_LANGUAGE_CONTEXT
        low = REPLY_LANGUAGE_CONTEXT.lower()
        assert "voice transcript" in low, "the voice case must be named explicitly"
        assert "forwarded" in low
        assert "do not switch" in low

        call_src = inspect.getsource(ah.call_openrouter)
        assert "REPLY_LANGUAGE_CONTEXT" in call_src, (
            "the block must be appended to system_content on every call"
        )
        # It rides next to the other live per-call state, not inside the static prompt.
        assert "REPLY_LANGUAGE_CONTEXT" not in ah.SYSTEM_PROMPT
        assert "time_context + REPLY_LANGUAGE_CONTEXT" in call_src
        assert "Responses must be in English" in ah.SYSTEM_PROMPT, (
            "the static instruction stays as the fallback"
        )
        print("[OK] the interface language is restated on every AI call")
    except Exception as e:
        print(f"[ERROR] reply-language test failed: {e}")


def test_settings_command_reentry():
    """A second /settings while the menu is still open must be accepted.

    /settings is a ConversationHandler whose waiting state only matches inline
    callbacks, and the menu stays in that state until a completing tap or
    /cancel. With PTB's default allow_reentry=False every later /settings is
    dropped in silence - the usual "let me open it again" path.
    """
    print("\nTesting /settings reentry while the menu is still open...")
    import warnings as _warnings
    from datetime import datetime as _dt, timezone as _tz
    from telegram import (
        Update as _Update, Message as _Message, User as _TGUser,
        Chat as _TGChat, MessageEntity as _ME,
    )
    from telegram.ext import (
        ConversationHandler as _CH, CommandHandler as _Cmd,
        CallbackQueryHandler as _CQ, MessageHandler as _MH, filters as _filters,
    )
    import bot as bot_module

    def _make_conv(allow_reentry: bool):
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            return _CH(
                entry_points=[_Cmd("settings", bot_module.settings_command)],
                states={
                    bot_module.SETTINGS_MENU: [_CQ(
                        bot_module.settings_callback, pattern="^(settings_|tzsel_|currsel_)",
                    )],
                    bot_module.SETTINGS_TZ_SEARCH: [_MH(
                        _filters.TEXT & ~_filters.COMMAND, bot_module.settings_tz_search,
                    )],
                },
                fallbacks=[_Cmd("cancel", bot_module.cancel)],
                allow_reentry=allow_reentry,
            )

    class _DummyBot:
        username = "testbot"
        defaults = None

    user_id = 990_010
    sent = []
    db = SessionLocal()
    try:
        tg_user = _TGUser(id=42, first_name="T", is_bot=False)
        chat = _TGChat(id=42, type="private")
        entity = _ME(type=_ME.BOT_COMMAND, offset=0, length=len("/settings"))
        message = _Message(
            message_id=1,
            date=_dt.now(_tz.utc),
            chat=chat,
            from_user=tg_user,
            text="/settings",
            entities=(entity,),
        )
        dummy = _DummyBot()
        message.set_bot(dummy)
        update = _Update(update_id=1, message=message)
        update.set_bot(dummy)
        key = (chat.id, tg_user.id)

        stuck = _make_conv(allow_reentry=False)
        stuck._conversations[key] = bot_module.SETTINGS_MENU
        assert stuck.check_update(update) is None, (
            "without allow_reentry a second /settings must be the silent drop"
        )

        fixed = _make_conv(allow_reentry=True)
        fixed._conversations[key] = bot_module.SETTINGS_MENU
        check = fixed.check_update(update)
        assert check is not None, "allow_reentry=True must accept a second /settings"
        _state, _key, handler, _inner = check
        assert handler.commands == {"settings"}, handler.commands

        # The shipped registration must actually carry the flag.
        src_path = os.path.join(os.path.dirname(__file__), "bot.py")
        bot_src = open(src_path, encoding="utf-8").read()
        conv_chunk = bot_src[
            bot_src.find("conv_settings = ConversationHandler("):
            bot_src.find("application.add_handler(conv_settings)")
        ]
        assert conv_chunk, "conv_settings registration not found"
        assert "allow_reentry=True" in conv_chunk, (
            "the shipped /settings conversation must set allow_reentry"
        )

        # A first open must still render (a crash here would look identical to
        # the silent drop above).
        init_db()
        migrate_db()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.add(User(id=user_id))
        db.commit()

        class _Msg:
            async def reply_text(self, text, **kwargs):
                sent.append(text)

        class _Open:
            def __init__(self):
                self.effective_user = type("U", (), {"id": user_id})()
                self.message = _Msg()
                self.callback_query = None

        result = asyncio.run(bot_module.settings_command(_Open(), None))
        assert result == bot_module.SETTINGS_MENU, result
        assert sent and "Settings" in sent[-1], sent
        print("[OK] a second /settings re-enters; the first open still renders")
    except Exception as e:
        print(f"[ERROR] /settings reentry: {e}")
        db.rollback()
    finally:
        try:
            db.rollback()
            db.query(User).filter(User.id == user_id).delete()
            db.commit()
        except Exception:
            db.rollback()
        db.close()


TESTS = [
    test_database,
    test_ai_multiline_title_note,
    test_ai_mode_toggle_and_routing,
    test_ai_on_slash_commands_isolated_from_free_text,
    test_voice_transcription_http_contract,
    test_voice_ai_handler_routing,
    test_reply_language_pinned_to_interface,
    test_settings_command_reentry,
    test_bot_command_menu,
    test_user_seq_per_user,
    test_scheduler_logic,
    test_timezone_utils,
    test_timezone_recalculation,
    test_tz_rollback,
    test_reminder_engine_stages,
    test_reminder_plan_model,
    test_reminder_plan_slots,
    test_reminder_plan_ui,
    test_reminder_engine_compute,
    test_reminder_engine_backfill,
    test_recompute_hooks,
    test_same_day_ai_reschedule_rearms_exact_time,
    test_d_day_send_allowed,
    test_list_immediate_exhausted_due_day,
    test_task_reminder_buttons_hidden_for_future,
    test_record_reminder_sent,
    test_recompute_stages_for_window,
    test_plan_engine_custom_plan_db,
    test_reminder_hour_setting,
    test_quiet_hours,
    test_reminder_reschedule_buttons,
    test_legacy_snooze_migration,
    test_edit_plan_buttons,
    test_edit_menu_delete_option,
    test_recur_anchor,
    test_ai_set_reminder_plan,
    test_ai_create_task_returns_user_seq,
    test_action_intent_not_triggered_by_forward_context,
    test_ai_response_fallbacks,
    test_ai_mixed_modify_tool_aggregation,
    test_telegram_message_context,
    test_currency_conversion,
    test_detect_currency_default,
    test_list_sort_preference_and_order,
    test_user_default_currency_migration,
    test_user_backup_snapshot_and_list,
    test_user_backup_restore_happy_path,
    test_user_backup_restore_ownership_isolation,
    test_user_backup_restore_unknown_id,
    test_user_backup_pruning,
    test_user_backup_pruning_by_age,
    test_purge_old_inactive_expenses,
    test_ai_handler_backup_dispatch,
    test_ics_parse_basic,
    test_ics_parse_outlook_custom_tzid,
    test_ics_rrule_period_mapping,
    test_add_ics_events_sync,
]


def _run_test(fn) -> bool:
    """Run one test function and report pass/fail honestly.

    Most test functions here catch their own exceptions internally (to keep
    running and print a readable [ERROR] line) rather than letting them
    propagate — which used to mean the whole script always exited 0, even
    with failures, so it could never gate a deploy/CI even if wired up. A
    test now counts as failed if it raises OR if it printed an [ERROR] line
    (the convention every test already follows), without changing any of
    the individual test bodies.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except Exception as e:
        print(buf.getvalue(), end="")
        print(f"[ERROR] {fn.__name__} raised an uncaught exception: {e}")
        return False

    output = buf.getvalue()
    print(output, end="")
    return "[ERROR]" not in output


if __name__ == "__main__":
    try:
        failures = [fn.__name__ for fn in TESTS if not _run_test(fn)]

        print("\n" + "=" * 50)
        if failures:
            print(f"[FAILED] {len(failures)}/{len(TESTS)} test group(s): {', '.join(failures)}")
            print("=" * 50)
            sys.exit(1)

        print(f"[SUCCESS] All {len(TESTS)} test groups passed")
        print("=" * 50)
        print("To run the bot, you need to:")
        print("1. Get a bot token from @BotFather")
        print("2. Set TELEGRAM_BOT_TOKEN environment variable")
        print("3. Run: python bot.py")
        print("=" * 50)
    finally:
        try:
            os.remove(_TEST_DB_PATH)
        except OSError:
            pass
