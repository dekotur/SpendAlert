import asyncio
import html
import json
import logging
import os
import time
from collections import deque, OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()  # Load .env for local development
except ImportError:
    pass  # python-dotenv not installed — rely on system environment variables

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, BotCommand
from telegram.constants import ChatAction
from telegram.error import BadRequest, InvalidToken
from telegram.ext import (
    Application, CommandHandler, CallbackContext,
    CallbackQueryHandler, MessageHandler, filters,
    ConversationHandler, Defaults, AIORateLimiter, BaseUpdateProcessor,
)
from database import (
    User, Expense, AISession, get_db, get_user_timezone, update_user_timezone,
    get_user_default_currency, update_user_default_currency,
    get_user_list_sort, update_user_list_sort,
    get_user_reminder_hour, update_user_reminder_hour,
    get_user_quiet_hours, update_user_quiet_hours,
    get_user_ai_mode, toggle_user_ai_mode,
    LIST_SORT_DATE, LIST_SORT_ID, LIST_SORT_MODES, DEFAULT_LIST_SORT,
    RECUR_ANCHOR_SCHEDULED, RECUR_ANCHOR_ACTUAL,
    normalize_recur_anchor, apply_recur_anchor,
    add_expense, get_expense_by_user_seq, utcnow_naive,
)
from scheduler import (
    setup_scheduler, trigger_reminder_check, cleanup_reminder_messages,
    send_list_immediate_reminders,
)
from reminder_engine import recompute_after_mutation
from reminder_plan import (
    cycle_ui, plan_from_json, plan_from_ui, plan_summary, plan_to_json,
    slot_for_day, slot_times, ui_all_off, ui_day_only, ui_default, ui_from_plan, ui_summary,
    UI_EXTRA_OFFSET_MIN, UI_EXTRA_OFFSET_MAX, UI_FIXED_OFFSETS,
)
from currency import (
    get_exchange_rates, currency_symbol, currency_label,
    SETTINGS_CURRENCIES, convert_amount,
)
from ai_handler import (
    call_openrouter, backup_database,
    transcribe_audio, voice_audio_format, MAX_VOICE_FILE_BYTES,
)
import ics_import
import monitoring
from tz_rollback import (
    save_tz_change_snapshot, get_tz_rollback_info, restore_tz_change,
)
from utils import (
    parse_date, get_period_str, plural_days, compute_next_due_date,
    now_in_tz, today_in_tz, to_user_tz,
    search_timezones, format_tz_current, POPULAR_TIMEZONES,
    build_ai_user_message, effective_conversation_text, has_telegram_message_context,
)
import config  # ensures logging + directories are set up

logger = logging.getLogger(__name__)

TYPING_REFRESH_SECONDS = 4

# Privacy copy — one source for /start, /help, /privacy, first free-text AI.
# Corporate tone: what we process, how protected, third parties. No "other users
# don't see you" (obvious) and no cynical "not zero-knowledge" disclaimers.
PRIVACY_ONBOARD_BLOCK = (
    "🔒 <b>Your data</b>\n"
    "I store your payments, tasks, time zone and currency.\n"
    "Titles and amounts are encrypted on the server.\n"
    "When the AI assistant is on (/ai), your text and your record list "
    "are sent to an external AI service.\n"
    "Details: /privacy.\n"
    "By using the bot you agree to this."
)

PRIVACY_FULL = (
    "🔒 <b>Data processing policy</b>\n\n"
    "<b>1. What is processed</b>\n"
    "▫️ payments and tasks (title, amount, date, recurrence)\n"
    "▫️ time zone and currency\n"
    "▫️ your Telegram id\n"
    "▫️ a short AI dialogue history (up to 6 turns) when the assistant is on\n"
    "▫️ internal snapshots so a change can be undone\n\n"
    "<b>2. Why</b>\n"
    "Tracking obligations, sending reminders, answering you, storing settings.\n\n"
    "<b>3. Protection</b>\n"
    "Titles and amounts are stored encrypted on the server.\n\n"
    "<b>4. Third parties</b>\n"
    "▫️ with the AI assistant on (/ai): your free text and the list of "
    "records go to an external AI service\n"
    "▫️ Telegram: delivery of messages and reminders\n\n"
    "⚠️ <b>Consent</b>\n"
    "By continuing to use the bot (commands, messages, AI assistant) "
    "you confirm that you have read this policy and agree "
    "to the processing described above.\n"
    "If you disagree, stop using the bot and do not send it your data.\n\n"
    "Full command list: /help"
)

PRIVACY_AI_LINE = (
    "⚠️ With the AI assistant on, your text and your record list "
    "are sent to an external AI service."
)

# User-facing help — /ai is permanent assistant ON/OFF; free text (and voice
# notes) when ON. Plain language on purpose: this is most readers' first
# contact, so it says "write as in a normal chat" rather than naming internal
# concepts like free text or a toggle. The assistant is off until the reader
# turns it on (users.ai_mode defaults to False), so every "just write" line
# stays attached to the /ai that enables it.
HELP_TEXT = (
    "📖 <b>Help</b>\n\n"
    "I remind you about payments and tasks.\n\n"
    "<b>AI assistant</b> - /ai turns it on or off. It starts off.\n"
    "While it is <b>on</b>, write as in a normal chat. For example:\n"
    "<code>netflix 999 every month</code>\n"
    "<code>task doctor tomorrow</code>\n"
    "<code>what is coming up</code>\n"
    "<code>paid 2</code>  - 2 is the id from /list\n"
    "<code>move 2 to Friday</code>\n"
    "<code>remind me about 2 a week ahead</code>\n"
    "<code>undo</code>  - roll back the last change\n"
    "You can send a voice message — I will understand it too.\n"
    "While it is <b>off</b>, only slash commands work.\n\n"
    "<b>Commands</b>\n"
    "/ai - turn the AI assistant on or off\n"
    "/add - a payment, step by step\n"
    "/task - a task, step by step\n"
    "/list - my list\n"
    "/edit 2 - change a record, including its reminders, or delete it\n"
    "/delete 2 - delete a record\n"
    "/settings - time zone, currency, reminder hour, quiet hours, sorting\n"
    "/privacy - data processing\n"
    "/cancel - cancel the current step\n\n"
    "<b>Reminders</b> arrive on their own, with buttons:\n"
    "✅ Paid / Done and ↪️ snooze.\n"
    "You choose when while adding; change it in /edit → 🔔.\n"
    "At night (23:00-08:00) I stay quiet (switchable in /settings).\n\n"
    "<b>Money:</b> <code>750</code> - your currency, <code>$10</code> - dollars.\n"
    "<b>Dates:</b> <code>25.12.26</code> · <code>tomorrow</code> · with a time: <code>25.12.26 19:00</code>\n"
    "📅 Send a calendar invite (<code>.ics</code>) and I will turn the meeting into a task.\n\n"
    f"{PRIVACY_ONBOARD_BLOCK}"
)

AI_MODE_ON_TEXT = (
    "🤖 <b>AI assistant is on</b>\n\n"
    "Write as in a normal chat — what to add, move or look up.\n"
    "You can send a voice message.\n"
    "The /add /task /list /edit /settings commands still work.\n"
    "While an /add step is open, your text goes to that step, not to the assistant.\n\n"
    "<b>Examples:</b>\n"
    "<code>netflix 999 every month</code>\n"
    "<code>task bank tomorrow</code>\n"
    "<code>what is coming up</code>\n"
    "<code>paid 2</code>\n"
    "<code>move 2 to Friday</code>\n"
    "<code>undo</code> - roll back the last change\n\n"
    "/ai again turns it off.\n\n"
    f"{PRIVACY_AI_LINE}"
)

# Shown when text or a voice note arrives while a step with buttons is open
# (the /settings menu, the reminder-plan editor). Those states match only
# callbacks, so the message would otherwise be swallowed without a word.
STEP_STILL_OPEN_TEXT = (
    "⏳ A /add, /task, /edit or /settings step is still open. "
    "Finish it or /cancel — then what you send goes to the assistant again."
)

AI_MODE_OFF_TEXT = (
    "🔇 <b>AI assistant is off</b>\n\n"
    "Commands only now: /add /task /list /edit /delete /settings /help.\n"
    "Ordinary messages and voice messages are not read.\n"
    "/ai again turns it back on."
)

# /list sort mode labels (users.list_sort: "date" | "id")
LIST_SORT_LABELS = {
    LIST_SORT_DATE: "by date (nearest first)",
    LIST_SORT_ID: "by id",
}

# /ai anti-abuse + cost control: each call spends real OpenRouter tokens/money
# on one shared API key and (before concurrent_updates + AIORateLimiter) could
# otherwise monopolize a chat's request slot. In-memory only — resets on
# restart, which is fine for basic abuse prevention (not a hard billing cap).
AI_RATE_LIMIT_PER_MINUTE = 5
AI_RATE_LIMIT_PER_DAY = 100
_ai_call_times: dict[int, deque] = {}


def _ai_rate_limit_check(user_id: int) -> str | None:
    """None if this /ai call is allowed (and records it); otherwise a reason
    string ("minute" or "day") for which quota was hit, so the user can be
    told how long to actually wait instead of a vague "try later"."""
    now = time.monotonic()
    times = _ai_call_times.setdefault(user_id, deque())
    while times and now - times[0] > 86400:
        times.popleft()
    per_minute = sum(1 for t in times if now - t <= 60)
    if per_minute >= AI_RATE_LIMIT_PER_MINUTE:
        return "minute"
    if len(times) >= AI_RATE_LIMIT_PER_DAY:
        return "day"
    times.append(now)
    return None


@asynccontextmanager
async def typing_indicator(chat):
    """Keep Telegram typing status alive during long /ai work (refreshed every 4s)."""
    stop = asyncio.Event()

    async def _keep_typing():
        while not stop.is_set():
            try:
                await chat.send_action(action=ChatAction.TYPING)
            except Exception as e:
                logger.debug("typing indicator send failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_SECONDS)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_keep_typing())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _reply_ai_result(update: Update, result: dict):
    """Always send a user-visible /ai reply; fallback if HTML parsing fails."""
    message = update.message
    if not message:
        logger.error("AI reply skipped: update has no message")
        return

    body = (result.get("message") or "").strip() or "✅ Done."
    if result.get("success"):
        dialog_count = result.get("dialog_count", 1)
        counter_line = f"{dialog_count}/6"
        if dialog_count >= 6:
            counter_line += (
                "\nDialogue limit reached. Your next message starts a fresh context."
            )
        footer = f"\n\n<i>{counter_line}</i>"
        prefix = "🤖 "
        plain_footer = f"\n\n{counter_line}"
    else:
        footer = ""
        prefix = "🤖 Error:\n\n"
        plain_footer = ""

    plain_prefix = "🤖 " if result.get("success") else "🤖 Error:\n\n"
    plain_text = f"{plain_prefix}{body}{plain_footer}"

    try:
        await message.reply_text(f"{prefix}{body}{footer}", parse_mode='HTML')
        return
    except BadRequest as e:
        logger.warning("AI reply HTML rejected, retrying plain text: %s", e)
    except Exception as e:
        logger.warning("AI reply HTML failed: %s", e)

    try:
        await message.reply_text(plain_text)
        return
    except Exception as e:
        logger.error("AI plain reply failed: %s", e)

    try:
        await update.effective_chat.send_message(plain_text[:4096])
    except Exception as e:
        logger.error("AI chat fallback reply failed: %s", e)


async def _run_ai_post_processing(application, result: dict):
    """Scheduler/cleanup after the user already received the AI reply."""
    try:
        if result.get("cleanup"):
            for uid, eid in result["cleanup"]:
                await cleanup_reminder_messages(application, uid, eid, max_logs=15)

        if result.get("expense_ids"):
            await setup_scheduler(application, run_check=False)
            for eid in set(result["expense_ids"]):
                await trigger_reminder_check(application, eid)
    except Exception as e:
        logger.error("AI post-processing failed (user already got reply): %s", e)


def _tz_for_user_sync(telegram_user_id: int) -> str:
    with get_db() as db:
        return get_user_timezone(db, telegram_user_id)


async def tz_for_user(telegram_user_id: int) -> str:
    return await asyncio.to_thread(_tz_for_user_sync, telegram_user_id)


def _is_registered_user_sync(telegram_user_id: int) -> bool:
    with get_db() as db:
        return db.query(User).filter(User.id == telegram_user_id).first() is not None


async def is_registered_user(telegram_user_id: int) -> bool:
    return await asyncio.to_thread(_is_registered_user_sync, telegram_user_id)


def _get_user_default_currency_sync(telegram_user_id: int) -> str:
    with get_db() as db:
        return get_user_default_currency(db, telegram_user_id)


def _get_user_list_sort_sync(telegram_user_id: int) -> str:
    with get_db() as db:
        return get_user_list_sort(db, telegram_user_id)


def _save_expense_with_plan_sync(user_id: int, expense_data: dict, plan_json: str | None) -> int | None:
    """Create the payment/task from a finished /add–/task dialog (period and
    reminder plan already chosen on the PLAN_EDITOR screen). Single save path
    for all four period flows. Returns expense_id, or None if the Telegram
    user has no matching row in `users` (defensive — /start registers)."""
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        is_task = expense_data.get('expense_type') == 'task'
        expense = add_expense(
            db,
            user_id=user.id,
            expense_type='task' if is_task else 'payment',
            title=expense_data['title'],
            amount=0 if is_task else expense_data.get('amount'),
            currency='RUB' if is_task else expense_data.get('currency', 'RUB'),
            next_payment_date=expense_data['next_payment_date'],
            period=expense_data.get('period', 'none'),
            period_days=expense_data.get('period_days'),
            recur_anchor=normalize_recur_anchor(expense_data.get('recur_anchor')),
            reminder_time=expense_data.get('reminder_time'),
            reminder_plan=plan_json,
        )
        expense_id = expense.id
        user_seq = expense.user_seq
        recompute_after_mutation(db, expense_id=expense_id)
        logger.info(f"Expense saved user_seq={user_seq} internal_id={expense_id}")
        return expense_id


def _get_owned_expense(db, expense_id: int, owner_id: int, active_only: bool = False):
    """The record only if it belongs to owner_id (ownership check in the filter,
    so someone else's id behaves exactly like a missing one)."""
    q = db.query(Expense).filter(Expense.id == expense_id, Expense.user_id == owner_id)
    if active_only:
        q = q.filter(Expense.is_active == True)
    return q.first()


def _load_plan_for_edit_sync(owner_id: int, expense_id: int) -> dict | None:
    """Load the current reminder plan of a record for the button editor."""
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id, active_only=True)
        if not expense:
            return None
        return {"plan": plan_from_json(expense.reminder_plan)}


def _update_reminder_plan_sync(owner_id: int, expense_id: int, plan_json: str | None,
                               recur_anchor: str | None = None) -> bool:
    """Persist an edited reminder plan (and, for recurring records, the
    recurrence anchor) on an existing record and reschedule."""
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id, active_only=True)
        if not expense:
            return False
        expense.reminder_plan = plan_json
        apply_recur_anchor(expense, recur_anchor)  # no-op unless a valid mode on a recurring row
        recompute_after_mutation(db, expense_id=expense_id)
        return True


# Tracks .ics files already auto-imported by handle_ics_document, keyed by
# Telegram's file_unique_id, so a follow-up "/ai ..." reply against the same
# document can be told the event already exists (edit it) instead of the AI
# blindly creating a second, duplicate task/expense for it. In-memory only —
# losing this on restart just means the AI won't see the dedup hint, no data
# is at risk. Bounded so it can't grow unbounded on a busy bot.
_ICS_IMPORT_CACHE_MAX = 500
_ics_import_cache: "OrderedDict[str, dict]" = OrderedDict()


def _register_ics_import(file_unique_id: str, user_id: int, user_seq: int, title: str) -> None:
    entry = _ics_import_cache.setdefault(file_unique_id, {"user_id": user_id, "items": []})
    entry["items"].append((user_seq, title))
    _ics_import_cache.move_to_end(file_unique_id)
    while len(_ics_import_cache) > _ICS_IMPORT_CACHE_MAX:
        _ics_import_cache.popitem(last=False)


# The /add, /task, /edit, /settings ConversationHandlers, populated in main()
# once they exist. Used only by _user_in_active_conversation below, so a
# document dropped mid-flow (e.g. while /add is waiting for a title) doesn't
# get hijacked by handle_ics_document — none of those flows' states match a
# document, so without this check it would silently fall through to the
# .ics auto-import instead of leaving the conversation alone as it did
# before that handler existed.
_TRACKED_CONVERSATION_HANDLERS: list = []


def _user_in_active_conversation(update: Update) -> bool:
    for conv in _TRACKED_CONVERSATION_HANDLERS:
        try:
            key = conv._get_key(update)
            if conv._conversations.get(key) is not None:
                return True
        except Exception:
            continue
    return False


def should_route_free_text_to_ai(
    *,
    ai_mode_on: bool,
    in_active_conversation: bool,
    is_command: bool = False,
) -> bool:
    """Pure routing decision (unit-testable, no I/O).

    Free-text goes to the OpenRouter path only when permanent AI mode is ON,
    the update is not a slash-command, and the user is not mid /add|/task|
    /edit|/settings (or other tracked ConversationHandler).
    """
    if is_command:
        return False
    if in_active_conversation:
        return False
    return bool(ai_mode_on)


def _get_ai_mode_sync(user_id: int) -> bool:
    with get_db() as db:
        return get_user_ai_mode(db, user_id)


def _toggle_ai_mode_sync(user_id: int) -> bool:
    with get_db() as db:
        return toggle_user_ai_mode(db, user_id)


def _add_ics_events_sync(user_id: int, events: list[dict], user_tz: str) -> list[dict]:
    """Create one task per parsed .ics event (see ics_import.py). Returns one
    result dict per event: {"success", "title", "user_seq", "error"} — never
    raises for an individual bad event (e.g. a past date), so one broken
    VEVENT in a multi-event file doesn't block the rest."""
    results = []
    today = today_in_tz(user_tz)
    with get_db() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return [
                {"success": False, "title": e["title"], "user_seq": None, "error": "run /start first"}
                for e in events
            ]

        for event in events:
            local_date, local_time = ics_import.event_local_date_and_time(event, user_tz)
            if local_date < today:
                results.append({
                    "success": False, "title": event["title"], "user_seq": None,
                    "error": "the date has already passed",
                })
                continue

            # title can be multi-line: line 1 is the short name /list shows,
            # further lines are hidden there but shown in full in the reminder
            # (see bot.py's title_first_line = exp.title.split('\n')[0] in the
            # /list handlers, and send_reminder's unsplit title_html) — puts
            # the location/link where it's actually useful (at reminder time)
            # without cluttering the compact list.
            display_title = event["title"]
            location = event.get("location")
            full_title = f"{display_title}\n{location}" if location else display_title

            expense = add_expense(
                db,
                user_id=user.id,
                expense_type='task',
                title=full_title,
                amount=0,
                currency='RUB',
                next_payment_date=local_date,
                period=event["period"],
                period_days=event["period_days"],
                reminder_time=local_time,
            )
            recompute_after_mutation(db, expense_id=expense.id)
            results.append({"success": True, "title": display_title, "user_seq": expense.user_seq, "error": None})

    return results


async def deny_access(update: Update):
    text = "🚫 <b>No access.</b>\n\nRun /start first"
    if update.message:
        await update.message.reply_text(text, parse_mode='HTML')
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        except Exception:
            pass


# Conversation states for /add
TITLE, AMOUNT, DATE, PERIOD = range(4)

# Conversation states for /task
TASK_TITLE, TASK_DATE, TASK_PERIOD = range(4, 7)

# Conversation states for /edit
EDIT_FIELD, EDIT_VALUE = range(7, 9)

# Custom period input (/add, /task)
CUSTOM_PERIOD = 9

# Conversation states for /settings
SETTINGS_MENU, SETTINGS_TZ_SEARCH = 10, 11

# Custom period input (/edit)
EDIT_CUSTOM_PERIOD = 12

# Reminder plan editor (/add, /task) — one screen after the period step;
# save happens only on ✅ Done (docs/reminder-plan.md §3)
PLAN_EDITOR = 13
PLAN_CUSTOM_OFFSET = 14


def _date_format_hint(tz: str, *, label: str = "DD.MM.YY") -> str:
    today = now_in_tz(tz)
    d = today.strftime('%d.%m.%y')
    return (
        f"Format: <b>{label}</b> · <code>today</code> · <code>tomorrow</code>\n"
        f"<i>A time works too: {d} 19:00</i>"
    )


def _date_format_error(tz: str) -> str:
    today = now_in_tz(tz)
    return (
        "❌ Could not read that date.\n\n"
        f"<code>{today.strftime('%d.%m.%y')}</code> · <code>{today.strftime('%d.%m.%y')} 19:00</code>\n"
        "<code>today</code> · <code>tomorrow</code>"
    )


# --- Reminder plan editor (/add, /task) — docs/reminder-plan.md §3 -----------
# One screen after the period step: presets recolor the slot buttons, a tap
# on a slot button cycles its state in place, ✅ Done saves. Internal
# n0..n3/every_hours never appear on buttons — only human labels.

_PLAN_D_LABELS = {"1x": "✓ Due day · 1x", "often": "✓ Due day · often", "off": "· Due day · off"}
_PLAN_F_LABELS = {"daily": "✓ Overdue · daily", "often": "✓ Overdue · often", "off": "· Overdue · off"}

# Short phrase for the anchor mode, single source for the toggle button, the
# create-success line and the /edit confirmation (recurring records only —
# one-time records have no "next time" to anchor).
_ANCHOR_SUMMARY = {
    RECUR_ANCHOR_SCHEDULED: "the schedule",
    RECUR_ANCHOR_ACTUAL: "completion",
}


def _is_recurring(expense_data: dict) -> bool:
    return expense_data.get('period') not in (None, 'none')


def _plan_anchor(expense_data: dict) -> str:
    return normalize_recur_anchor(expense_data.get('recur_anchor'))


def _anchor_label(expense_data: dict) -> str:
    """'🔁 Count from: <mode>' for the toggle button / success / edit messages."""
    return f"🔁 Count from: {_ANCHOR_SUMMARY[_plan_anchor(expense_data)]}"


def _plan_n_label(offset: int, mode: str) -> str:
    base = f"{offset} {plural_days(offset)} before"
    if mode == "off":
        return f"· {base} · off"
    return f"✓ {base} · {'1x' if mode == '1x' else '2x'}"


def _plan_editor_keyboard(ui: dict, expense_data: dict) -> InlineKeyboardMarkup:
    extra = ui.get("extra")
    # Extra wave cycled to "off" -> offer "+ Another day" again so the offset
    # can be replaced without a full preset reset (§3.5.5); the new input
    # overwrites the old wave.
    if extra and extra["mode"] != "off":
        extra_btn = InlineKeyboardButton(
            _plan_n_label(extra["offset"], extra["mode"]), callback_data="plan_cycle_extra",
        )
    else:
        extra_btn = InlineKeyboardButton("+ Another day…", callback_data="plan_add_n")
    keyboard = [
        [
            InlineKeyboardButton("⭐ Usual", callback_data="plan_preset_default"),
            InlineKeyboardButton("📅 Due day only", callback_data="plan_preset_day"),
        ],
        [InlineKeyboardButton("🔕 All off", callback_data="plan_preset_off")],
        [
            InlineKeyboardButton(_plan_n_label(3, ui["n_3"]), callback_data="plan_cycle_n_3"),
            InlineKeyboardButton(_plan_n_label(2, ui["n_2"]), callback_data="plan_cycle_n_2"),
        ],
        [
            InlineKeyboardButton(_plan_n_label(1, ui["n_1"]), callback_data="plan_cycle_n_1"),
            extra_btn,
        ],
        [
            InlineKeyboardButton(_PLAN_D_LABELS[ui["d"]], callback_data="plan_cycle_d"),
            InlineKeyboardButton(_PLAN_F_LABELS[ui["f"]], callback_data="plan_cycle_f"),
        ],
    ]
    if _is_recurring(expense_data):
        keyboard.append([
            InlineKeyboardButton(_anchor_label(expense_data), callback_data="plan_anchor_toggle"),
        ])
    keyboard.append([InlineKeyboardButton("✅ Done", callback_data="plan_done")])
    return InlineKeyboardMarkup(keyboard)


def _format_amount(amount) -> str:
    return f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"


def _plan_editor_text(expense_data: dict, ui: dict) -> str:
    is_task = expense_data.get('expense_type') == 'task'
    date_str = expense_data['next_payment_date'].strftime('%d.%m.%y')
    if expense_data.get('reminder_time'):
        date_str += f" {expense_data['reminder_time']}"
    parts = [date_str]
    if not is_task and expense_data.get('amount') is not None:
        parts.append(f"{_format_amount(expense_data['amount'])} {currency_symbol(expense_data.get('currency', 'RUB'))}")
    parts.append(_period_label(expense_data.get('period', 'none'), expense_data.get('period_days')))
    title = html.escape(expense_data['title'].split('\n')[0])
    anchor_hint = (
        "<i>🔁 Count from - what the next date is measured from:\n"
        "the planned date, or the day you actually did it.</i>\n"
        if _is_recurring(expense_data) else ""
    )
    return (
        "🔔 <b>When should I remind you?</b>\n\n"
        f"📌 <b>{title}</b>\n"
        f"{' · '.join(parts)}\n\n"
        f"Now: {ui_summary(ui)}\n\n"
        f"{anchor_hint}"
        "Tap a button to change its mode.\n"
        "<b>✅ Done</b> saves."
    )


async def _show_plan_editor(update: Update, context: CallbackContext):
    """Render the plan editor. The expense is NOT saved yet - only on Done."""
    context.user_data.setdefault('plan_ui', ui_default())
    expense_data = context.user_data['expense_data']
    # A missing recur_anchor normalizes to the default everywhere it's read
    # (_plan_anchor / save path), so no seeding is needed here.
    text = _plan_editor_text(expense_data, context.user_data['plan_ui'])
    keyboard = _plan_editor_keyboard(context.user_data['plan_ui'], expense_data)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
    return PLAN_EDITOR


def _period_label(period: str, period_days: int | None = None) -> str:
    names = {
        'month': 'every month',
        'quarter': 'every 3 months',
        'year': 'every year',
        'none': 'one-off',
    }
    if period == 'custom' and period_days:
        return f"every {period_days} days"
    return names.get(period, period)


def detect_currency(text, default='RUB'):
    """Detect USD ($) or local currency (default)."""
    text_lower = text.lower().strip()

    if '$' in text or 'usd' in text_lower or 'dollar' in text_lower:
        return 'USD'

    return default


async def cancel(update: Update, context: CallbackContext):
    """Cancel current conversation and clear state. Safe to call anytime."""
    context.user_data.clear()
    try:
        await update.message.reply_text("❌ Cancelled.", parse_mode='HTML')
    except Exception:
        pass
    return ConversationHandler.END


def _register_user_sync(user_id: int) -> None:
    with get_db() as db:
        db_user = db.query(User).filter(User.id == user_id).first()
        if not db_user:
            db_user = User(id=user_id)
            db.add(db_user)
            logger.info(f"User registered: {user_id}")


def _user_ever_used_ai_sync(user_id: int) -> bool:
    """True if this user already has an AISession row (used /ai at least once)."""
    with get_db() as db:
        return db.query(AISession).filter(AISession.user_id == user_id).first() is not None


async def start(update: Update, context: CallbackContext):
    """Register user and show welcome + privacy onboarding."""
    user = update.effective_user
    await asyncio.to_thread(_register_user_sync, user.id)

    await update.message.reply_text(
        f"👋 <b>{user.first_name}</b>! I remind you about payments and tasks.\n\n"
        "<b>/ai</b> turns on the AI assistant. After that, write as in a normal chat:\n"
        "<code>netflix 999 every month</code>\n"
        "<code>task doctor tomorrow</code>\n"
        "A voice message works too — I will understand it.\n\n"
        "Or step by step, no assistant needed: /add for a payment, /task for a task.\n"
        "List: /list · Help: /help\n\n"
        "I will send the reminders myself.\n\n"
        f"{PRIVACY_ONBOARD_BLOCK}",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: CallbackContext):
    """Show help — short and plain (see HELP_TEXT)."""
    await update.message.reply_text(HELP_TEXT, parse_mode='HTML')


async def privacy_command(update: Update, context: CallbackContext):
    """Full privacy notice (also linked from /start and /help)."""
    await update.message.reply_text(PRIVACY_FULL, parse_mode='HTML')


# /add conversation
async def add_expense_command(update: Update, context: CallbackContext):
    """Start /add conversation."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return ConversationHandler.END
    context.user_data.clear()  # e.g. stale plan_ui from an abandoned dialog
    context.user_data['expense_data'] = {}
    await update.message.reply_text(
        "📝 <b>New payment</b>\n\n"
        "Title:\n"
        "<i>For example: Netflix, rent, internet</i>\n"
        "<i>From line 2 on it is a note (link, address), shown only in the reminder</i>",
        parse_mode='HTML'
    )
    return TITLE


async def get_title(update: Update, context: CallbackContext):
    """Get title."""
    context.user_data['expense_data']['title'] = effective_conversation_text(update.message)
    default_cur = await asyncio.to_thread(_get_user_default_currency_sync, update.effective_user.id)
    default_sym = currency_symbol(default_cur)
    await update.message.reply_text(
        "💰 <b>Amount</b>\n\n"
        f"<code>750</code> → {default_sym}\n"
        "<code>$10</code> → USD",
        parse_mode='HTML'
    )
    return AMOUNT


async def get_amount(update: Update, context: CallbackContext):
    """Get amount with currency detection."""
    text = update.message.text.strip()

    default_cur = await asyncio.to_thread(_get_user_default_currency_sync, update.effective_user.id)
    currency = detect_currency(text, default=default_cur)

    clean_text = text.replace('$', '').replace('USD', '').replace('usd', '').replace('dollar', '')
    clean_text = clean_text.replace(' ', '').replace(',', '.')

    try:
        amount = float(clean_text)
        context.user_data['expense_data']['amount'] = amount
        context.user_data['expense_data']['currency'] = currency

        cur_sym = currency_symbol(currency)
        tz = await tz_for_user(update.effective_user.id)
        await update.message.reply_text(
            f"✅ {amount} {cur_sym}\n\n"
            "📅 <b>Due date</b>\n\n"
            f"{_date_format_hint(tz)}",
            parse_mode='HTML'
        )
        return DATE

    except ValueError:
        default_sym = currency_symbol(default_cur)
        await update.message.reply_text(
            "❌ Invalid amount.\n\n"
            f"<code>750</code> ({default_sym})\n"
            "<code>$10</code>",
            parse_mode='HTML'
        )
        return AMOUNT


def _period_keyboard(prefix: str = "period") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📆 Month", callback_data=f"{prefix}_month"),
            InlineKeyboardButton("📆 3 months", callback_data=f"{prefix}_quarter"),
            InlineKeyboardButton("📆 Year", callback_data=f"{prefix}_year"),
        ],
        [InlineKeyboardButton("📅 Custom days", callback_data=f"{prefix}_custom")],
        [InlineKeyboardButton("🔄 One-off", callback_data=f"{prefix}_none")],
    ])


async def _handle_date_input(update: Update, context: CallbackContext, retry_state, next_state):
    """Shared /add + /task date step: parse, reject past dates, ask period."""
    tz = await tz_for_user(update.effective_user.id)
    try:
        payment_date, reminder_time = parse_date(update.message.text, tz)
    except ValueError:
        await update.message.reply_text(_date_format_error(tz), parse_mode='HTML')
        return retry_state

    if payment_date < today_in_tz(tz):
        await update.message.reply_text(
            "❌ That date is in the past.\n\n"
            "Use today or later.",
            parse_mode='HTML'
        )
        return retry_state

    context.user_data['expense_data']['next_payment_date'] = payment_date
    if reminder_time:
        context.user_data['expense_data']['reminder_time'] = reminder_time

    await update.message.reply_text(
        "🔄 <b>Recurrence</b>",
        reply_markup=_period_keyboard(),
        parse_mode='HTML'
    )
    return next_state


async def get_date(update: Update, context: CallbackContext):
    """Get date with smart parsing."""
    return await _handle_date_input(update, context, DATE, PERIOD)


async def get_period(update: Update, context: CallbackContext):
    """Period chosen (/add and /task) → open the reminder plan editor.
    Nothing is saved yet - save happens on ✅ Done (PLAN_EDITOR)."""
    query = update.callback_query
    await query.answer()

    if query.data == 'period_custom':
        await query.edit_message_text(
            "📅 <b>Interval (days)</b>\n\n"
            "A number: <code>14</code> · <code>30</code>",
            parse_mode='HTML'
        )
        return CUSTOM_PERIOD

    period_map = {
        'period_month': 'month',
        'period_quarter': 'quarter',
        'period_year': 'year',
        'period_none': 'none',
    }
    context.user_data['expense_data']['period'] = period_map.get(query.data, 'month')
    context.user_data['expense_data']['period_days'] = None
    return await _show_plan_editor(update, context)



async def get_custom_period(update: Update, context: CallbackContext):
    """Custom period days (/add and /task) → open the reminder plan editor."""
    try:
        days = int(update.message.text.strip())
        if days < 1 or days > 3650:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ A number from 1 to 3650",
            parse_mode='HTML'
        )
        return CUSTOM_PERIOD

    context.user_data['expense_data']['period'] = 'custom'
    context.user_data['expense_data']['period_days'] = days
    return await _show_plan_editor(update, context)


async def _session_lost(context: CallbackContext, send, cmd: str = "/add or /task") -> int:
    """Conversation state lost (bot restart, cleared user_data): tell the user
    to start over, wipe leftovers, end the conversation."""
    await send(f"🔄 Session reset.\n\nStart {cmd} again.", parse_mode='HTML')
    context.user_data.clear()
    return ConversationHandler.END


# /task conversation
async def add_task(update: Update, context: CallbackContext):
    """Start /task conversation - like /add but without money."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return ConversationHandler.END
    context.user_data.clear()  # e.g. stale plan_ui from an abandoned dialog
    context.user_data['expense_data'] = {'expense_type': 'task'}
    await update.message.reply_text(
        "📝 <b>New task</b>\n\n"
        "Title:\n"
        "<i>For example: renew the subscription, bank</i>\n"
        "<i>From line 2 on it is a note (link, address), shown only in the reminder</i>",
        parse_mode='HTML'
    )
    return TASK_TITLE


async def get_task_title(update: Update, context: CallbackContext):
    """Get task title."""
    context.user_data['expense_data']['title'] = effective_conversation_text(update.message)
    tz = await tz_for_user(update.effective_user.id)
    await update.message.reply_text(
        "📅 <b>Date</b>\n\n"
        f"{_date_format_hint(tz)}",
        parse_mode='HTML'
    )
    return TASK_DATE


async def get_task_date(update: Update, context: CallbackContext):
    """Get task date with smart parsing."""
    return await _handle_date_input(update, context, TASK_DATE, TASK_PERIOD)


# /task period + custom period reuse get_period/get_custom_period — the
# flows differ only by expense_data['expense_type'], set at the entry point.


def _plan_success_text(expense_data: dict, ui: dict) -> str:
    """§3.6 save-success message: only enabled plan parts, human labels."""
    is_task = expense_data.get('expense_type') == 'task'
    title = html.escape(expense_data['title'].split('\n')[0])
    date_str = expense_data['next_payment_date'].strftime('%d.%m.%y')
    if expense_data.get('reminder_time'):
        date_str += f" {expense_data['reminder_time']}"
    lines = [
        "✅ <b>Task saved</b>" if is_task else "✅ <b>Payment saved</b>",
        "",
        f"📌 <b>{title}</b>",
    ]
    if not is_task:
        cur_sym = currency_symbol(expense_data.get('currency', 'RUB'))
        lines.append(f"💰 {_format_amount(expense_data['amount'])} {cur_sym}")
    lines.append(f"📅 {date_str}")
    lines.append(f"🔄 {_period_label(expense_data.get('period', 'none'), expense_data.get('period_days'))}")
    if _is_recurring(expense_data):
        lines.append(_anchor_label(expense_data))
    lines.append(f"🔔 {ui_summary(ui)}")
    has_reminders = ui_summary(ui) != "no reminders"
    if expense_data.get('period') == 'none' and has_reminders:
        action = "Done" if is_task else "Paid"
        lines.append("")
        lines.append(f"Then tap <b>{action}</b> in the reminder.")
    return "\n".join(lines)


async def plan_editor_callback(update: Update, context: CallbackContext):
    """All plan_* buttons on the PLAN_EDITOR screen: presets recolor the slot
    buttons, slot taps cycle their state (§3.3), Done saves."""
    query = update.callback_query
    await query.answer()
    data = query.data

    ui = context.user_data.get('plan_ui')
    expense_data = context.user_data.get('expense_data')
    if not ui or not expense_data or 'next_payment_date' not in expense_data:
        # conversation state lost (e.g. bot restarted mid-dialog)
        return await _session_lost(context, query.edit_message_text)

    if data == "plan_done":
        plan_json = plan_to_json(plan_from_ui(ui))

        # Edit mode (opened from /edit): update the existing record's plan
        # instead of creating a new expense.
        edit_id = context.user_data.get('plan_edit_expense_id')
        if edit_id is not None:
            ok = await asyncio.to_thread(
                _update_reminder_plan_sync, query.from_user.id, edit_id, plan_json,
                expense_data.get('recur_anchor'),
            )
            context.user_data.clear()
            if query.message:  # guard a queued double-tap (see create branch)
                finalized = context.chat_data.setdefault('plan_finalized_msg_ids', [])
                finalized.append(query.message.message_id)
                del finalized[:-20]
            try:
                await setup_scheduler(context.application, run_check=False)
                await trigger_reminder_check(context.application, edit_id)
            except Exception as e:
                logger.error(f"Post-plan-edit scheduling failed for expense {edit_id}: {e}")
            if ok:
                msg = f"🔔 Reminders updated: {ui_summary(ui)}"
                if _is_recurring(expense_data):
                    msg += f"\n{_anchor_label(expense_data)}"
            else:
                msg = "❌ Record not found."
            try:
                await query.edit_message_text(msg, parse_mode='HTML')
            except Exception:
                pass
            return ConversationHandler.END

        try:
            expense_id = await asyncio.to_thread(
                _save_expense_with_plan_sync, query.from_user.id, expense_data, plan_json,
            )
        except Exception as e:
            logger.error(f"Error saving expense: {e}")
            await query.edit_message_text("❌ Could not save. Try again.", parse_mode='HTML')
            context.user_data.clear()
            return ConversationHandler.END
        if expense_id is None:
            await query.edit_message_text("❌ Run /start first", parse_mode='HTML')
            context.user_data.clear()
            return ConversationHandler.END

        # The record exists from here on: end the dialog state FIRST, then do
        # the fallible post-save work — otherwise a failure below would leave
        # PLAN_EDITOR alive and a second tap on Done would save a duplicate.
        success_text = _plan_success_text(expense_data, ui)
        context.user_data.clear()
        # Remember the finalized editor message: a queued second tap on
        # Done lands in button_callback after this handler ends the
        # conversation, and query.message there is a press-time snapshot
        # (its reply_markup still shows the old keyboard) — the id is the
        # only reliable way to tell "already finalized" from "lost session".
        finalized = context.chat_data.setdefault('plan_finalized_msg_ids', [])
        if query.message:
            finalized.append(query.message.message_id)
            del finalized[:-20]
        try:
            await setup_scheduler(context.application, run_check=False)
            await trigger_reminder_check(context.application, expense_id)
        except Exception as e:
            logger.error(f"Post-save scheduling failed for expense {expense_id}: {e}")
        try:
            await query.edit_message_text(success_text, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"Success message edit failed for expense {expense_id}: {e}")
        return ConversationHandler.END

    if data == "plan_add_n":
        await query.edit_message_text(
            "📅 <b>How many days before?</b>\n\n"
            f"A number from {UI_EXTRA_OFFSET_MIN} to {UI_EXTRA_OFFSET_MAX}\n"
            "<i>(3/2/1 days before are buttons on the plan screen)</i>",
            parse_mode='HTML',
        )
        return PLAN_CUSTOM_OFFSET

    if data == "plan_preset_default":
        context.user_data['plan_ui'] = ui_default()
    elif data == "plan_preset_day":
        context.user_data['plan_ui'] = ui_day_only()
    elif data == "plan_preset_off":
        context.user_data['plan_ui'] = ui_all_off()
    elif data == "plan_anchor_toggle":
        if _is_recurring(expense_data):
            current = _plan_anchor(expense_data)
            expense_data['recur_anchor'] = (
                RECUR_ANCHOR_ACTUAL if current == RECUR_ANCHOR_SCHEDULED
                else RECUR_ANCHOR_SCHEDULED
            )
    elif data.startswith("plan_cycle_"):
        key = data[len("plan_cycle_"):]
        if key in ("n_3", "n_2", "n_1", "extra", "d", "f"):
            cycle_ui(ui, key)

    try:
        return await _show_plan_editor(update, context)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return PLAN_EDITOR  # double-tap on a preset that changed nothing
        raise


async def plan_custom_offset(update: Update, context: CallbackContext):
    """Text input for "+ Another day": days-before-due for the extra wave (§3.5)."""
    ui = context.user_data.get('plan_ui')
    expense_data = context.user_data.get('expense_data')
    if not ui or not expense_data or 'next_payment_date' not in expense_data:
        return await _session_lost(context, update.message.reply_text)

    try:
        offset = int(update.message.text.strip())
    except ValueError:
        offset = 0
    if offset in UI_FIXED_OFFSETS:
        await update.message.reply_text(
            "3/2/1 days before are buttons on the plan screen.\n"
            f"A number from {UI_EXTRA_OFFSET_MIN} to {UI_EXTRA_OFFSET_MAX}:",
            parse_mode='HTML',
        )
        return PLAN_CUSTOM_OFFSET
    if not (UI_EXTRA_OFFSET_MIN <= offset <= UI_EXTRA_OFFSET_MAX):
        await update.message.reply_text(
            f"❌ A number from {UI_EXTRA_OFFSET_MIN} to {UI_EXTRA_OFFSET_MAX}",
            parse_mode='HTML',
        )
        return PLAN_CUSTOM_OFFSET

    ui['extra'] = {"offset": offset, "mode": "1x"}
    return await _show_plan_editor(update, context)


# /settings conversation
def _settings_currency_keyboard(current_currency: str):
    rows = []
    for code in SETTINGS_CURRENCIES:
        marker = "✓ " if code == current_currency else ""
        rows.append([
            InlineKeyboardButton(
                f"{marker}{currency_label(code)}",
                callback_data=f"currsel_{code}",
            )
        ])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)


def _settings_tz_keyboard():
    rows = []
    for tz in POPULAR_TIMEZONES:
        label = tz.split("/")[-1].replace("_", " ")
        rows.append([InlineKeyboardButton(label, callback_data=f"tzsel_{tz}")])
    rows.append([InlineKeyboardButton("🔍 Search", callback_data="settings_tz_search")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)


async def settings_command(update: Update, context: CallbackContext):
    """Start /settings conversation."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return ConversationHandler.END
    return await _show_settings_menu(update, context)


def _settings_menu_keyboard(rollback_info: dict | None, list_sort: str = DEFAULT_LIST_SORT,
                            reminder_hour: int = config.DEFAULT_FIRE_HOUR, quiet: bool = True):
    sort_label = LIST_SORT_LABELS.get(list_sort, LIST_SORT_LABELS[DEFAULT_LIST_SORT])
    quiet_label = "on" if quiet else "off"
    keyboard = [
        [InlineKeyboardButton("🕐 Time zone", callback_data="settings_tz")],
        [InlineKeyboardButton("💱 Currency", callback_data="settings_currency")],
        [InlineKeyboardButton(f"🔔 Reminder hour: {reminder_hour:02d}:00", callback_data="settings_hour")],
        [InlineKeyboardButton(
            f"🌙 Quiet hours (23:00-08:00): {quiet_label}", callback_data="settings_quiet_toggle")],
        [InlineKeyboardButton(f"📋 /list sorting: {sort_label}", callback_data="settings_sort")],
    ]
    if rollback_info is not None:
        saved_tz = rollback_info.get("timezone", "?")
        keyboard.append([
            InlineKeyboardButton(
                f"↩️ Undo TZ ({saved_tz})",
                callback_data="settings_tz_rollback",
            )
        ])
    return InlineKeyboardMarkup(keyboard)


def _settings_sort_keyboard(current: str):
    rows = []
    for mode, label in (
        (LIST_SORT_DATE, "📅 By date (nearest first)"),
        (LIST_SORT_ID, "🔢 By id"),
    ):
        mark = " ✓" if current == mode else ""
        rows.append([InlineKeyboardButton(f"{label}{mark}", callback_data=f"settings_sort_{mode}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)


def _sort_expenses_for_list(items: list, mode: str) -> list:
    """Order expenses inside a payments/tasks group for /list display."""
    if mode == LIST_SORT_ID:
        return sorted(items, key=lambda e: (e.user_seq is None, e.user_seq or 0))
    # date (default): soonest due first; same day → earlier reminder_time; then id
    return sorted(
        items,
        key=lambda e: (
            e.next_payment_date,
            e.reminder_time or "",
            e.user_seq is None,
            e.user_seq or 0,
        ),
    )


def _change_currency_sync(user_id: int, new_currency: str, rates: dict) -> dict:
    """Backup DB, then convert the user's active payments to the new currency.
    May raise ValueError (invalid currency / user not found) — left to the
    caller, same as the original inline code."""
    db_backup_file = backup_database()
    with get_db() as db:
        old_currency, updated = update_user_default_currency(db, user_id, new_currency, rates)
    return {"db_backup_file": db_backup_file, "old_currency": old_currency, "updated": updated}


def _change_timezone_sync(user_id: int, new_tz: str) -> dict:
    """Backup DB, snapshot for rollback, then apply the new timezone. May raise
    ValueError (invalid timezone / user not found)."""
    db_backup_file = backup_database()
    with get_db() as db:
        save_tz_change_snapshot(db, user_id, db_backup_file)
        old_tz, updated = update_user_timezone(db, user_id, new_tz)
    return {"db_backup_file": db_backup_file, "old_tz": old_tz, "updated": updated}


def _confirm_rollback_sync(user_id: int) -> dict:
    with get_db() as db:
        return restore_tz_change(db, user_id)


def _settings_menu_data_sync(user_id: int) -> dict:
    with get_db() as db:
        tz = get_user_timezone(db, user_id)
        user_currency = get_user_default_currency(db, user_id)
        list_sort = get_user_list_sort(db, user_id)
        reminder_hour = get_user_reminder_hour(db, user_id)
        quiet = get_user_quiet_hours(db, user_id)
    # Single read of the rollback snapshot (was previously read up to 3x per
    # /settings open: once here, once in the keyboard, once for the note).
    rollback_info = get_tz_rollback_info(user_id)
    return {
        "tz": tz,
        "user_currency": user_currency,
        "list_sort": list_sort,
        "reminder_hour": reminder_hour,
        "quiet": quiet,
        "rollback_info": rollback_info,
    }


def _set_list_sort_sync(user_id: int, mode: str) -> dict:
    with get_db() as db:
        old = update_user_list_sort(db, user_id, mode)
    return {"old": old, "new": mode}


def _reminder_hour_sync(user_id: int) -> int:
    with get_db() as db:
        return get_user_reminder_hour(db, user_id)


def _toggle_quiet_hours_sync(user_id: int) -> bool:
    """Flip quiet hours, recompute the user's schedule, return the new state."""
    with get_db() as db:
        new_state = not get_user_quiet_hours(db, user_id)
        update_user_quiet_hours(db, user_id, new_state)
        return new_state


def _set_reminder_hour_sync(user_id: int, hour: int) -> dict:
    with get_db() as db:
        old, count = update_user_reminder_hour(db, user_id, hour)
    return {"old": old, "new": hour, "count": count}


def _settings_hour_keyboard(current: int):
    rows, row = [], []
    for h in config.REMINDER_HOUR_CHOICES:
        mark = " ✓" if h == current else ""
        row.append(InlineKeyboardButton(f"{h:02d}:00{mark}", callback_data=f"settings_hour_{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)


async def _show_settings_menu(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    data = await asyncio.to_thread(_settings_menu_data_sync, user_id)
    tz = data["tz"]
    user_currency = data["user_currency"]
    list_sort = data["list_sort"]
    reminder_hour = data["reminder_hour"]
    quiet = data["quiet"]
    rollback_info = data["rollback_info"]
    keyboard = _settings_menu_keyboard(rollback_info, list_sort, reminder_hour, quiet)
    rollback_note = ""
    if rollback_info is not None:
        rollback_note = "\n\n<i>A time-zone rollback is available.</i>"
    sort_label = LIST_SORT_LABELS.get(list_sort, LIST_SORT_LABELS[DEFAULT_LIST_SORT])
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🕐 <b>Time zone:</b> {tz}\n"
        f"<i>{format_tz_current(tz)}</i>\n\n"
        f"💱 <b>Currency:</b> {currency_label(user_currency)}\n\n"
        f"🔔 <b>Reminder hour:</b> {reminder_hour:02d}:00\n"
        f"🌙 <b>Quiet hours:</b> {'on (23:00-08:00)' if quiet else 'off'}\n\n"
        f"📋 <b>/list sorting:</b> {sort_label}"
        f"{rollback_note}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
    return SETTINGS_MENU


async def settings_callback(update: Update, context: CallbackContext):
    """Handle /settings inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "settings_back":
        return await _show_settings_menu(update, context)

    if data == "settings_tz":
        tz = await asyncio.to_thread(_tz_for_user_sync, user_id)
        text = (
            "🕐 <b>Time zone</b>\n\n"
            f"Current: <b>{tz}</b>\n"
            f"<i>{format_tz_current(tz)}</i>\n\n"
            "Pick from the list, or search:"
        )
        await query.edit_message_text(text, reply_markup=_settings_tz_keyboard(), parse_mode='HTML')
        return SETTINGS_MENU

    if data == "settings_currency":
        user_currency = await asyncio.to_thread(_get_user_default_currency_sync, user_id)
        text = (
            "💱 <b>Currency</b>\n\n"
            f"Current: <b>{currency_label(user_currency)}</b>\n\n"
            "Changing it recalculates active payments.\n"
            "New ones: <code>750</code> = this currency · <code>$10</code> = USD."
        )
        await query.edit_message_text(
            text,
            reply_markup=_settings_currency_keyboard(user_currency),
            parse_mode='HTML',
        )
        return SETTINGS_MENU

    if data == "settings_sort":
        list_sort = await asyncio.to_thread(_get_user_list_sort_sync, user_id)
        text = (
            "📋 <b>/list sorting</b>\n\n"
            f"Now: <b>{LIST_SORT_LABELS.get(list_sort, list_sort)}</b>\n\n"
            "Payments and tasks always stay in two blocks.\n"
            "• <b>By date</b> - nearest first (overdue on top)\n"
            "• <b>By id</b> - by the number in the list"
        )
        await query.edit_message_text(
            text,
            reply_markup=_settings_sort_keyboard(list_sort),
            parse_mode='HTML',
        )
        return SETTINGS_MENU

    if data in (f"settings_sort_{LIST_SORT_DATE}", f"settings_sort_{LIST_SORT_ID}"):
        new_mode = data[len("settings_sort_"):]
        if new_mode not in LIST_SORT_MODES:
            return await _show_settings_menu(update, context)
        try:
            change = await asyncio.to_thread(_set_list_sort_sync, user_id, new_mode)
        except ValueError as e:
            logger.error("list_sort change failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Could not change the sorting.", parse_mode='HTML')
            return ConversationHandler.END
        if change["old"] == change["new"]:
            await query.edit_message_text(
                f"ℹ️ Already selected: <b>{LIST_SORT_LABELS[new_mode]}</b>.",
                parse_mode='HTML',
            )
            return ConversationHandler.END
        await query.edit_message_text(
            "✅ <b>Sorting updated</b>\n\n"
            f"Was: {LIST_SORT_LABELS.get(change['old'], change['old'])}\n"
            f"Now: <b>{LIST_SORT_LABELS[new_mode]}</b>\n\n"
            "Takes effect on your next /list.",
            parse_mode='HTML',
        )
        return ConversationHandler.END

    if data == "settings_quiet_toggle":
        try:
            await asyncio.to_thread(_toggle_quiet_hours_sync, user_id)
        except ValueError as e:
            logger.error("quiet_hours toggle failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Could not change it.", parse_mode='HTML')
            return ConversationHandler.END
        # re-render the settings menu so the button label flips in place
        return await _show_settings_menu(update, context)

    if data == "settings_hour":
        current = await asyncio.to_thread(_reminder_hour_sync, user_id)
        text = (
            "🔔 <b>Reminder hour</b>\n\n"
            f"Now: <b>{current:02d}:00</b>\n\n"
            "When, in your time zone, to send reminders "
            "that have no explicit time of their own.\n"
            "Records with an explicit time are not affected."
        )
        await query.edit_message_text(
            text, reply_markup=_settings_hour_keyboard(current), parse_mode='HTML',
        )
        return SETTINGS_MENU

    if data.startswith("settings_hour_"):
        try:
            new_hour = int(data[len("settings_hour_"):])
        except ValueError:
            return await _show_settings_menu(update, context)
        try:
            change = await asyncio.to_thread(_set_reminder_hour_sync, user_id, new_hour)
        except ValueError as e:
            logger.error("reminder_hour change failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Could not change the hour.", parse_mode='HTML')
            return ConversationHandler.END
        if change["old"] == change["new"]:
            await query.edit_message_text(
                f"ℹ️ Already selected: <b>{new_hour:02d}:00</b>.", parse_mode='HTML',
            )
            return ConversationHandler.END
        await query.edit_message_text(
            "✅ <b>Reminder hour updated</b>\n\n"
            f"Was: {change['old']:02d}:00\n"
            f"Now: <b>{new_hour:02d}:00</b>\n"
            f"Records recalculated: <b>{change['count']}</b>",
            parse_mode='HTML',
        )
        return ConversationHandler.END

    if data.startswith("currsel_"):
        new_currency = data[len("currsel_"):]
        rates = await asyncio.to_thread(get_exchange_rates)
        if not rates:
            await query.edit_message_text(
                "❌ Exchange rate unavailable.\n\n"
                "Try again later.",
                parse_mode='HTML',
            )
            return ConversationHandler.END

        try:
            change = await asyncio.to_thread(_change_currency_sync, user_id, new_currency, rates)
        except ValueError as e:
            logger.error("Currency change failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Currency change failed. Try again.", parse_mode='HTML')
            return ConversationHandler.END
        db_backup_file = change["db_backup_file"]
        old_currency, updated = change["old_currency"], change["updated"]

        if old_currency == new_currency:
            await query.edit_message_text(
                f"ℹ️ Already selected: <b>{currency_label(new_currency)}</b>.",
                parse_mode='HTML',
            )
            return ConversationHandler.END

        backup_note = "\n💾 A backup was saved." if db_backup_file else ""
        await query.edit_message_text(
            "✅ <b>Currency updated</b>\n\n"
            f"Was: {currency_label(old_currency)}\n"
            f"Now: <b>{currency_label(new_currency)}</b>\n"
            f"Payments recalculated: <b>{updated}</b>"
            f"{backup_note}",
            parse_mode='HTML',
        )
        return ConversationHandler.END

    if data == "settings_tz_search":
        await query.edit_message_text(
            "🔍 <b>Time-zone search</b>\n\n"
            "City name in Latin letters:\n"
            "<i>moscow, berlin, new york</i>",
            parse_mode='HTML',
        )
        return SETTINGS_TZ_SEARCH

    if data == "settings_tz_rollback":
        info = await asyncio.to_thread(get_tz_rollback_info, user_id)
        if not info:
            await query.edit_message_text(
                "❌ No snapshot to roll back to.",
                parse_mode='HTML',
            )
            return await _show_settings_menu(update, context)
        exp_count = len(info.get("expenses", []))
        keyboard = [
            [
                InlineKeyboardButton("✅ Roll back", callback_data="settings_confirm_rollback"),
                InlineKeyboardButton("❌ Cancel", callback_data="settings_back"),
            ]
        ]
        await query.edit_message_text(
            "⚠️ <b>Roll back the time zone?</b>\n\n"
            f"🕐 Was: <b>{info.get('timezone')}</b>\n"
            f"📅 Records: <b>{exp_count}</b>\n\n"
            "The zone and the dates will be restored.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML',
        )
        return SETTINGS_MENU

    if data == "settings_confirm_rollback":
        try:
            result = await asyncio.to_thread(_confirm_rollback_sync, user_id)
        except ValueError as e:
            logger.error("TZ rollback failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Rollback failed. Try again.", parse_mode='HTML')
            return ConversationHandler.END
        db_note = "\n💾 A backup was saved." if result.get("db_backup_file") else ""
        await query.edit_message_text(
            "✅ <b>Rolled back</b>\n\n"
            f"🕐 Time zone: <b>{result['timezone']}</b>\n"
            f"📅 Records: <b>{result['expenses_restored']}</b>"
            f"{db_note}",
            parse_mode='HTML',
        )
        return ConversationHandler.END

    if data.startswith("tzsel_"):
        new_tz = data[len("tzsel_"):]
        try:
            change = await asyncio.to_thread(_change_timezone_sync, user_id, new_tz)
        except ValueError as e:
            logger.error("TZ change failed for user %s: %s", user_id, e)
            await query.edit_message_text("❌ Time-zone change failed. Try again.", parse_mode='HTML')
            return ConversationHandler.END
        db_backup_file = change["db_backup_file"]
        old_tz, updated = change["old_tz"], change["updated"]

        recalc_note = ""
        if updated:
            recalc_note = f"\n📅 Records recalculated: <b>{updated}</b>"
        backup_note = "\n💾 A backup was saved." if db_backup_file else ""
        await query.edit_message_text(
            "✅ <b>Time zone updated</b>\n\n"
            f"Was: {old_tz}\n"
            f"Now: <b>{new_tz}</b>\n"
            f"<i>{format_tz_current(new_tz)}</i>"
            f"{recalc_note}"
            f"{backup_note}\n\n"
            "<i>Rollback: /settings → Undo TZ</i>",
            parse_mode='HTML',
        )
        return ConversationHandler.END

    return SETTINGS_MENU


async def settings_tz_search(update: Update, context: CallbackContext):
    """Filter timezones by user query."""
    query_text = update.message.text.strip()
    results = search_timezones(query_text)
    if not results:
        await update.message.reply_text(
            "❌ Nothing found.\n\n"
            "Latin letters:\n"
            "<i>moscow, london, tokyo</i>",
            parse_mode='HTML',
        )
        return SETTINGS_TZ_SEARCH

    keyboard = [[InlineKeyboardButton(tz, callback_data=f"tzsel_{tz}")] for tz in results]
    keyboard.append([InlineKeyboardButton("↩️ Back", callback_data="settings_tz")])
    await update.message.reply_text(
        f"🔍 Found: <b>{len(results)}</b>\n\nPick one:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
    )
    return SETTINGS_MENU


# /list command
def _status_emoji(days_left: int) -> str:
    """/list urgency marker: 🔴 overdue · ⚠️ today · 🟡 in 3 days or less · 🟢 later."""
    if days_left < 0:
        return "🔴"
    if days_left == 0:
        return "⚠️"
    if days_left <= 3:
        return "🟡"
    return "🟢"


def _list_section(title: str, items, user_tz: str, with_amount: bool) -> str:
    """One compact /list section (payments or tasks); empty string if no items."""
    if not items:
        return ""
    out = f"<b>{title}</b>\n"
    for exp in items:
        days_left = (exp.next_payment_date - today_in_tz(user_tz)).days
        title_first_line = html.escape(exp.title.split('\n')[0])
        date_str = exp.next_payment_date.strftime('%d.%m.%y')
        if exp.reminder_time:
            date_str += f" {exp.reminder_time}"
        amount = ""
        if with_amount:
            amount_str = f"{exp.amount:.0f}" if exp.amount == int(exp.amount) else f"{exp.amount:.2f}"
            amount = f" | {amount_str}{currency_symbol(exp.currency)}"
        out += (
            f"{_status_emoji(days_left)} <code>{exp.user_seq:2d}</code> | <b>{title_first_line}</b>"
            f"{amount} | {date_str} | {get_period_str(exp)} | {days_left}d\n"
        )
    return out + "\n"


def _build_list_message_sync(telegram_user_id: int) -> tuple[str | None, int | None]:
    """Fetch + format the /list message. Returns (None, user_id) when the user
    has no active records (caller sends the empty-state message instead)."""
    with get_db() as db:
        user = db.query(User).filter(User.id == telegram_user_id).first()
        current_user_id = user.id
        user_tz = get_user_timezone(db, user.id)
        user_currency = get_user_default_currency(db, user.id)
        list_sort = get_user_list_sort(db, user.id)
        expenses = db.query(Expense).filter(
            Expense.user_id == user.id,
            Expense.is_active == True
        ).all()

        if not expenses:
            return None, current_user_id

        # Build the full message while objects are still attached to the session
        payments = _sort_expenses_for_list(
            [e for e in expenses if e.expense_type != 'task'], list_sort,
        )
        tasks = _sort_expenses_for_list(
            [e for e in expenses if e.expense_type == 'task'], list_sort,
        )

        message = "<b>📋 List</b>\n\n"
        message += _list_section("💰 Payments", payments, user_tz, with_amount=True)
        message += _list_section("📋 Tasks", tasks, user_tz, with_amount=False)

        rates = get_exchange_rates()
        local_sym = currency_symbol(user_currency)

        rate_date_str = ""
        try:
            cache_path = config.CACHE_FILE
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                    if "timestamp" in cache:
                        ts = datetime.fromisoformat(cache["timestamp"])
                        ts_local = to_user_tz(ts, user_tz)
                        rate_date_str = ts_local.strftime("%d.%m.%y %H:%M")
        except Exception:
            pass

        if payments and rates:
            try:
                total_usd = sum(
                    convert_amount(exp.amount, exp.currency, "USD", rates) for exp in payments
                )
                total_local = sum(
                    convert_amount(exp.amount, exp.currency, user_currency, rates) for exp in payments
                )
                usd_to_local = convert_amount(1, "USD", user_currency, rates)
                rate_str = f" (1$ = {usd_to_local:.2f}"
                if rate_date_str:
                    rate_str += f", as of {rate_date_str})"
                else:
                    rate_str += ")"
                message += (
                    f"<b>💰 Total: ${total_usd:.2f} / {total_local:.2f} {local_sym}</b>"
                    f"{rate_str}\n\n"
                )
            except ValueError:
                message += "<b>💰 Total:</b> <i>rate unavailable</i>\n\n"
        elif payments:
            total_usd = sum(
                exp.amount for exp in payments if (exp.currency or "").upper() == "USD"
            )
            total_local = sum(
                exp.amount for exp in payments if (exp.currency or user_currency).upper() != "USD"
            )
            message += "<b>💰 Total:</b>\n"
            message += f"   ${total_usd:.2f} / {total_local:.2f} {local_sym}\n"
            message += "   <i>Rate unavailable - amounts shown as stored</i>\n\n"

        sort_hint = LIST_SORT_LABELS.get(list_sort, LIST_SORT_LABELS[DEFAULT_LIST_SORT])
        message += (
            "<i>💡 The id on the left is what /edit and /delete take</i>\n"
            "<i>💡 The total covers payments only</i>\n"
            f"<i>💡 Sorting: {sort_hint} · /settings</i>"
        )

        return message, current_user_id


async def list_expenses(update: Update, context: CallbackContext):
    """Show all expenses."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return

    built_message, current_user_id = await asyncio.to_thread(
        _build_list_message_sync, update.effective_user.id,
    )

    if built_message is None:
        await update.message.reply_text(
            "📭 <b>The list is empty</b>\n\n"
            "Add one step by step:\n"
            "/add - a payment\n"
            "/task - a task\n"
            "Or /ai to turn on the assistant, then write as in a normal chat.",
            parse_mode='HTML'
        )
        return

    # Telegram caps a message at 4096 chars. A long list (many records, some
    # with a 🔔-plan sub-line) could exceed it → reply_text raises and the
    # immediate-reminder pass below would be skipped. Truncate defensively so
    # the reply always goes out; also fall back to plain text on any HTML
    # rejection — either way the reminders still fire.
    if len(built_message) > 4096:
        cut = built_message.rfind("\n", 0, 3900)
        built_message = built_message[:cut if cut > 0 else 3900] + "\n\n<i>… list truncated. /settings for sorting</i>"
    try:
        await update.message.reply_text(built_message, parse_mode='HTML')
    except BadRequest as e:
        logger.warning("/list HTML reply rejected, retrying plain: %s", e)
        try:
            await update.message.reply_text(built_message[:4096])
        except Exception as e2:
            logger.error("/list plain reply failed: %s", e2)

    if current_user_id is not None:
        await send_list_immediate_reminders(context.application, current_user_id)


# /test_button command (dev-only diagnostic, not documented to users)
async def test_button(update: Update, context: CallbackContext):
    """Send test message with payment button."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return
    keyboard = [[InlineKeyboardButton("✅ Paid", callback_data="test_pay")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    tz = await tz_for_user(update.effective_user.id)
    today = now_in_tz(tz).strftime('%d.%m.%y')

    await update.message.reply_text(
        "🧪 <b>Test payment reminder</b>\n\n"
        "📌 <b>Internet</b>\n"
        "💰 Amount: <b>750 ₽</b>\n"
        f"📅 Due date: <b>{today}</b>\n\n"
        "This is a test message with a payment button.\n"
        "Tap the button if it is paid.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def _extract_ics_ai_context(context: CallbackContext, message, user_tz: str, user_id: int | None = None) -> str | None:
    """If `message` (an /ai command message) or its reply target carries an
    .ics document, download and parse it and return a text block describing
    the event(s) for the AI prompt — lets /ai reason about a forwarded
    meeting invite the same way it does for a forwarded receipt text.
    Checks the message itself first (doc sent with caption "/ai ..."), then
    a replied-to message (doc sent separately, then "/ai ..." as a reply).
    If this same file was already auto-imported by handle_ics_document, adds
    a note pointing the AI at the existing task instead of letting it create
    a duplicate."""
    for candidate in (message, getattr(message, "reply_to_message", None)):
        document = getattr(candidate, "document", None) if candidate else None
        if not document or not ics_import.is_ics_filename(document.file_name, document.mime_type):
            continue
        if document.file_size and document.file_size > ics_import.MAX_ICS_FILE_BYTES:
            logger.warning(f"Ignoring oversized .ics for AI context: {document.file_size} bytes")
            return None
        try:
            tg_file = await context.bot.get_file(document.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            events = await asyncio.to_thread(ics_import.parse_ics_events, data)
        except Exception as e:
            logger.warning(f"Failed to parse .ics for AI context: {e}")
            return None
        if not events:
            return None
        blocks = [ics_import.format_event_for_ai(e, user_tz) for e in events]
        text = f"[Calendar invite {document.file_name or 'invite.ics'}]\n" + "\n\n".join(blocks)
        cached = _ics_import_cache.get(document.file_unique_id)
        if cached and cached.get("user_id") == user_id and cached.get("items"):
            existing = ", ".join(f"ID {seq} «{title}»" for seq, title in cached["items"])
            text += (
                f"\n\n(Note: this file was already imported as task {existing}. "
                "If the user asks to change this meeting, use edit_expense or "
                "reschedule_expense on it instead of creating a new task.)"
            )
        return text
    return None


# /ai command — permanent free-text AI assistant ON/OFF toggle (not a one-shot prompt).
async def ai_command(update: Update, context: CallbackContext):
    """Toggle permanent AI mode for this user. Does not treat args as a prompt;
    when mode is ON, free-text messages use the OpenRouter path instead."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return

    user = update.effective_user
    try:
        new_state = await asyncio.to_thread(_toggle_ai_mode_sync, user.id)
    except ValueError:
        await deny_access(update)
        return

    if new_state:
        await update.message.reply_text(AI_MODE_ON_TEXT, parse_mode='HTML')
    else:
        await update.message.reply_text(AI_MODE_OFF_TEXT, parse_mode='HTML')


async def run_ai_from_message(
    update: Update,
    context: CallbackContext,
    user_text: str,
    already_rate_limited: bool = False,
) -> None:
    """Shared OpenRouter entry used by free-text AI when permanent mode is ON.

    Same rate-limit, privacy first-use, reply/forward/ics context, and
    post-processing as the former one-shot /ai <text> path.

    already_rate_limited: the voice path spends the 5/min 100/day slot *before*
    transcription, so speech-to-text cannot be used to bypass the cap; skip a
    second charge here.
    """
    user = update.effective_user

    if not already_rate_limited:
        rate_limit_hit = _ai_rate_limit_check(user.id)
        if rate_limit_hit == "minute":
            await update.message.reply_text(
                "⏳ Rate limit: wait a minute.",
                parse_mode='HTML',
            )
            return
        if rate_limit_hit == "day":
            await update.message.reply_text(
                "⏳ Daily AI limit reached. Try again tomorrow.",
                parse_mode='HTML',
            )
            return

    user_text = (user_text or "").strip()
    ics_context = await _extract_ics_ai_context(
        context, update.message, await tz_for_user(user.id), user_id=user.id,
    )
    user_message = build_ai_user_message(update.message, user_text)
    if ics_context:
        user_message = (
            f"{ics_context}\n\n{user_message}".strip() if user_message.strip()
            else f"{ics_context}\n\n[User request]\n(no text: add this meeting as a task)"
        )

    if not user_message.strip():
        # No typed text and no reply/forward/ics context — nothing to send.
        return

    if has_telegram_message_context(update.message):
        logger.info(
            "AI message with Telegram context for user %s (reply/forward/quote)",
            user.id,
        )

    # First-ever AI call: surface privacy before any data leaves to OpenRouter.
    ever_used_ai = await asyncio.to_thread(_user_ever_used_ai_sync, user.id)
    if not ever_used_ai:
        await update.message.reply_text(
            f"{PRIVACY_AI_LINE}\n"
            "Shown once, before your first AI request. /privacy",
            parse_mode='HTML',
        )

    try:
        async with typing_indicator(update.effective_chat):
            result = await call_openrouter(user_message, user.id, intent_text=user_text)
    except Exception as e:
        logger.error("Unhandled AI error for user %s: %s", user.id, e)
        result = {
            "success": False,
            "message": "❌ Internal error. Try again.",
            "dialog_count": 0,
            "expense_ids": [],
            "cleanup": [],
        }

    await _reply_ai_result(update, result)
    asyncio.create_task(_run_ai_post_processing(context.application, result))


async def free_text_ai_handler(update: Update, context: CallbackContext):
    """Route non-command free text to AI only when permanent mode is ON.

    Registered after ConversationHandlers so /add|/task|/edit|/settings steps
    keep priority when those handlers claim the update. Extra guard via
    _user_in_active_conversation covers callback-only states (PLAN_EDITOR etc.)
    where free text would otherwise fall through.
    """
    if not update.message or not update.effective_user:
        return
    if not await is_registered_user(update.effective_user.id):
        return

    in_conv = _user_in_active_conversation(update)
    ai_on = await asyncio.to_thread(_get_ai_mode_sync, update.effective_user.id)
    if not should_route_free_text_to_ai(
        ai_mode_on=ai_on,
        in_active_conversation=in_conv,
        is_command=False,
    ):
        # Callback-only states (the /settings menu, PLAN_EDITOR) never consume
        # text, so this handler still runs for them. Returning in silence looks
        # exactly like a dead assistant; say the step is still open instead.
        if ai_on and in_conv:
            logger.info(
                "AI free-text dropped: user %s in active conversation",
                update.effective_user.id,
            )
            try:
                await update.message.reply_text(
                    STEP_STILL_OPEN_TEXT,
                    parse_mode='HTML',
                )
            except Exception:
                pass
        return

    user_text = (update.message.text or "").strip()
    await run_ai_from_message(update, context, user_text=user_text)


async def voice_ai_handler(update: Update, context: CallbackContext):
    """Telegram voice note (message.voice) → speech-to-text → the same AI path
    as typed text.

    The gates match free_text_ai_handler exactly: with the assistant off the
    note is ignored, and mid /add|/task|/edit|/settings it gets the same
    "step still open" notice. The rate limit is charged before anything is
    downloaded or transcribed, so a voice note cannot buy a free AI call. A
    successful transcript goes through run_ai_from_message and therefore spends
    one of the dialog's turns, like a typed message.
    """
    if not update.message or not update.message.voice or not update.effective_user:
        return
    if not await is_registered_user(update.effective_user.id):
        return

    in_conv = _user_in_active_conversation(update)
    ai_on = await asyncio.to_thread(_get_ai_mode_sync, update.effective_user.id)
    if not should_route_free_text_to_ai(
        ai_mode_on=ai_on,
        in_active_conversation=in_conv,
        is_command=False,
    ):
        if ai_on and in_conv:
            logger.info(
                "AI voice dropped: user %s in active conversation",
                update.effective_user.id,
            )
            try:
                await update.message.reply_text(
                    STEP_STILL_OPEN_TEXT,
                    parse_mode='HTML',
                )
            except Exception:
                pass
        return

    rate_limit_hit = _ai_rate_limit_check(update.effective_user.id)
    if rate_limit_hit == "minute":
        await update.message.reply_text(
            "⏳ Rate limit: wait a minute.",
            parse_mode='HTML',
        )
        return
    if rate_limit_hit == "day":
        await update.message.reply_text(
            "⏳ Daily AI limit reached. Try again tomorrow.",
            parse_mode='HTML',
        )
        return

    voice = update.message.voice
    # Telegram reports the size up front, so an oversized note is refused
    # without downloading it.
    if voice.file_size and voice.file_size > MAX_VOICE_FILE_BYTES:
        await update.message.reply_text(
            "❌ That file is too large.",
            parse_mode='HTML',
        )
        return

    audio_format = voice_audio_format(getattr(voice, "mime_type", None))

    try:
        async with typing_indicator(update.effective_chat):
            tg_file = await context.bot.get_file(voice.file_id)
            data = bytes(await tg_file.download_as_bytearray())
            # file_size is advisory; check the bytes actually received too.
            if len(data) > MAX_VOICE_FILE_BYTES:
                await update.message.reply_text(
                    "❌ That file is too large.",
                    parse_mode='HTML',
                )
                return
            stt = await transcribe_audio(data, audio_format)
    except Exception as e:
        logger.warning(
            "Failed to download/transcribe voice from user %s: %s",
            update.effective_user.id, e,
        )
        await update.message.reply_text(
            "❌ Could not download the voice note.",
            parse_mode='HTML',
        )
        return

    if stt.error or not (stt.text or "").strip():
        if stt.error == "empty":
            await update.message.reply_text(
                "❌ No speech in that voice note — type it or record again.",
                parse_mode='HTML',
            )
        else:
            await update.message.reply_text(
                "❌ Could not transcribe the voice note. Try again.",
                parse_mode='HTML',
            )
        return

    await run_ai_from_message(
        update, context, user_text=stt.text, already_rate_limited=True,
    )


async def handle_ics_document(update: Update, context: CallbackContext):
    """Any .ics calendar invite dropped on the bot directly becomes a task
    immediately when AI mode is OFF (or caption is empty) — deterministic,
    no OpenRouter. See ics_import.py. Registered on filters.Document.ALL.

    PTB's CommandHandler only matches on message.text/entities, never on a
    document's caption. Caption `/ai` toggles permanent AI mode (same as the
    command). Free-text caption while AI mode is ON goes to the AI path with
    the calendar file as context instead of blind auto-import."""
    document = update.message.document
    if not document or not ics_import.is_ics_filename(document.file_name, document.mime_type):
        return  # not an .ics — ignore silently regardless of registration status

    caption = (update.message.caption or "").strip()
    if caption.startswith("/"):
        cmd_word, _, rest = caption[1:].partition(" ")
        if cmd_word.split("@", 1)[0].lower() == "ai":
            # /ai as caption = mode toggle only (args ignored; not a one-shot prompt)
            await ai_command(update, context)
            return

    if _user_in_active_conversation(update):
        # e.g. mid-/add waiting for a title — leave the conversation alone,
        # matching the pre-existing behavior of silently ignoring a document
        # that doesn't match any state handler.
        return

    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return

    # AI mode ON + free-text caption (or bare file with mode ON and user
    # intent implied): prefer AI path with ics context over blind import when
    # there is a non-command caption. Bare file without caption still
    # auto-imports even if AI is ON — deterministic calendar drop.
    if caption and not caption.startswith("/"):
        ai_on = await asyncio.to_thread(_get_ai_mode_sync, update.effective_user.id)
        if should_route_free_text_to_ai(
            ai_mode_on=ai_on,
            in_active_conversation=False,
            is_command=False,
        ):
            await run_ai_from_message(update, context, user_text=caption)
            return

    if document.file_size and document.file_size > ics_import.MAX_ICS_FILE_BYTES:
        await update.message.reply_text(
            "❌ That file is too large.",
            parse_mode='HTML',
        )
        return

    try:
        tg_file = await context.bot.get_file(document.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        events = await asyncio.to_thread(ics_import.parse_ics_events, data)
    except Exception as e:
        logger.warning(f"Failed to parse .ics document from user {update.effective_user.id}: {e}")
        await update.message.reply_text(
            "❌ Could not read the .ics file.",
            parse_mode='HTML',
        )
        return

    if not events:
        await update.message.reply_text(
            "❌ No dated events in that .ics file.",
            parse_mode='HTML',
        )
        return

    user_tz = await tz_for_user(update.effective_user.id)
    results = await asyncio.to_thread(_add_ics_events_sync, update.effective_user.id, events, user_tz)

    await setup_scheduler(context.application, run_check=False)

    lines = []
    plain_lines = []
    for r in results:
        if r["success"]:
            lines.append(f"✅ <b>{html.escape(r['title'])}</b> - task ID {r['user_seq']}")
            plain_lines.append(f"✅ {r['title']} - task ID {r['user_seq']}")
            _register_ics_import(document.file_unique_id, update.effective_user.id, r['user_seq'], r['title'])
        else:
            lines.append(f"❌ <b>{html.escape(r['title'])}</b> — {r['error']}")
            plain_lines.append(f"❌ {r['title']} — {r['error']}")
    try:
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    except BadRequest as e:
        logger.warning("ICS import reply HTML rejected, retrying plain text: %s", e)
        await update.message.reply_text("\n".join(plain_lines))


# /edit command
def _find_expense_for_edit_sync(user_id: int, user_seq: int) -> dict | None:
    with get_db() as db:
        expense = get_expense_by_user_seq(db, user_id, user_seq)
        if not expense:
            return None
        plan = plan_from_json(expense.reminder_plan)
        return {
            "id": expense.id,
            "expense_type": expense.expense_type,
            "title": expense.title,
            "plan_summary": plan_summary(plan),
            # display context so the plan editor can render without re-querying
            "expense_data": {
                "expense_type": expense.expense_type,
                "title": expense.title,
                "amount": expense.amount,
                "currency": expense.currency,
                "next_payment_date": expense.next_payment_date,
                "reminder_time": expense.reminder_time,
                "period": expense.period,
                "period_days": expense.period_days,
                "recur_anchor": expense.recur_anchor,
            },
        }


async def edit_expense(update: Update, context: CallbackContext):
    """Start edit conversation."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return ConversationHandler.END

    # A bad id returns None, not END: /edit is re-entrant, so this call may have
    # arrived while a working menu for another id is still on screen. END would
    # close that conversation and leave its buttons dead, while None leaves the
    # state exactly as it was (and no state at all when there was none).
    try:
        user_seq = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Format: <code>/edit 3</code>\n"
            "The id is in /list",
            parse_mode='HTML'
        )
        return None

    found = await asyncio.to_thread(_find_expense_for_edit_sync, update.effective_user.id, user_seq)
    if found is None:
        await update.message.reply_text(
            f"❌ No record with ID {user_seq}. /list",
            parse_mode='HTML',
        )
        return None
    context.user_data['edit_expense_id'] = found["id"]
    context.user_data['edit_expense_type'] = found["expense_type"]
    context.user_data['edit_plan_ctx'] = found["expense_data"]  # for the plan editor
    context.user_data['edit_user_seq'] = user_seq  # for the 🗑 Delete option

    # Show edit options
    keyboard = [
        [InlineKeyboardButton("📌 Title", callback_data="edit_title")],
        [InlineKeyboardButton("📅 Date", callback_data="edit_date")],
        [InlineKeyboardButton("🔄 Recurrence", callback_data="edit_period")],
        [InlineKeyboardButton("🔔 Reminders", callback_data="edit_plan")],
        [InlineKeyboardButton("🗑 Delete", callback_data="edit_delete")],
    ]
    if context.user_data.get('edit_expense_type') != 'task':
        keyboard.insert(1, [InlineKeyboardButton("💰 Amount", callback_data="edit_amount")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    menu = await update.message.reply_text(
        f"✏️ <b>Editing ID {user_seq}</b>\n\n"
        # The whole title, notes included: a title edit replaces all of it, and
        # /edit is the one screen where the id alone says nothing.
        f"📌 {html.escape(found['title'])}\n"
        f"🔔 Reminders: {found['plan_summary']}\n\n"
        "Field:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    # /edit is re-entrant, so a second /edit leaves the previous menu on screen
    # with buttons that still look live. Remember which message is the current
    # one; edit_field_selected retires any older one instead of applying its
    # taps to the newest ID.
    context.user_data['edit_menu_msg_id'] = menu.message_id
    return EDIT_FIELD


async def edit_field_selected(update: Update, context: CallbackContext):
    """Handle field selection."""
    query = update.callback_query
    await query.answer()

    menu_msg_id = context.user_data.get('edit_menu_msg_id')
    if menu_msg_id is not None and query.message and query.message.message_id != menu_msg_id:
        # Tap on the menu of an earlier /edit: its header names another ID, so
        # acting on it would change a record the user is not looking at.
        await query.edit_message_text(
            "⌛ <b>This menu is out of date</b>\n\n"
            "/edit was opened again. Use the newest menu, or run /edit ID.",
            parse_mode='HTML',
        )
        return EDIT_FIELD

    field = query.data  # edit_title, edit_amount, etc.
    context.user_data['edit_field'] = field.replace('edit_', '')

    if field == 'edit_delete':
        # Same mechanics as /delete: one confirmation screen, then the
        # confirm_delete / cancel_delete callbacks in button_callback do the
        # soft delete. The conversation still MUST end here: EDIT_FIELD is
        # pattern-scoped to edit_ callbacks, but leaving the state alive would
        # keep answering later stray taps with a field prompt.
        user_seq = context.user_data.get('edit_user_seq')
        if user_seq is None:
            return await _session_lost(context, query.edit_message_text, "/edit ID")
        info = await asyncio.to_thread(
            _prepare_delete_confirmation_sync, query.from_user.id, user_seq,
        )
        if info is None:
            await query.edit_message_text("❌ Record not found.", parse_mode='HTML')
            context.user_data.clear()
            return ConversationHandler.END
        context.user_data.clear()
        context.user_data['delete_expense_id'] = info['expense_id']
        await _ask_delete_confirmation(query.edit_message_text, info)
        return ConversationHandler.END

    if field == 'edit_plan':
        expense_id = context.user_data.get('edit_expense_id')
        ctx = context.user_data.get('edit_plan_ctx')
        if not expense_id or not ctx:
            return await _session_lost(context, query.edit_message_text, "/edit ID")
        found = await asyncio.to_thread(_load_plan_for_edit_sync, query.from_user.id, expense_id)
        if found is None:
            await query.edit_message_text("❌ Record not found.", parse_mode='HTML')
            context.user_data.clear()
            return ConversationHandler.END
        ui = ui_from_plan(found["plan"])
        if ui is None:
            # plan too complex for the button editor — hand off to free-text AI
            await query.edit_message_text(
                f"🔔 Now: {plan_summary(found['plan'])}\n\n"
                "This plan is too complex for the buttons. Turn on the AI assistant (/ai) "
                "and describe it instead, for example:\n"
                "<code>remind me about ID … a week before and on the day</code>",
                parse_mode='HTML',
            )
            context.user_data.clear()
            return ConversationHandler.END
        context.user_data['plan_ui'] = ui
        context.user_data['plan_edit_expense_id'] = expense_id
        context.user_data['expense_data'] = ctx  # display context for the editor
        return await _show_plan_editor(update, context)

    field_prompts = {
        'title': "✏️ Title:",
        'amount': "✏️ Amount:\n<code>750</code> · <code>$10</code>",
        'date': None,  # special
        'period': None,
    }

    if field == 'edit_period':
        await query.edit_message_text(
            "🔄 <b>Recurrence</b>",
            reply_markup=_period_keyboard("new_period"),
            parse_mode='HTML'
        )
        return EDIT_VALUE
    if field == 'edit_date':
        user_tz = await tz_for_user(query.from_user.id)
        await query.edit_message_text(
            f"✏️ <b>Date</b>\n\n{_date_format_hint(user_tz)}",
            parse_mode='HTML'
        )
        return EDIT_VALUE

    prompt = field_prompts.get(context.user_data['edit_field'], "✏️ Value:")
    await query.edit_message_text(prompt, parse_mode='HTML')
    return EDIT_VALUE


def _apply_edit_value_sync(
    expense_id: int, owner_id: int, field: str, raw_text: str, title_text: str | None,
) -> dict:
    """Validate + persist one /edit field change. Returns a status dict that the
    async handler turns into Telegram replies (kept out of the worker thread)."""
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id)

        if not expense:
            return {"status": "not_found"}

        if field == 'amount' and expense.expense_type == 'task':
            return {"status": "task_no_amount"}

        try:
            if field == 'title':
                expense.title = title_text
                recompute_after_mutation(db, expense_id=expense_id)
                return {
                    "status": "ok",
                    "reply_html": f"✅ Title: 📌 {html.escape(title_text)}",
                    "reschedule": False,
                }
            elif field == 'amount':
                default_cur = get_user_default_currency(db, owner_id)
                currency = detect_currency(raw_text, default=default_cur)
                clean_text = raw_text.replace('$', '').replace('USD', '').replace('usd', '')
                clean_text = clean_text.replace(' ', '').replace(',', '.')
                amount = float(clean_text)
                expense.amount = amount
                expense.currency = currency
                cur_sym = currency_symbol(currency)
                return {
                    "status": "ok_end",
                    "reply_html": f"✅ Amount: 💰 {amount} {cur_sym}",
                }
            elif field == 'date':
                user_tz = get_user_timezone(db, owner_id)
                new_date, new_time = parse_date(raw_text, user_tz)
                if new_date < today_in_tz(user_tz):
                    return {"status": "past_date"}
                expense.next_payment_date = new_date
                if new_time:
                    expense.reminder_time = new_time
                recompute_after_mutation(db, expense_id=expense_id)
                date_display = new_date.strftime('%d.%m.%y')
                if new_time:
                    date_display += f" {new_time}"
                return {
                    "status": "ok",
                    "reply_html": f"✅ Date: 📅 {date_display}",
                    "reschedule": True,
                }
            elif field == 'period':
                # This shouldn't happen here, period is handled via callback
                return {"status": "noop"}
        except ValueError:
            if field == 'date':
                return {"status": "date_format_error", "tz": get_user_timezone(db, owner_id)}
            return {"status": "format_error"}
    return {"status": "noop"}


async def edit_value(update: Update, context: CallbackContext):
    """Save edited value."""
    expense_id = context.user_data.get('edit_expense_id')
    field = context.user_data.get('edit_field')
    if not expense_id or not field:
        # state lost (e.g. another conversation's entry point cleared
        # user_data while this conversation was still parked at EDIT_VALUE)
        return await _session_lost(context, update.message.reply_text, "/edit ID")
    owner_id = update.effective_user.id
    raw_text = update.message.text
    title_text = effective_conversation_text(update.message) if field == 'title' else None

    result = await asyncio.to_thread(
        _apply_edit_value_sync, expense_id, owner_id, field, raw_text, title_text,
    )
    status = result["status"]

    if status == "not_found":
        await update.message.reply_text("❌ Record not found.", parse_mode='HTML')
        return ConversationHandler.END
    if status == "task_no_amount":
        await update.message.reply_text(
            "❌ Tasks have no amount. Pick title, date or recurrence.",
            parse_mode='HTML',
        )
        context.user_data.clear()
        return ConversationHandler.END
    if status == "past_date":
        await update.message.reply_text("❌ That date is in the past.", parse_mode='HTML')
        return ConversationHandler.END
    if status == "date_format_error":
        await update.message.reply_text(_date_format_error(result["tz"]), parse_mode='HTML')
        return ConversationHandler.END
    if status == "format_error":
        await update.message.reply_text("❌ Invalid format.", parse_mode='HTML')
        return ConversationHandler.END
    if status == "ok_end":
        await update.message.reply_text(result["reply_html"], parse_mode='HTML')
        context.user_data.clear()
        return ConversationHandler.END
    if status == "ok":
        await update.message.reply_text(result["reply_html"], parse_mode='HTML')
        if result.get("reschedule"):
            await setup_scheduler(context.application, run_check=False)
            await trigger_reminder_check(context.application, expense_id)
        context.user_data.clear()
        return ConversationHandler.END

    # status == "noop" (field == 'period' text input — shouldn't normally happen)
    context.user_data.clear()
    return ConversationHandler.END


def _apply_edit_period_sync(expense_id: int, owner_id: int, new_period: str) -> dict:
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id)
        if not expense:
            return {"status": "not_found"}
        expense.period = new_period
        if new_period != 'custom':
            expense.period_days = None
        recompute_after_mutation(db, expense_id=expense_id)
        return {"status": "ok", "period_days": expense.period_days}


def _apply_edit_custom_period_sync(expense_id: int, owner_id: int, days: int) -> bool:
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id)
        if not expense:
            return False
        expense.period = 'custom'
        expense.period_days = days
        recompute_after_mutation(db, expense_id=expense_id)
        return True


async def edit_period_selected(update: Update, context: CallbackContext):
    """Handle period selection for edit."""
    query = update.callback_query
    await query.answer()

    if query.data == 'new_period_custom':
        await query.edit_message_text(
            "📅 <b>Interval (days)</b>\n\n"
            "A number: <code>14</code> · <code>30</code>",
            parse_mode='HTML'
        )
        return EDIT_CUSTOM_PERIOD

    expense_id = context.user_data.get('edit_expense_id')
    if not expense_id:
        return await _session_lost(context, query.edit_message_text, "/edit ID")
    owner_id = query.from_user.id

    period_map = {
        'new_period_month': 'month',
        'new_period_quarter': 'quarter',
        'new_period_year': 'year',
        'new_period_none': 'none',
    }
    new_period = period_map.get(query.data)
    if not new_period:
        return ConversationHandler.END

    result = await asyncio.to_thread(_apply_edit_period_sync, expense_id, owner_id, new_period)
    if result["status"] == "not_found":
        await query.edit_message_text("❌ Record not found.", parse_mode='HTML')
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text(
        f"✅ Recurrence: 🔄 {_period_label(new_period, result['period_days'])}",
        parse_mode='HTML'
    )

    await setup_scheduler(context.application, run_check=False)
    await trigger_reminder_check(context.application, expense_id)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_custom_period(update: Update, context: CallbackContext):
    """Save custom period days for /edit."""
    try:
        days = int(update.message.text.strip())
        if days < 1 or days > 3650:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ A number from 1 to 3650",
            parse_mode='HTML'
        )
        return EDIT_CUSTOM_PERIOD

    expense_id = context.user_data.get('edit_expense_id')
    if not expense_id:
        return await _session_lost(context, update.message.reply_text, "/edit ID")
    owner_id = update.effective_user.id

    found = await asyncio.to_thread(_apply_edit_custom_period_sync, expense_id, owner_id, days)
    if not found:
        await update.message.reply_text("❌ Record not found.", parse_mode='HTML')
        context.user_data.clear()
        return ConversationHandler.END

    await setup_scheduler(context.application, run_check=False)
    await trigger_reminder_check(context.application, expense_id)
    await update.message.reply_text(
        f"✅ Recurrence: 🔄 {_period_label('custom', days)}",
        parse_mode='HTML'
    )
    context.user_data.clear()
    return ConversationHandler.END


# /delete command
def _prepare_delete_confirmation_sync(owner_id: int, user_seq: int) -> dict | None:
    with get_db() as db:
        expense = get_expense_by_user_seq(db, owner_id, user_seq)
        if not expense:
            return None

        cur_sym = currency_symbol(expense.currency)
        record_type = "task" if expense.expense_type == 'task' else "payment"

        title_html = html.escape(expense.title)
        if expense.expense_type == 'task':
            detail = f"📌 <b>{title_html}</b>\n📅 {expense.next_payment_date.strftime('%d.%m.%y')}"
        else:
            detail = (
                f"📌 <b>{title_html}</b>\n"
                f"💰 {expense.amount} {cur_sym}\n"
                f"📅 {expense.next_payment_date.strftime('%d.%m.%y')}"
            )

        return {"expense_id": expense.id, "record_type": record_type, "detail": detail}


async def _ask_delete_confirmation(send, info: dict) -> None:
    """Render the delete confirmation screen. `send` is update.message.reply_text
    for /delete and query.edit_message_text for the 🗑 Delete option in /edit, so
    both entry points show the same text and the same pair of callbacks."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Delete", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete"),
        ]
    ]
    await send(
        f"⚠️ <b>Delete this {info['record_type']}?</b>\n\n"
        f"{info['detail']}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
    )


async def delete_expense(update: Update, context: CallbackContext):
    """Delete expense with confirmation."""
    if not await is_registered_user(update.effective_user.id):
        await deny_access(update)
        return

    try:
        user_seq = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Format: <code>/delete 3</code>\n"
            "The id is in /list",
            parse_mode='HTML'
        )
        return

    owner_id = update.effective_user.id
    info = await asyncio.to_thread(_prepare_delete_confirmation_sync, owner_id, user_seq)

    if info is None:
        await update.message.reply_text(
            f"❌ No record with ID {user_seq}. /list",
            parse_mode='HTML',
        )
        return

    context.user_data['delete_expense_id'] = info['expense_id']
    await _ask_delete_confirmation(update.message.reply_text, info)


def _confirm_delete_sync(expense_id: int, owner_id: int) -> str | None:
    """Soft-delete the expense. Returns its title, or None if not found/owned."""
    with get_db() as db:
        expense = _get_owned_expense(db, expense_id, owner_id)
        if not expense:
            return None
        title = expense.title
        expense.is_active = False
        expense.deactivated_at = utcnow_naive()
        recompute_after_mutation(db, expense_id=expense_id)
        return title


async def button_callback(update: Update, context: CallbackContext):
    """Handle all button callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("pay_"):
        user_seq = int(data.split("_")[1])
        await mark_as_paid(update, context, user_seq)

    elif data.startswith("move_") or data.startswith("snooze_"):
        # move_<user_seq>_<3h|1d>. The old snooze_ prefix is accepted only
        # for reminder messages sent before the reschedule migration.
        parts = data.split("_")
        try:
            user_seq, code = int(parts[1]), parts[2]
        except (IndexError, ValueError):
            return
        if not await is_registered_user(query.from_user.id):
            return
        result = await asyncio.to_thread(
            _reschedule_from_reminder_sync, query.from_user.id, user_seq, code,
        )
        if result["status"] == "ok":
            due = result["due_local"].strftime("%d.%m.%y %H:%M")
            try:
                await query.edit_message_text(
                    f"📅 <b>{html.escape(result['title'])}</b> - moved to {due}.",
                    parse_mode='HTML',
                )
            except Exception:
                pass

    elif data == "test_pay":
        # Test payment
        await query.edit_message_text(
            "✅ <b>Payment confirmed (test)</b>\n\n"
            "That was a test button.",
            parse_mode='HTML'
        )

    elif data == "confirm_delete":
        # Delete confirmed
        expense_id = context.user_data.get('delete_expense_id')
        owner_id = query.from_user.id
        if expense_id:
            title = await asyncio.to_thread(_confirm_delete_sync, expense_id, owner_id)
            if title is not None:
                await query.edit_message_text(
                    f"✅ Deleted\n\n📌 {html.escape(title)}",
                    parse_mode='HTML'
                )
        context.user_data.clear()

    elif data == "cancel_delete":
        # Delete cancelled
        await query.edit_message_text(
            "❌ Deletion cancelled.",
            parse_mode='HTML'
        )
        context.user_data.clear()

    elif data.startswith("edit_"):
        # This is handled by the conversation handler
        pass

    elif data.startswith("period_") or data.startswith("plan_"):
        # These belong to the /add–/task conversation; reaching here means
        # the conversation didn't take the update. Three distinct cases:
        if context.user_data.get('expense_data'):
            # Dialog is still ALIVE in a text-input state (e.g. double-tap
            # on "+ Another day" while PLAN_CUSTOM_OFFSET waits for a number,
            # or on "Custom days" in CUSTOM_PERIOD) - those states have no
            # callback handlers, so the tap fell through. Don't destroy the
            # active prompt; the user just continues typing.
            return
        if query.message and query.message.message_id in context.chat_data.get('plan_finalized_msg_ids', ()):
            # Stale queued tap on a message plan_done already finalized
            # (double-tap on Done racing the edit). query.message is a
            # press-time snapshot — its reply_markup still shows the old
            # keyboard — so only the message id is trustworthy. Overwriting
            # the success text with "Session reset" would mislead the user
            # into re-creating the already-saved record; do nothing.
            return
        # Genuinely lost session (e.g. bot restarted mid-dialog).
        logger.warning(f"Callback {data} received outside conversation")
        if query.message and query.message.reply_markup:
            await query.edit_message_text(
                "🔄 Session reset.\n\n"
                "Start /add or /task again.",
                parse_mode='HTML'
            )


def _reschedule_from_reminder_sync(owner_id: int, user_seq: int, code: str) -> dict:
    """Move the record's actual due date from a reminder action."""
    with get_db() as db:
        expense = get_expense_by_user_seq(db, owner_id, user_seq)
        if not expense or not expense.is_active:
            return {"status": "not_found"}

        user_tz = get_user_timezone(db, owner_id)
        now_local = now_in_tz(user_tz).replace(second=0, microsecond=0)
        if code == "3h":
            due_local = now_local + timedelta(hours=3)
        elif code == "1d":
            hour = get_user_reminder_hour(db, owner_id)
            due_local = (now_local + timedelta(days=1)).replace(hour=hour, minute=0)
        else:
            return {"status": "bad_code"}

        title = expense.title.split('\n')[0]
        expense.next_payment_date = due_local.date()
        expense.reminder_time = due_local.strftime("%H:%M")

        # A move acknowledges any pre-due wave that falls on today. Without
        # this, moving a due-today task to tomorrow would instantly fire the
        # plan's "one day before" reminder again.
        expense.reminder_slot = None
        expense.reminder_slot_sends = 0
        days_until = (expense.next_payment_date - now_local.date()).days
        active = slot_for_day(plan_from_json(expense.reminder_plan), days_until)
        if days_until > 0 and active and active[0].startswith("n"):
            expense.reminder_slot = active[0]
            expense.reminder_slot_sends = int(slot_times(active[1]))

        recompute_after_mutation(db, expense_id=expense.id)
        return {"status": "ok", "due_local": due_local, "title": title}


def _mark_as_paid_sync(owner_id: int, user_seq: int) -> dict:
    """Load + mutate the expense for a paid/done action. Telegram I/O (message
    cleanup/delete) happens in the caller, off the worker thread."""
    with get_db() as db:
        expense = get_expense_by_user_seq(db, owner_id, user_seq)
        if not expense:
            return {"status": "not_found"}

        expense_id = expense.id
        is_task = expense.expense_type == 'task'
        period = expense.period
        period_days = expense.period_days
        next_payment_date = expense.next_payment_date
        user_id = expense.user_id

        # Handle one-time (no repeats)
        if period == 'none':
            expense.is_active = False
            expense.deactivated_at = utcnow_naive()
            recompute_after_mutation(db, expense_id=expense_id)
            return {
                "status": "done_one_time",
                "expense_id": expense_id,
                "user_id": user_id,
                "is_task": is_task,
            }

        user_tz = get_user_timezone(db, owner_id)
        today = today_in_tz(user_tz)
        # Next due basis is driven by the record's recurrence anchor
        # (scheduled = calendar grid, actual = from the completion day).
        next_date = compute_next_due_date(
            period, period_days, next_payment_date, today, expense.recur_anchor,
        )

        expense.next_payment_date = next_date
        recompute_after_mutation(db, expense_id=expense_id)
        return {"status": "done_recurring", "expense_id": expense_id, "user_id": user_id}


async def mark_as_paid(update: Update, context: CallbackContext, user_seq: int):
    """Mark expense as paid/task done and calculate next date.

    Robustly handles DB session, bulk deletion of consecutive reminder messages,
    and Telegram API errors (e.g. message too old to delete).
    """
    owner_id = update.effective_user.id
    if not await is_registered_user(owner_id):
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "🚫 <b>No access.</b>\n\nRun /start first",
                parse_mode='HTML',
            )
        return

    result = await asyncio.to_thread(_mark_as_paid_sync, owner_id, user_seq)

    if result["status"] == "not_found":
        await update.callback_query.edit_message_text(
            "❌ Record not found.",
            parse_mode='HTML'
        )
        return

    expense_id = result["expense_id"]
    user_id = result["user_id"]

    if result["status"] == "done_one_time":
        await cleanup_reminder_messages(context.application, user_id, expense_id, max_logs=10)
        try:
            await update.callback_query.message.delete()
        except Exception:
            label = "Done" if result["is_task"] else "Paid"
            try:
                await update.callback_query.edit_message_text(
                    f"✅ <b>{label}</b>\n\n"
                    "A one-off record was removed from the list.",
                    parse_mode='HTML'
                )
            except Exception:
                pass  # message gone completely
        return

    # done_recurring
    await cleanup_reminder_messages(context.application, user_id, expense_id, max_logs=15)

    try:
        await setup_scheduler(context.application, run_check=False)
        await trigger_reminder_check(context.application, expense_id)
    except Exception as e:
        logger.error(f"Error rescheduling after mark paid for expense {expense_id}: {e}")

    # Final attempt to delete the clicked message (may already be gone from bulk)
    try:
        await update.callback_query.message.delete()
    except Exception:
        pass  # Already handled or not needed (common for old reminders)


class _PerChatUpdateProcessor(BaseUpdateProcessor):
    """Concurrent across chats, serialized within a chat.

    PTB's built-in SimpleUpdateProcessor (used by plain `.concurrent_updates(n)`)
    just awaits every update's coroutine under a shared semaphore, with no
    per-chat ordering — its own docs warn this is unsafe for stateful handlers
    like ConversationHandler. This bot relies on ConversationHandler/
    context.user_data heavily (/add, /task, /edit, /settings), so two rapid
    updates from the SAME user must not run concurrently (they'd race on the
    same conversation state), while different users must be free to run in
    parallel — otherwise one slow /ai call still blocks everyone else.
    """

    def __init__(self, max_concurrent_updates: int):
        super().__init__(max_concurrent_updates)
        # Grows with distinct chats (~10k users -> ~10k Lock objects, trivial
        # memory); no eviction needed at that scale.
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def do_process_update(self, update, coroutine) -> None:
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            await coroutine
            return
        lock = self._chat_locks.setdefault(chat.id, asyncio.Lock())
        async with lock:
            await coroutine

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# Native command menu (Telegram «/» list + Menu button). /start and
# /test_button are intentionally omitted — /start is the entry link, and
# /test_button is a dev-only diagnostic.
BOT_COMMANDS = [
    ("ai", "Turn the AI assistant on or off"),
    ("add", "Add a payment"),
    ("task", "Add a task"),
    ("list", "List payments and tasks"),
    ("edit", "Change a record - /edit ID"),
    ("delete", "Delete a record - /delete ID"),
    ("settings", "Time zone, currency, reminders, sorting"),
    ("cancel", "Cancel the current step"),
    ("privacy", "Data processing"),
    ("help", "Help"),
]

BOT_SHORT_DESCRIPTION = (
    "Reminders for payments and tasks. With /ai on, write as in a normal "
    "chat - voice too. Or /add, /task."
)


async def _post_init(application):
    """Runs once inside the PTB event loop on startup: start the scheduler,
    then publish the command menu + short description. The Telegram calls are
    wrapped so a transient API hiccup can't block the bot from starting."""
    await setup_scheduler(application)
    try:
        await application.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        await application.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception as e:
        logger.warning("Publishing command menu / description failed (non-fatal): %s", e)


def main():
    """Start the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    missing = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.DB_ENCRYPTION_KEY:
        missing.append("DB_ENCRYPTION_KEY")
    if missing:
        names = ", ".join(missing)
        raise SystemExit(
            f"Missing required environment variable(s): {names}. "
            "Copy .env.example to .env and follow README.md."
        )

    # Initialize project directories + database (using central config)
    config.ensure_directories()
    config.init_database()

    # Optional ops alerting (no-ops unless ADMIN_CHAT_ID is set — see monitoring.py).
    # Registering the bot/loop happens later in setup_scheduler(), once the
    # event loop is actually running; the handler is safe to attach now.
    logging.getLogger().addHandler(monitoring.DatabaseLockAlertHandler())

    application = (
        Application.builder()
        .token(token)
        .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
        # Without this, PTB dispatches updates from ALL users one at a time —
        # one slow /ai call or DB wait stalls the whole bot. _PerChatUpdateProcessor
        # keeps different chats concurrent (256 at once, matching PTB's own
        # default for concurrent_updates=True) while serializing same-chat
        # updates so ConversationHandler/context.user_data can't race.
        .concurrent_updates(_PerChatUpdateProcessor(256))
        # Rate-limits every outgoing Bot API call (Telegram's ~30/s global,
        # ~1/s per chat) and auto-retries on 429 — covers scheduler.py sends
        # too, since it goes through the same Application/Bot instance.
        .rate_limiter(AIORateLimiter())
        .build()
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("privacy", privacy_command))
    application.add_handler(CommandHandler("list", list_expenses))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("test_button", test_button))
    application.add_handler(CommandHandler("delete", delete_expense))

    # allow_reentry: SETTINGS_MENU only matches inline callbacks, and the menu
    # stays open until a completing tap or /cancel. Without reentry a second
    # /settings (the usual "let me look again" path) matches neither the state
    # handlers nor /cancel, so PTB drops it in silence.
    conv_settings = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            SETTINGS_MENU: [CallbackQueryHandler(settings_callback, pattern="^(settings_|tzsel_|currsel_)")],
            SETTINGS_TZ_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_tz_search)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv_settings)

    # Add conversation handler for /add
    conv_add = ConversationHandler(
        entry_points=[CommandHandler("add", add_expense_command)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            PERIOD: [CallbackQueryHandler(get_period, pattern="^period_")],
            CUSTOM_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_period)],
            PLAN_EDITOR: [CallbackQueryHandler(plan_editor_callback, pattern="^plan_")],
            PLAN_CUSTOM_OFFSET: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_custom_offset)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # A second /add restarts the dialog instead of being dropped in
        # silence — none of the states above match a command.
        allow_reentry=True,
    )
    application.add_handler(conv_add)

    # Add conversation handler for /task (shares period/plan handlers with
    # /add — flows differ only by expense_data['expense_type'])
    conv_task = ConversationHandler(
        entry_points=[CommandHandler("task", add_task)],
        states={
            TASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_title)],
            TASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_date)],
            TASK_PERIOD: [CallbackQueryHandler(get_period, pattern="^period_")],
            CUSTOM_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_period)],
            PLAN_EDITOR: [CallbackQueryHandler(plan_editor_callback, pattern="^plan_")],
            PLAN_CUSTOM_OFFSET: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_custom_offset)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # same as /add above
    )
    application.add_handler(conv_task)

    # Add conversation handler for /edit
    conv_edit = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_expense)],
        states={
            # Pattern-scoped on purpose: this state must not take a tap that
            # belongs to another screen (a stale plan editor, a delete
            # confirmation), which a re-entered /edit can leave on display.
            EDIT_FIELD: [CallbackQueryHandler(edit_field_selected, pattern="^edit_")],
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value),
                CallbackQueryHandler(edit_period_selected, pattern="^new_period_")
            ],
            EDIT_CUSTOM_PERIOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_custom_period)
            ],
            # "🔔 Reminders" reopens the plan editor for this record (shared
            # handlers with /add–/task; edit vs create keyed by
            # user_data['plan_edit_expense_id'] in plan_editor_callback)
            PLAN_EDITOR: [CallbackQueryHandler(plan_editor_callback, pattern="^plan_")],
            PLAN_CUSTOM_OFFSET: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_custom_offset)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # /edit ID while the field menu of another ID is open: without this
        # the second command matched nothing and the bot went quiet.
        allow_reentry=True,
    )
    application.add_handler(conv_edit)

    # Let handle_ics_document check whether a user is mid-conversation in any
    # of these before treating a bare document drop as an .ics auto-import.
    _TRACKED_CONVERSATION_HANDLERS.extend([conv_settings, conv_add, conv_task, conv_edit])

    # Global /cancel — registered AFTER the ConversationHandlers on purpose:
    # PTB runs the first matching handler per group, so registering it before
    # them would swallow /cancel and leave the conversation state alive (the
    # fallbacks would never fire); the next message would then hit a state
    # handler with cleared user_data. Outside a conversation this still
    # answers "Cancelled".
    application.add_handler(CommandHandler("cancel", cancel))

    # Permanent AI free-text (only when users.ai_mode is ON). After ConversationHandlers
    # so /add|/task|/edit|/settings step text keeps priority when those states match;
    # free_text_ai_handler also no-ops via _user_in_active_conversation for callback-only
    # states (PLAN_EDITOR etc.) where free text would otherwise fall through.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_ai_handler)
    )

    # Voice notes (the microphone button): transcribed, then routed exactly
    # like the free text above — same AI-mode gate, same rate limit.
    application.add_handler(
        MessageHandler(filters.VOICE, voice_ai_handler)
    )

    # Any .ics dropped on the bot directly becomes a task right away (unless
    # AI mode is ON and the caption is free text — then AI path; caption /ai
    # toggles mode). PTB never matches CommandHandler on captions.
    application.add_handler(MessageHandler(filters.Document.ALL, handle_ics_document))

    # Add callback handler for buttons
    application.add_handler(CallbackQueryHandler(button_callback))

    # Scheduler + native command menu (shown in Telegram's «/» list and the
    # blue Menu button — discoverability, no need to remember commands).
    application.post_init = _post_init

    # Start polling. A token typo is by far the most common first-run
    # mistake, and PTB surfaces it as a traceback out of its own internals —
    # not something a reader of the quick start can act on.
    logger.info("Bot started!")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Copy it again from "
            "@BotFather into .env, with no quotes and no surrounding spaces."
        ) from None


if __name__ == "__main__":
    main()
