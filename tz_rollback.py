"""Per-user snapshot before timezone change — restore timezone + expense dates."""

import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone

import config
from database import Expense, User, get_user_timezone
from reminder_engine import recompute_for_user

logger = logging.getLogger(__name__)

TZ_ROLLBACK_DIR = os.path.join(config.DATA_DIR, "tz_rollback")

# Users almost never invoke the rollback feature (timezone is normally set
# once at onboarding), so snapshots that are never restored become permanent
# orphans — the only other place a file is deleted is a successful
# restore_tz_change(). Anything older than this is swept by
# cleanup_old_tz_rollback_snapshots (wired into the scheduler).
TZ_ROLLBACK_MAX_AGE_DAYS = 30


def _rollback_path(user_id: int) -> str:
    return os.path.join(TZ_ROLLBACK_DIR, f"{user_id}.json")


def save_tz_change_snapshot(db, user_id: int, db_backup_file: str | None) -> dict:
    """Save user timezone and active expense dates before TZ change."""
    os.makedirs(TZ_ROLLBACK_DIR, exist_ok=True)
    tz = get_user_timezone(db, user_id)
    expenses = db.query(Expense).filter(
        Expense.user_id == user_id,
        Expense.is_active == True,
    ).all()
    snapshot = {
        "user_id": user_id,
        "timezone": tz,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "db_backup_file": db_backup_file,
        "expenses": [
            {
                "id": e.id,
                "next_payment_date": e.next_payment_date.isoformat(),
                "next_reminder_at": (
                    e.next_reminder_at.isoformat() if e.next_reminder_at else None
                ),
                "reminder_stage": e.reminder_stage,
            }
            for e in expenses
        ],
    }
    path = _rollback_path(user_id)
    fd, tmp_path = tempfile.mkstemp(dir=TZ_ROLLBACK_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic — a killed process can't leave a partial file
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    logger.info(
        "TZ rollback snapshot saved for user %s: tz=%s, expenses=%d, db_backup=%s",
        user_id, tz, len(snapshot["expenses"]), db_backup_file,
    )
    return snapshot


def get_tz_rollback_info(user_id: int) -> dict | None:
    """Returns None both when there's no snapshot and when the file is
    corrupt (e.g. a process killed mid-write before atomic writes were
    added) — callers already treat None as "no rollback available", so a
    bad file degrades gracefully instead of crashing /settings."""
    path = _rollback_path(user_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Corrupt tz_rollback snapshot for user %s: %s", user_id, e)
        return None


def cleanup_old_tz_rollback_snapshots(max_age_days: int = TZ_ROLLBACK_MAX_AGE_DAYS) -> int:
    """Delete snapshot files older than max_age_days. Returns count removed."""
    if not os.path.isdir(TZ_ROLLBACK_DIR):
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    removed = 0
    for name in os.listdir(TZ_ROLLBACK_DIR):
        path = os.path.join(TZ_ROLLBACK_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError as e:
            logger.warning("Failed to clean up tz_rollback file %s: %s", name, e)
    if removed:
        logger.info("Cleaned up %d expired tz_rollback snapshot(s)", removed)
    return removed


def has_tz_rollback(user_id: int) -> bool:
    return os.path.exists(_rollback_path(user_id))


def restore_tz_change(db, user_id: int) -> dict:
    """Restore timezone and expense dates from last snapshot; remove snapshot."""
    snapshot = get_tz_rollback_info(user_id)
    if not snapshot:
        raise ValueError("No rollback snapshot found")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError("User not found")

    user.timezone = snapshot["timezone"]
    restored = 0
    for item in snapshot.get("expenses", []):
        expense = db.query(Expense).filter(
            Expense.id == item["id"],
            Expense.user_id == user_id,
        ).first()
        if not expense:
            continue
        if item.get("next_payment_date"):
            expense.next_payment_date = date.fromisoformat(item["next_payment_date"])
        raw_reminder_at = item.get("next_reminder_at")
        if raw_reminder_at:
            expense.next_reminder_at = datetime.fromisoformat(raw_reminder_at)
        else:
            expense.next_reminder_at = None
        expense.reminder_stage = item.get("reminder_stage")
        restored += 1

    recompute_for_user(db, user_id)

    path = _rollback_path(user_id)
    os.remove(path)
    logger.info(
        "TZ rollback restored for user %s: tz=%s, expenses=%d",
        user_id, snapshot["timezone"], restored,
    )
    return {
        "timezone": snapshot["timezone"],
        "expenses_restored": restored,
        "db_backup_file": snapshot.get("db_backup_file"),
        "saved_at": snapshot.get("saved_at"),
    }
