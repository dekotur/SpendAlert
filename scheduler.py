import asyncio
import html
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import case
from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

import config
import monitoring
from ai_handler import daily_backup
from currency import update_exchange_rate
from database import Expense, get_db, get_user_timezone, utcnow_naive, purge_old_inactive_expenses
from reminder_engine import (
    STAGE_PRIORITY,
    d_day_send_allowed,
    record_reminder_sent,
    recompute_stages_for_window,
    slot_due_for_send,
)
from reminder_plan import plan_from_json, slot_for_day, slot_times
from tz_rollback import cleanup_old_tz_rollback_snapshots
from utils import now_in_tz, today_in_tz, plural_days, MSK

logger = logging.getLogger(__name__)

scheduler = None
_send_semaphore = asyncio.Semaphore(10)
DUE_BATCH_LIMIT = 200


async def safe_delete_message(bot, chat_id: int, message_id: int) -> bool:
    """Safely delete a Telegram message."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message to delete not found" in msg or "message can't be deleted" in msg:
            return False
        logger.warning(f"Unexpected BadRequest deleting message {message_id}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")
        return False


def _fetch_reminder_log_rows_sync(expense_id: int, max_logs: int) -> list[tuple[int, int | None]]:
    from database import ReminderLog

    with get_db() as db:
        rows = db.query(ReminderLog.id, ReminderLog.message_id).filter(
            ReminderLog.expense_id == expense_id,
            # v3 slot keys + legacy v2 stage names (old rows predating the
            # plan engine are still cleaned up the same way)
            ReminderLog.stage.in_([
                'n0', 'n1', 'n2', 'n3', 'd', 'f',
                'd_day', 'overdue', '1_day', '2_days', '3_days',
            ])
        ).order_by(ReminderLog.sent_at.desc()).limit(max_logs).all()
        return list(rows)


def _delete_reminder_log_rows_sync(log_ids: list[int]) -> None:
    from database import ReminderLog

    with get_db() as db:
        db.query(ReminderLog).filter(ReminderLog.id.in_(log_ids)).delete(synchronize_session=False)


async def cleanup_reminder_messages(application, user_id: int, expense_id: int, max_logs: int = 15):
    """Delete consecutive reminder messages for an expense and prune ReminderLog rows.

    Two short DB sessions (read, then delete) instead of one held open across
    up to `max_logs` sequential Telegram calls; the deletes themselves run
    concurrently via asyncio.gather rather than one at a time.
    """
    try:
        rows = await asyncio.to_thread(_fetch_reminder_log_rows_sync, expense_id, max_logs)
        deletable = [(log_id, message_id) for log_id, message_id in rows if message_id]
        if not deletable:
            return

        results = await asyncio.gather(
            *[safe_delete_message(application.bot, user_id, message_id) for _, message_id in deletable]
        )

        deleted_ids = [log_id for (log_id, _), ok in zip(deletable, results) if ok]
        if deleted_ids:
            await asyncio.to_thread(_delete_reminder_log_rows_sync, deleted_ids)
            logger.info(f"Cleaned up {len(deleted_ids)} reminder messages for expense {expense_id}")
    except Exception as e:
        logger.warning(f"Reminder cleanup failed for expense {expense_id}: {e}")


def _sweep_sync():
    with get_db() as db:
        recompute_stages_for_window(db)


async def _run_reminder_sweep():
    await asyncio.to_thread(_sweep_sync)


async def setup_scheduler(application, run_check=True):
    """Initialize the scheduler for reminders."""
    global scheduler

    # Safe to call every time setup_scheduler runs (cheap, idempotent) — this
    # is the only point guaranteed to run inside the actual PTB event loop
    # (post_init), so it's where monitoring can safely capture a loop
    # reference for cross-thread alerts.
    monitoring.register_alert_bot(application, asyncio.get_running_loop())

    if scheduler is None:
        # The default Python executor can be too small for concurrent DB/file
        # I/O. This is the first guaranteed place with a running event loop.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=config.DB_THREAD_POOL_WORKERS)
        )

        scheduler = AsyncIOScheduler(timezone=MSK)

        scheduler.add_job(
            fire_due_reminders,
            trigger=CronTrigger(minute='*'),
            args=[application],
            id='fire_reminders',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.add_job(
            _run_reminder_sweep,
            trigger=IntervalTrigger(hours=1),
            id='reminder_window_sweep',
            replace_existing=True,
        )

        scheduler.add_job(
            daily_backup,
            trigger=IntervalTrigger(hours=24),
            id='daily_backup',
            replace_existing=True,
        )

        scheduler.add_job(
            update_exchange_rate,
            trigger=IntervalTrigger(hours=6),
            id='update_exchange_rate',
            replace_existing=True,
        )

        scheduler.add_job(
            cleanup_old_tz_rollback_snapshots,
            trigger=IntervalTrigger(hours=24),
            id='cleanup_tz_rollback',
            replace_existing=True,
        )

        scheduler.add_job(
            purge_old_inactive_expenses,
            trigger=IntervalTrigger(hours=24),
            id='purge_old_inactive_expenses',
            replace_existing=True,
        )

        scheduler.add_job(
            monitoring.check_disk_space,
            trigger=IntervalTrigger(hours=1),
            id='check_disk_space',
            replace_existing=True,
        )

        scheduler.add_job(
            monitoring.check_bot_disk_budget,
            trigger=IntervalTrigger(hours=1),
            id='check_bot_disk_budget',
            replace_existing=True,
        )

        scheduler.start()
        logger.info("Scheduler started (reminder v2: 1m cron + hourly sweep)")

    if run_check:
        await fire_due_reminders(application)
        await update_exchange_rate()


async def trigger_reminder_check(application, specific_expense_id=None):
    """Fire due reminders for one expense or all due items."""
    await fire_due_reminders(application, specific_expense_id)


async def fire_due_reminders(application, specific_expense_id=None):
    """Send reminders where next_reminder_at <= now (naive UTC)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    due_items: list[tuple[int, str, int, int]] = []

    with get_db() as db:
        query = db.query(Expense).filter(
            Expense.is_active == True,
            Expense.next_reminder_at.isnot(None),
            Expense.next_reminder_at <= now_utc,
        )
        if specific_expense_id:
            query = query.filter(Expense.id == specific_expense_id)

        # Urgent rows first, so a burst of fresh far-out waves (e.g. many
        # subscriptions renewing on the 1st) can't push overdue/due-day out
        # of the DUE_BATCH_LIMIT for this minute. The reminder_stage mirror
        # is an exact days-until proxy (slot keys are not: their index
        # depends on how many waves the plan has); far pre-due waves have
        # stage NULL → lowest priority, which is correct.
        stage_priority = case(
            STAGE_PRIORITY,
            value=Expense.reminder_stage,
            else_=5,
        )
        expenses = (
            query.options(joinedload(Expense.user))
            .order_by(stage_priority, Expense.next_reminder_at)
            .limit(DUE_BATCH_LIMIT)
            .all()
        )

        for expense in expenses:
            try:
                tz = get_user_timezone(db, expense.user_id)
                now_local = now_in_tz(tz)
                today = today_in_tz(tz)
                days_until = (expense.next_payment_date - today).days
                # Derive the active slot from the current plan + due date;
                # stale schedules self-heal in place.
                slot = slot_due_for_send(expense, tz, db)
                if not slot:
                    continue
                if slot == "d" and not d_day_send_allowed(expense, tz, now_local):
                    continue
                due_items.append((expense.id, slot, days_until, expense.user_id))
            except Exception as e:
                logger.error(
                    "Error preparing reminder for expense %s: %s",
                    getattr(expense, "id", "?"), e,
                )

    for expense_id, slot, days_until, user_id in due_items:
        try:
            with get_db() as db:
                expense = (
                    db.query(Expense)
                    .options(joinedload(Expense.user))
                    .filter(Expense.id == expense_id, Expense.is_active == True)
                    .first()
                )
                if not expense or not expense.next_reminder_at or expense.next_reminder_at > now_utc:
                    continue
                # Re-gate against fresh state: between phase 1 and here a
                # concurrent sender (/list immediate) may have consumed the
                # slot's remaining sends — next_reminder_at alone doesn't
                # show that for a just-exhausted finite slot.
                tz = get_user_timezone(db, expense.user_id)
                if slot_due_for_send(expense, tz, db) != slot:
                    continue
                # Copy scalars before session closes (avoid DetachedInstanceError on send)
                expense_snapshot = {
                    "id": expense.id,
                    "user_seq": expense.user_seq,
                    "user_id": expense.user_id,
                    "expense_type": expense.expense_type,
                    "title": expense.title,
                    "amount": expense.amount,
                    "currency": expense.currency,
                    "next_payment_date": expense.next_payment_date,
                    "reminder_time": expense.reminder_time,
                }

            async with _send_semaphore:
                status, value = await send_reminder(application, expense_snapshot, slot, days_until)

            with get_db() as db:
                if status == "ok":
                    sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    record_reminder_sent(db, expense_id, slot, value, sent_at)
                elif status == "forbidden":
                    db.query(Expense).filter(Expense.id == expense_id).update(
                        {"is_active": False, "next_reminder_at": None, "deactivated_at": utcnow_naive()}
                    )
                    logger.info(f"Deactivated expense {expense_id}: user blocked the bot")
                elif status == "retry":
                    retry_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=value)
                    db.query(Expense).filter(Expense.id == expense_id).update(
                        {"next_reminder_at": retry_at}
                    )
                # "fail" — leave next_reminder_at untouched, retried on the next cron tick
        except Exception as e:
            logger.error(f"Error sending reminder for expense {expense_id}: {e}")


def _list_immediate_send_allowed(slot: str, slot_cfg: dict, sends: int) -> bool:
    """Whether an enabled d/f slot may be sent by an explicit /list call.

    Opening /list is a user-initiated request, so an enabled due-day slot is
    useful as a manual re-show even after its scheduled quota was consumed.
    Overdue reminders keep their configured quota to avoid reviving an
    intentionally exhausted backlog on every /list call.
    """
    return slot == "d" or sends < slot_times(slot_cfg)


async def send_list_immediate_reminders(application, user_id: int):
    """Send due-day/overdue reminders from /list without rate-limit.

    Respects disabled d/f slots and the due-day clock gate.
    An enabled due-day slot is re-shown on every explicit /list call even if
    its scheduled send quota is exhausted; overdue slots still honor times.
    """
    user_tz = None
    with get_db() as db:
        user_tz = get_user_timezone(db, user_id)

    now_local = now_in_tz(user_tz)
    today = today_in_tz(user_tz)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    with get_db() as db:
        due_today = db.query(Expense).filter(
            Expense.user_id == user_id,
            Expense.is_active == True,
            Expense.next_payment_date <= today,
        ).options(joinedload(Expense.user)).all()

        for expense in due_today:
            # Refresh + per-item commit below keep this loop honest against
            # the minute cron running in parallel: without them the whole
            # loop reads one stale snapshot and both paths can pass the
            # exhaustion gate for the same slot (duplicate ping).
            db.refresh(expense)
            days_until = (expense.next_payment_date - today).days
            if days_until > 0:
                continue
            plan = plan_from_json(expense.reminder_plan)
            active = slot_for_day(plan, days_until)
            if not active:
                continue  # d/f disabled in this plan → no immediate ping
            slot, slot_cfg = active
            sends = (expense.reminder_slot_sends or 0) if expense.reminder_slot == slot else 0
            if not _list_immediate_send_allowed(slot, slot_cfg, sends):
                continue  # exhausted overdue slot → keep the configured silence
            if slot == "d" and not d_day_send_allowed(expense, user_tz, now_local):
                continue
            expense_id = expense.id
            try:
                status, value = await send_reminder(application, expense, slot, days_until)
                if status == "ok":
                    record_reminder_sent(db, expense_id, slot, value, now_utc)
                    logger.info(f"Sent immediate reminder for expense {expense_id}, slot={slot}")
                elif status == "forbidden":
                    expense.is_active = False
                    expense.next_reminder_at = None
                    expense.deactivated_at = utcnow_naive()
                    logger.info(f"Deactivated expense {expense_id}: user blocked the bot (immediate send)")
                elif status == "retry":
                    expense.next_reminder_at = now_utc + timedelta(seconds=value)
                # "fail" — leave state untouched, retried on the next cron tick
                db.commit()  # per item, so the cron's re-gate sees it immediately
            except Exception as e:
                logger.error(f"Failed to send immediate reminder for expense {expense_id}: {e}")


def _reminder_header(is_task: bool, days_until: int) -> str:
    """Header line by urgency. Plan slots allow arbitrary pre-due offsets
    (14 days out, 90 days out), so the text is derived from days_until instead
    of a fixed stage name."""
    kind = "Task" if is_task else "Payment"
    if days_until < 0:
        return f"🔴 <b>{kind} · overdue</b>"
    if days_until == 0:
        return f"🔴 <b>{kind} · today</b>"
    if days_until == 1:
        return f"🚨 <b>{kind} · tomorrow</b>"
    if days_until == 2:
        return f"⚠️ <b>{kind} · in 2 days</b>"
    return f"📅 <b>{kind} · in {days_until} {plural_days(days_until)}</b>"


def _reminder_reply_markup(is_task: bool, pay_id: int, days_until: int):
    """Build reminder actions; future tasks are not actionable yet."""
    if is_task and days_until > 0:
        return None

    button_label = "✅ Done" if is_task else "✅ Paid"
    keyboard = [
        [InlineKeyboardButton(button_label, callback_data=f"pay_{pay_id}")],
        [
            InlineKeyboardButton("↪️ +3 hours", callback_data=f"move_{pay_id}_3h"),
            InlineKeyboardButton("↪️ Tomorrow", callback_data=f"move_{pay_id}_1d"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_reminder(application, expense, slot, days_until):
    """Send reminder message to user (HTML formatted for consistency).

    ``expense`` may be an ORM instance (session open) or a scalar dict from
    ``fire_due_reminders`` after its DB session has closed.

    Returns a typed ``(status, value)`` pair the caller uses to decide what
    to do next — previously any failure (including the user having blocked
    the bot, or Telegram flood-control) looked identical to a successful
    send, so a blocked user's expense kept "sending" every 2h forever and a
    429 was silently treated as delivered:
      ("ok", message_id)   — delivered
      ("forbidden", None)  — user blocked the bot / deleted account; caller
                              should deactivate the expense, not retry
      ("retry", seconds)   — Telegram flood control; caller should reschedule
                              without advancing the stage or logging a send
      ("fail", None)       — other error; caller leaves state untouched and
                              retries on the next cron tick
    """
    from database import User

    def _get(field):
        if isinstance(expense, dict):
            return expense[field]
        return getattr(expense, field)

    chat_id = _get("user_id")
    eid = _get("id")
    try:
        with get_db() as db:
            user = db.query(User).filter(User.id == chat_id).first()
            if not user:
                return ("fail", None)
        is_task = _get("expense_type") == 'task'

        currency = _get("currency")
        from currency import currency_symbol
        cur_sym = currency_symbol(currency)
        reminder_time = _get("reminder_time")
        time_note = f"\n⏰ {reminder_time}" if reminder_time else ""

        title_html = f"<b>{html.escape(str(_get('title')))}</b>"
        date_str = _get("next_payment_date").strftime('%d.%m.%y')
        pay_id = _get("user_seq")

        header = _reminder_header(is_task, days_until)
        body = title_html
        if not is_task:
            body += f"\n💰 {_get('amount')} {cur_sym}"
        if days_until == 0:
            body += time_note  # due today — the date adds nothing, an explicit time does
        elif days_until < 0:
            body += f"\nDate: {date_str}"
        else:
            body += f"\n{date_str}"
        message = f"{header}\n\n{body}"

        reply_markup = _reminder_reply_markup(is_task, pay_id, days_until)

        sent_message = await application.bot.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        logger.info(f"Sent {slot} reminder for expense user_seq={pay_id} internal_id={eid}")
        return ("ok", sent_message.message_id)

    except Forbidden:
        logger.info(f"User {chat_id} blocked the bot — deactivating expense {eid}")
        return ("forbidden", None)
    except RetryAfter as e:
        retry_after = int(e.retry_after)
        logger.warning(f"Flood control sending reminder for expense {eid}: retry after {retry_after}s")
        return ("retry", retry_after)
    except Exception as e:
        logger.error(f"Failed to send reminder for expense {eid}: {e}")
        return ("fail", None)


def stop_scheduler():
    """Stop the scheduler."""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
