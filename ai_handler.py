import asyncio
import html
import json
import os
import logging
import re
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()  # defensive: normally already loaded via bot.py's import chain,
    # but this module shouldn't silently read a None API key if ever imported
    # standalone (idempotent — safe to call more than once).
except ImportError:
    pass

from datetime import datetime
from database import (
    Expense, User, AISession, get_db, get_user_timezone,
    get_user_default_currency, add_expense, get_expense_by_user_seq, utcnow_naive,
    snapshot_user_data, list_user_backups, restore_user_backup,
    normalize_recur_anchor, apply_recur_anchor,
)
from currency import SETTINGS_CURRENCIES, currency_symbol
from reminder_engine import recompute_after_mutation
import reminder_plan
from utils import parse_date, now_in_tz, today_in_tz, compute_next_due_date
import config  # central project configuration

logger = logging.getLogger(__name__)

BACKUP_DIR = config.BACKUP_DIR
MAX_BACKUPS = config.MAX_BACKUPS

# Whole-file database snapshots are named "<prefix><timestamp>.db". daily_backup
# matches today's file by this prefix, so both must stay in sync — hence one
# constant. Rotation matches any *.db, so files left by an older prefix still
# age out normally.
DB_BACKUP_PREFIX = "spendalert_"

MODIFYING_FUNCTIONS = {
    "create_expense", "create_task", "delete_expense",
    "mark_expense_done", "reschedule_expense", "edit_expense",
    "set_reminder_plan", "restore_backup",
}

# Functions the generic tool-loop pre-action snapshot (below) should fire
# for. restore_backup is excluded: execute_restore_backup already takes its
# own dedicated "before_restore" snapshot immediately before looking up the
# requested backup_id, so an extra generic snapshot here would (a) be a
# redundant second prune-to-MAX_USER_BACKUPS_PER_USER pass and (b) risk
# evicting the very backup_id the user asked to restore before
# restore_user_backup ever gets to look it up.
AUTO_SNAPSHOT_FUNCTIONS = MODIFYING_FUNCTIONS - {"restore_backup"}


def _snapshot_before_ai_action(user_id: int, label: str = "ai_auto") -> int:
    """Sync wrapper for snapshot_user_data, run via asyncio.to_thread from the
    tool loop below."""
    with get_db() as db:
        return snapshot_user_data(db, user_id, label=label)

# User messages that require a modifying tool call.
_ACTION_INTENT_RE = re.compile(
    r"\b(?:add|creat|delet|remov|mark|complet|paid|pay|done|move|resched|shift"
    r"|chang|edit|swap|renam)",
    re.IGNORECASE,
)


def parse_expense_id(value):
    """Accept int or numeric string from AI tool arguments."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def md_to_html(text):
    """Convert basic Markdown to Telegram-compatible HTML."""
    if not text:
        return ""
    text = str(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'___(.+?)___', r'<u>\1</u>', text)
    return text


def _model_content(message: dict) -> str | None:
    """Extract non-empty assistant text; API may return content=null after tool rounds."""
    content = message.get("content")
    if content is None:
        return None
    text = str(content).strip()
    return text or None


def _last_tool_user_message(messages: list[dict], exclude_ids: set | None = None) -> str | None:
    """Fallback when the model omits a final user-facing reply after tool calls.

    exclude_ids skips read-only calls (list_expenses/list_backups) — their
    result is a raw pipe-delimited dump built for the AI's own context, not
    for display, and leaking it verbatim would fail readability for any user
    if the model ever skips its required closing reply after one of those."""
    exclude_ids = exclude_ids or set()
    for entry in reversed(messages):
        if entry.get("role") != "tool" or entry.get("tool_call_id") in exclude_ids:
            continue
        try:
            data = json.loads(entry.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        msg = (data.get("message") or "").strip()
        if msg:
            return msg
    return None


def _format_ai_display_text(raw_final: str | None) -> str:
    text = md_to_html(raw_final or "")
    return text.strip() or "✅ Done!"


def _aggregate_modify_tool_results(
    modify_tool_results: list[dict],
    *,
    expense_ids: list | None = None,
    cleanup: list | None = None,
    fallback_text: str | None = None,
) -> dict:
    """Turn ordered modifying-tool results into one user-facing outcome.

    Mixed success+failure keeps every tool message in order and overall
    success=True when any tool succeeded (so Telegram does not prefix the
    whole reply with an error prefix when work was already applied). All-fail
    success=False with every failure message still listed. Ids/cleanup from
    successful tools are never discarded because a sibling failed.
    """
    if not modify_tool_results:
        raise ValueError("modify_tool_results must be non-empty")

    any_success = any(r.get("success") for r in modify_tool_results)
    tool_lines = [r["message"] for r in modify_tool_results if r.get("message")]
    if tool_lines:
        raw_final = "\n\n".join(tool_lines)
    elif any_success:
        raw_final = (fallback_text or "").strip() or "✅ Done!"
    else:
        raw_final = (fallback_text or "").strip() or "❌ The action failed"

    return {
        "success": bool(any_success),
        "raw_final": raw_final,
        "message": _format_ai_display_text(raw_final),
        "expense_ids": list(expense_ids or []),
        "cleanup": list(cleanup or []),
    }


# System prompt for the AI
SYSTEM_PROMPT = """You are an assistant for managing expenses and tasks in Telegram bot "SpendAlert".

Your job is to understand user requests and call the appropriate functions to work with the database.

Available functions:
1. create_expense - create a payment (with money)
2. create_task - create a task (without money)
3. list_expenses - show all active records (use first to find ID by name)
4. delete_expense - delete a record by ID
5. mark_expense_done - mark a task as done or payment as paid (like clicking "Done"/"Paid")
6. reschedule_expense - change the due date of a task or payment
7. edit_expense - change period, period_days, title, amount, or currency
8. set_reminder_plan - change WHEN/HOW OFTEN reminders fire for a record (days before due, on the due day, after overdue). NOT the due date and NOT the repeat period.

Rules:
- For regular tasks (every day, every week) use period='custom' and specify period_days
- For one-time (no repeats) use period='none'
- For payments always specify amount and currency (RUB, USD, EUR, GBP, CNY)
- Specify date in DD.MM.YY format, optionally with time HH:MM (e.g. 25.06.26 16:29). Use CURRENT DATE AND TIME from the system message as "now".
- If user asks to create a task without mentioning money - you MUST call create_task (never skip the tool call)
- If user asks to create a payment with amount - you MUST call create_expense
- Currency is detected from text: "$10" = USD, "€20" = EUR, "750" = user's default currency
- When user says "every day" set period='custom' and period_days=1
- When user says "every week" set period='custom' and period_days=7
- When user asks to move/reschedule something, use reschedule_expense with new date
- When user says task is done or payment is paid, use mark_expense_done
- Reminder schedule vs due date/period are DIFFERENT: "remind me a week before / on the day / less often / do not remind me" -> set_reminder_plan (does NOT move the date); "move it to the 20th" -> reschedule_expense; "every month" -> period. Only pass the set_reminder_plan dimensions the user mentioned; the rest stays. New records start with the default plan (3/2/1 days before, on the due day, after it) - no need to call set_reminder_plan unless the user wants something different.
- recur_anchor controls HOW the next due date is computed after a recurring record is marked done. Default is 'scheduled' (fixed calendar grid: next = planned date + period). Use 'actual' ONLY when the user clearly wants the cadence to restart from the day they actually did it - e.g. "count N days from the day I replaced/did it", "every N days FROM COMPLETION". Set it on create_expense/create_task, or change it later with edit_expense. It does NOT move the current due date and is ignored for one-time records.
- title supports multiple lines (\n): the FIRST line is a short name shown in the compact /list view; any further lines (the "note") are hidden there but shown in FULL in the reminder message when it fires. USE THIS ACTIVELY: whenever the user's request contains a link, address, phone number, booking/order code, or other long details, put a SHORT name on line 1 and those details on line 2+ — never cram them into line 1 and never silently drop them. Example: title="Team sync\nhttps://meet.example.com/xyz". To ADD a note to an existing record, call edit_expense with title = its current first line + "\n" + the note. When renaming a record that already has a note (list_expenses shows it as "note: ..."), keep the note lines unless the user asks to remove them.

SCOPE — strictly on-topic (never violate):
- You ONLY handle expenses, payments, tasks and reminders of this bot. Nothing else.
- NEVER tell jokes, anecdotes, stories, poems, or chat about topics unrelated to the bot's functions — even if the user explicitly asks. No exceptions.
- If the request is off-topic, reply with ONE short sentence: "I only help with expenses, tasks and reminders. What can I do for you there?" - and nothing more. Do not fulfill the off-topic request "partially" or "as a bonus".

CRITICAL — database actions (never violate):
- To create / delete / edit / mark done / reschedule you MUST call the matching function. Never claim an action succeeded without a successful tool result.
- NEVER invent expense IDs. Use only IDs returned by tools or from the DB snapshot / list_expenses.
- If a tool returns success:false, show the user that error — do NOT say the action succeeded.
- For relative times ("in 2 minutes", "at 16:29"): compute absolute date+time from CURRENT DATE AND TIME and pass to create_task as DD.MM.YY HH:MM.
- Your chat reply is supplementary; the bot shows users the tool result message as the source of truth.

Always reply to user after executing an action.
- If user refers to a task or payment by name (not ID), FIRST call list_expenses to find its ID, then call the needed function with that ID.
- You can call multiple functions sequentially - first to get info, then to act.
- Recent conversation history (up to 6 user turns with assistant replies) may be provided. Use it for context.
- User messages may include Telegram context blocks:
  [Forwarded message from the user], [Reply to a message], [Reply to a message from another chat], [Quote], [User request].
  Treat forwarded/replied text as source material; the user's request may be only in [User request] or implied.

Formatting rules for Telegram (use ONLY HTML, NOT Markdown):
- <b>bold</b> for important words (NOT ** or any Markdown)
- <i>italic</i> for emphasis
- <code>code</code> for IDs or technical values
- Never use Markdown syntax like ** or * or __
- Responses must be in English, keep them concise and professional
- Use minimal emojis, only where truly needed
- Write every phone number in international format: a leading + followed by digits only, like +79991234567 — never with spaces, dashes, or parentheses

IMPORTANT: Your final response to user must use HTML formatting (<b>, <i>, <code>), NOT Markdown (** * _)."""

# OpenRouter is optional, but its key must come from the environment. There is
# deliberately no hard-coded fallback.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)

if not OPENROUTER_API_KEY:
    logger.warning(
        "OPENROUTER_API_KEY is not set: /ai will fail until it is added to .env "
        "(see .env.example)."
    )

# Whole-call time budget (all up-to-5 rounds combined) — without this, a
# slow/degraded OpenRouter could hold one /ai call for up to 5x60s=300s, and
# since every user's updates are serialized per-chat (see bot.py's
# _PerChatUpdateProcessor), that user's chat is unresponsive for the whole
# duration. A bounded semaphore caps how many OpenRouter requests are ever
# in flight at once, so a traffic spike degrades (queues) instead of each
# request opening its own unbounded connection.
OPENROUTER_CALL_BUDGET_SECONDS = 45
OPENROUTER_ROUND_TIMEOUT_SECONDS = 20
_OR_SEMAPHORE = asyncio.Semaphore(25)
_or_client: httpx.AsyncClient | None = None

# AI dialog: 6 user turns + assistant replies; reset after 6/6 or 10 min idle
MAX_DIALOG_TURNS = 6
SESSION_TIMEOUT_MINUTES = 10

# Shared param: how the NEXT due date is computed when a recurring record is
# marked done (see database.RECUR_ANCHOR_*). Optional — defaults to scheduled.
_RECUR_ANCHOR_PARAM = {
    "type": "string",
    "enum": ["scheduled", "actual"],
    "description": (
        "How the NEXT due date is set after this recurring record is marked "
        "done. 'scheduled' (default) = keep the fixed calendar grid: next = "
        "planned date + period, regardless of when the user actually did it "
        "(bills, rent, subscriptions). 'actual' = restart the cadence from the "
        "day it was actually done: next = completion day + period (chores done "
        "early/late, e.g. 'count from the day I actually did it'). Only "
        "set 'actual' if the user clearly wants counting from the actual day. "
        "Ignored for one-time records (period='none')."
    ),
}

# Function definitions for OpenRouter tool calling
FUNCTIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_expense",
            "description": "Create a new expense (payment). Use for regular payments like subscriptions, bills, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Name of the expense, e.g. 'Spotify', 'Internet'. Optionally multi-line: first line shown in /list, extra lines (\\n) hidden there but shown in full in the reminder — use for a link/address/note."},
                    "amount": {"type": "number", "description": "Amount of money"},
                    "currency": {"type": "string", "enum": list(SETTINGS_CURRENCIES), "description": "Currency"},
                    "date": {"type": "string", "description": "Date in DD.MM.YY format, optionally with time HH:MM (e.g. '20.06.26 19:00'). Use today's date as reference."},
                    "period": {"type": "string", "enum": ["month", "quarter", "year", "custom", "none"], "description": "Period: month=every month, quarter=every 3 months, year=every year, custom=specify days, none=one-time (no repeats)"},
                    "period_days": {"type": "integer", "description": "Number of days for custom period. Required if period='custom'"},
                    "recur_anchor": _RECUR_ANCHOR_PARAM
                },
                "required": ["title", "amount", "currency", "date", "period"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task (reminder without money). Use for tasks like 'renew subscription', 'file tax return', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Name of the task. Optionally multi-line: first line shown in /list, extra lines (\\n) hidden there but shown in full in the reminder — use for a link/address/note."},
                    "date": {"type": "string", "description": "Date in DD.MM.YY format, optionally with time HH:MM (e.g. '20.06.26 19:00'). Use today's date as reference."},
                    "period": {"type": "string", "enum": ["month", "quarter", "year", "custom", "none"], "description": "Period: month=every month, quarter=every 3 months, year=every year, custom=specify days, none=one-time (no repeats)"},
                    "period_days": {"type": "integer", "description": "Number of days for custom period. Required if period='custom'"},
                    "recur_anchor": _RECUR_ANCHOR_PARAM
                },
                "required": ["title", "date", "period"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_expenses",
            "description": "List all active expenses and tasks with their IDs, dates, and periods. Use this FIRST when user refers to a task or payment by name instead of ID - find the ID here, then use it in other functions.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_expense",
            "description": "Delete an expense or task by ID. Works for both payments and tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID of the expense/task to delete"}
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_expense_done",
            "description": "Mark a task as completed or a payment as paid (like clicking the Done/Paid button). For one-time records this deactivates them. For recurring ones it moves to next date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID of the expense/task to mark as done"}
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_expense",
            "description": "Change the due date of a task or payment. Use when user asks to move, reschedule, or postpone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID of the expense/task to reschedule"},
                    "new_date": {"type": "string", "description": "New due date in DD.MM.YY format, optionally with time HH:MM"}
                },
                "required": ["id", "new_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_expense",
            "description": "Edit an existing expense or task - change its title, period, period_days, amount, or currency. Use when user wants to change how often something repeats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID of the expense/task to edit"},
                    "title": {"type": "string", "description": "New name (optional). Optionally multi-line: first line shown in /list, extra lines (\\n) hidden there but shown in full in the reminder — use for a link/address/note."},
                    "period": {"type": "string", "enum": ["month", "quarter", "year", "custom", "none"], "description": "New period (optional)"},
                    "period_days": {"type": "integer", "description": "Days for custom period (required if period='custom')"},
                    "amount": {"type": "number", "description": "New amount (optional, payments only)"},
                    "currency": {"type": "string", "enum": list(SETTINGS_CURRENCIES), "description": "New currency (optional)"},
                    "recur_anchor": _RECUR_ANCHOR_PARAM
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder_plan",
            "description": (
                "Set or change the REMINDER SCHEDULE of an existing payment/task — WHEN and "
                "HOW OFTEN the user is reminded. Use for requests like 'remind me a week and a "
                "day before', 'only remind me on the due day', 'once a day after it is due', "
                "'stop reminding me about this', 'remind me more/less often'. This does NOT "
                "change the due date or the "
                "repeat period (use reschedule_expense / edit_expense for those). Only include the "
                "dimensions the user mentioned; omitted ones stay as they are. If the user refers "
                "to the record by name, call list_expenses first to get its id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID of the payment/task"},
                    "pre_due_days": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Days BEFORE the due date to remind, each fires once. E.g. [7,1] = a "
                            "week before and a day before. [] = no reminders before the due date. "
                            "Max 4 values, each 1..365, no duplicates. Default plan is [3,2,1]."
                        ),
                    },
                    "due_day": {
                        "type": "string",
                        "enum": ["once", "repeat", "off"],
                        "description": "On the due day: once=a single reminder, repeat=every couple hours, off=nothing.",
                    },
                    "overdue": {
                        "type": "string",
                        "enum": ["daily", "repeat", "off"],
                        "description": "After the due date passed: daily=once a day, repeat=every couple hours, off=nothing.",
                    },
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_backups",
            "description": "List the user's saved data snapshots taken automatically before recent AI-driven changes. Useful when the user asks to undo, roll back, cancel recent changes, or restore a previous version of their data.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restore_backup",
            "description": "Restore the user's expenses and settings to a previously saved snapshot, undoing everything since that point. Always call list_backups first and confirm the exact snapshot date and time with the user before calling this, since it overwrites current data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "backup_id": {"type": "integer", "description": "ID of the backup to restore, obtained from list_backups"}
                },
                "required": ["backup_id"]
            }
        }
    }
]


# =============================================================================
# Conversation history helpers (6 user turns + AI replies per dialog)
#
# Each user's session is its own row in ai_sessions (database.AISession) —
# was previously one shared JSON file for ALL users, rewritten in full on
# every /ai call with no locking (a correctness + scalability bug at more
# than a handful of concurrent users).
# =============================================================================

def _count_user_turns(messages):
    return sum(1 for m in messages if m.get("role") == "user")


def _trim_messages(messages):
    """Keep at most MAX_DIALOG_TURNS user+assistant pairs."""
    return messages[-(MAX_DIALOG_TURNS * 2):]


def _get_user_session_sync(user_id: int) -> tuple[list, int, datetime, str]:
    """Load session; reset silently after SESSION_TIMEOUT_MINUTES of inactivity."""
    with get_db() as db:
        user_tz = get_user_timezone(db, user_id)
        session = db.query(AISession).filter(AISession.user_id == user_id).first()
        messages = json.loads(session.messages_json) if session and session.messages_json else []
        last_activity = session.last_activity if session else None

    if last_activity is not None:
        elapsed = (utcnow_naive() - last_activity).total_seconds()
        if elapsed > SESSION_TIMEOUT_MINUTES * 60:
            logger.info(f"AI session reset for user {user_id} after {SESSION_TIMEOUT_MINUTES}m idle")
            messages = []

    messages = _trim_messages(messages)
    turn_count = _count_user_turns(messages)
    now = now_in_tz(user_tz)  # for prompt display only — not related to session TTL
    return messages, turn_count, now, user_tz


def _save_user_session_sync(user_id: int, messages: list) -> int:
    """Persist session and return current dialog turn count (1..6)."""
    messages = _trim_messages(messages)
    with get_db() as db:
        session = db.query(AISession).filter(AISession.user_id == user_id).first()
        if not session:
            session = AISession(user_id=user_id)
            db.add(session)
        session.messages_json = json.dumps(messages, ensure_ascii=False)
        session.last_activity = utcnow_naive()
    return _count_user_turns(messages)


def _get_or_client() -> httpx.AsyncClient:
    """Module-level shared client (one TCP/TLS connection pool reused across
    rounds and across users) instead of a fresh httpx.AsyncClient() per round."""
    global _or_client
    if _or_client is None or _or_client.is_closed:
        _or_client = httpx.AsyncClient(
            timeout=httpx.Timeout(OPENROUTER_ROUND_TIMEOUT_SECONDS),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _or_client


async def _post_openrouter(payload: dict, headers: dict) -> httpx.Response:
    """POST to OpenRouter, bounded by a concurrency semaphore, with one
    backoff retry on transient errors (429/500/502/503)."""
    client = _get_or_client()
    async with _OR_SEMAPHORE:
        response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
    if response.status_code in (429, 500, 502, 503):
        await asyncio.sleep(1.5)
        async with _OR_SEMAPHORE:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
    return response


async def call_openrouter(user_message, user_id, intent_text: str | None = None):
    """Call OpenRouter API with function calling loop (up to 5 rounds).
    Dialog context: up to 6 user turns + replies; resets after 6/6 or 10 min idle.
    Performs one DB backup before the first modifying action in the call (not
    once per tool call — see backed_up_this_call below).

    intent_text: typed /ai args only (excludes reply/forward context blocks).
    Used for action-intent guard — avoids false positives from forwarded receipts.
    """
    try:
        history, turn_count, now, user_tz = await asyncio.to_thread(_get_user_session_sync, user_id)

        # After 6/6 the next user message starts a fresh dialog
        if turn_count >= MAX_DIALOG_TURNS:
            logger.info(f"AI dialog reset for user {user_id} after {MAX_DIALOG_TURNS} turns")
            history = []
            turn_count = 0

        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        time_context = (
            f"\n\n"
            f"CURRENT DATE AND TIME ({user_tz}):\n"
            f"- Today: {day_names[now.weekday()]}, {now.strftime('%d.%m.%y')} ({now.strftime('%Y-%m-%d')})\n"
            f"- Time: {now.strftime('%H:%M')} ({user_tz.replace('_', ' ')})\n"
            f"- Weekday number (0=Mon ... 6=Sun): {now.weekday()}\n"
            f"Always use this date as \"today\" when computing due dates and parsing relative dates."
        )
        system_content = SYSTEM_PROMPT + time_context

        is_dialog_start = turn_count == 0
        if is_dialog_start:
            db_result = await asyncio.to_thread(fetch_db_snapshot_readonly, user_id)
            snapshot = db_result.get("message", "DB read error")
            system_content += (
                "\n\nCURRENT DB STATE (readonly, auto-loaded at dialog turn 1/6):\n"
                f"{snapshot}\n"
                "Use these IDs and dates when handling the request. Call list_expenses to refresh if needed."
            )
            logger.info(f"AI dialog 1/6: readonly DB snapshot loaded for user {user_id}")

        messages = [
            {"role": "system", "content": system_content}
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }

        max_rounds = 5
        final_text = None
        modified_expense_ids = []
        cleanup_pairs = []
        modify_tool_results: list[dict] = []
        readonly_tool_call_ids: set = set()
        backed_up_this_call = False

        async def _run_tool_loop():
            nonlocal final_text, backed_up_this_call

            for _round in range(max_rounds):
                payload = {
                    "model": OPENROUTER_MODEL,
                    "messages": messages,
                    "tools": FUNCTIONS,
                    "tool_choice": "auto",
                    # Low temperature: deterministic tool calls, no "creative"
                    # off-topic replies (scope is also enforced in SYSTEM_PROMPT).
                    "temperature": 0.2
                }

                response = await _post_openrouter(payload, headers)

                if response.status_code != 200:
                    logger.error(f"OpenRouter API error: {response.status_code} - {response.text}")
                    return {"success": False, "message": "❌ The AI is temporarily unavailable. Try again in a minute."}

                data = response.json()
                message = data["choices"][0]["message"]

                # Add model response to message history
                messages.append(message)

                if "tool_calls" not in message or not message["tool_calls"]:
                    final_text = _model_content(message)
                    break

                # Execute all tool calls (with backup before each modifying action)
                for tool_call in message["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    try:
                        arguments = json.loads(tool_call["function"]["arguments"])
                    except json.JSONDecodeError:
                        logger.error(f"AI returned invalid JSON arguments: {tool_call['function']['arguments']}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": "Error: Invalid JSON arguments provided. Please try again."
                        })
                        continue

                    tool_call_id = tool_call["id"]
                    logger.info(f"AI round {_round+1}: {function_name}({arguments})")

                    # Snapshot once per /ai call, before the first DB-modifying action
                    # (not once per tool call — a multi-item request like "delete 3,
                    # 5 and 7" used to trigger this 3 times in a row). This is a
                    # lightweight per-user JSON snapshot (a few KB, only this user's
                    # rows) — not a full database file copy, so one user's AI edit no
                    # longer pays the IO/CPU cost of copying every other user's data.
                    # daily_backup (scheduler, every 24h, unchanged) remains the
                    # separate whole-system disaster-recovery safety net.
                    if function_name in AUTO_SNAPSHOT_FUNCTIONS and not backed_up_this_call:
                        logger.info(f"Snapshot before AI DB action: {function_name}")
                        await asyncio.to_thread(_snapshot_before_ai_action, user_id, "ai_auto")
                        backed_up_this_call = True

                    result = await asyncio.to_thread(
                        execute_function, user_id, function_name, arguments, user_tz,
                    )

                    if function_name in MODIFYING_FUNCTIONS:
                        modify_tool_results.append(result)
                        if result.get("success"):
                            expense_id = result.get("expense_id")
                            if expense_id:
                                modified_expense_ids.append(expense_id)
                            if function_name == "mark_expense_done" and expense_id:
                                cleanup_pairs.append((user_id, expense_id))
                    else:
                        readonly_tool_call_ids.add(tool_call_id)

                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            return None

        try:
            # asyncio.wait_for keeps compatibility with Python 3.10.
            early_result = await asyncio.wait_for(_run_tool_loop(), timeout=OPENROUTER_CALL_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(f"OpenRouter call budget ({OPENROUTER_CALL_BUDGET_SECONDS}s) exceeded for user {user_id}")
            return {"success": False, "message": "❌ The AI is temporarily unavailable (timed out), try again later"}
        if early_result is not None:
            return early_result

        if not final_text:
            final_text = _last_tool_user_message(messages, readonly_tool_call_ids)

        if modify_tool_results:
            # Any-success ⇒ overall success (mixed results keep success + failure
            # messages and never drop expense_ids/cleanup from successful siblings).
            # All-fail ⇒ failure with every failure message, empty ids/cleanup.
            agg = _aggregate_modify_tool_results(
                modify_tool_results,
                expense_ids=modified_expense_ids,
                cleanup=cleanup_pairs,
                fallback_text=final_text,
            )
            if not agg["success"]:
                return {
                    "success": False,
                    "message": agg["message"],
                    "dialog_count": _count_user_turns(history),
                    "expense_ids": agg["expense_ids"],
                    "cleanup": agg["cleanup"],
                }
            raw_final = agg["raw_final"]
            display_text = agg["message"]
            modified_expense_ids = agg["expense_ids"]
            cleanup_pairs = agg["cleanup"]
        elif (
            not readonly_tool_call_ids
            and (intent_text or "").strip()
            and _ACTION_INTENT_RE.search(intent_text)
        ):
            logger.warning(
                "AI action intent without modifying tool call for user %s: %r",
                user_id, intent_text[:120],
            )
            return {
                "success": False,
                "message": (
                    "❌ <b>Nothing was written to the database.</b>\n\n"
                    "No record was created or changed. Try again, or use "
                    "<code>/task</code> / <code>/add</code>."
                ),
                "dialog_count": _count_user_turns(history),
                "expense_ids": [],
                "cleanup": [],
            }
        else:
            raw_final = final_text or _last_tool_user_message(messages, readonly_tool_call_ids) or "✅ Done!"
            display_text = _format_ai_display_text(raw_final)

        history_assistant_text = raw_final if isinstance(raw_final, str) and raw_final.strip() else display_text
        new_history = list(history) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": history_assistant_text},
        ]
        dialog_count = await asyncio.to_thread(_save_user_session_sync, user_id, new_history)

        return {
            "success": True,
            "message": display_text,
            "dialog_count": dialog_count,
            "expense_ids": modified_expense_ids,
            "cleanup": cleanup_pairs,
        }

    except Exception as e:
        logger.error(f"OpenRouter API error: {e}")
        return {"success": False, "message": "❌ Could not reach the AI. Please try again."}


def execute_function(user_id, function_name, arguments, user_tz=None):
    """Execute a function by name."""
    if user_tz is None:
        with get_db() as db:
            user_tz = get_user_timezone(db, user_id)
    if function_name == "create_expense":
        return execute_create_expense(user_id, arguments, user_tz)
    elif function_name == "create_task":
        return execute_create_task(user_id, arguments, user_tz)
    elif function_name == "list_expenses":
        return execute_list_expenses(user_id, user_tz)
    elif function_name == "delete_expense":
        return execute_delete_expense(user_id, arguments)
    elif function_name == "mark_expense_done":
        return execute_mark_done(user_id, arguments, user_tz)
    elif function_name == "reschedule_expense":
        return execute_reschedule(user_id, arguments, user_tz)
    elif function_name == "edit_expense":
        return execute_edit_expense(user_id, arguments)
    elif function_name == "set_reminder_plan":
        return execute_set_reminder_plan(user_id, arguments)
    elif function_name == "list_backups":
        return execute_list_backups(user_id)
    elif function_name == "restore_backup":
        return execute_restore_backup(user_id, arguments)
    else:
        return {"success": False, "message": f"❌ Unknown function: {function_name}"}


def _normalize_title(raw):
    """Hygiene for model-produced titles: some models emit a literal
    backslash-n instead of a real newline in the JSON string — without this
    the "note on line 2+" feature silently degrades to a title containing
    the two characters \\n."""
    if not isinstance(raw, str):
        return raw
    return raw.replace("\\n", "\n").strip()


def _title_confirmation(title: str) -> str:
    """First line for the success message + a note marker, so a multi-line
    title doesn't paste a raw line break into the middle of the reply."""
    first, *note = title.split("\n")
    first = html.escape(first)
    return f"{first} (+ note)" if any(l.strip() for l in note) else first


def execute_create_expense(user_id, args, user_tz):
    """Execute create_expense function. Uses safe context manager + validation."""
    try:
        args['title'] = _normalize_title(args.get('title'))
        if not args.get('title') or not isinstance(args.get('amount'), (int, float)):
            return {"success": False, "message": "Invalid arguments for creating a payment"}

        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return {"success": False, "message": "User not found"}

            date, reminder_time = parse_date(args['date'], user_tz)
            if date < today_in_tz(user_tz):
                return {"success": False, "message": "A payment date cannot be in the past"}

            expense = add_expense(
                db,
                user_id=user.id,
                expense_type='payment',
                title=args['title'],
                amount=args['amount'],
                currency=args.get('currency') or get_user_default_currency(db, user_id),
                next_payment_date=date,
                period=args['period'],
                period_days=args.get('period_days'),
                recur_anchor=normalize_recur_anchor(args.get('recur_anchor')),
                reminder_time=reminder_time,
            )
            expense_id = expense.id
            user_seq = expense.user_seq
            recompute_after_mutation(db, expense_id=expense_id)

        return {
            "success": True,
            "message": f"✅ Payment '{_title_confirmation(args['title'])}' created. ID: {user_seq}",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error creating expense: {e}")
        return {"success": False, "message": "❌ Could not create the payment. Please try again."}


def execute_create_task(user_id, args, user_tz):
    """Execute create_task function. Uses safe context manager + validation."""
    try:
        args['title'] = _normalize_title(args.get('title'))
        if not args.get('title'):
            return {"success": False, "message": "Invalid arguments for creating a task"}

        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return {"success": False, "message": "User not found"}

            date, reminder_time = parse_date(args['date'], user_tz)
            if date < today_in_tz(user_tz):
                return {"success": False, "message": "A task date cannot be in the past"}

            expense = add_expense(
                db,
                user_id=user.id,
                expense_type='task',
                title=args['title'],
                amount=0,
                currency='RUB',
                next_payment_date=date,
                period=args['period'],
                period_days=args.get('period_days'),
                recur_anchor=normalize_recur_anchor(args.get('recur_anchor')),
                reminder_time=reminder_time,
            )
            expense_id = expense.id
            user_seq = expense.user_seq
            recompute_after_mutation(db, expense_id=expense_id)

        return {
            "success": True,
            "message": f"✅ Task '{_title_confirmation(args['title'])}' created. ID: {user_seq}",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        return {"success": False, "message": "❌ Could not create the task. Please try again."}


def fetch_db_snapshot_readonly(user_id):
    """Read-only DB snapshot for AI (no writes). Used at dialog start (1/6)."""
    with get_db() as db:
        user_tz = get_user_timezone(db, user_id)
    return execute_list_expenses(user_id, user_tz)


LIST_EXPENSES_MAX_ITEMS = 50


def execute_list_expenses(user_id, user_tz=None):
    """Execute list_expenses function. Read-only view of active records.

    Capped at LIST_EXPENSES_MAX_ITEMS (soonest-due first) — an unbounded dump
    goes straight into the OpenRouter system prompt on every dialog start and
    can be resent on every round, so a power user with hundreds of records
    would otherwise balloon token cost/latency for that one conversation with
    no ceiling.
    """
    try:
        with get_db() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return {"success": False, "message": "User not found"}
            if user_tz is None:
                user_tz = user.timezone or "Europe/Moscow"
            expenses = db.query(Expense).filter(
                Expense.user_id == user.id,
                Expense.is_active == True
            ).order_by(Expense.next_payment_date.asc()).limit(LIST_EXPENSES_MAX_ITEMS + 1).all()

            if not expenses:
                return {"success": True, "message": "📭 No active records."}

            truncated = len(expenses) > LIST_EXPENSES_MAX_ITEMS
            if truncated:
                expenses = expenses[:LIST_EXPENSES_MAX_ITEMS]

            lines = ["Active records (full view for the AI):"]
            for exp in expenses:
                is_task = exp.expense_type == 'task'
                typ = "TASK" if is_task else "PAYMENT"
                days_left = (exp.next_payment_date - today_in_tz(user_tz)).days
                date_str = exp.next_payment_date.strftime('%d.%m.%y')
                period_str = exp.period or "one-off"
                if exp.period == 'custom' and exp.period_days:
                    period_str = f"every {exp.period_days}d"
                amt = ""
                if not is_task and exp.amount is not None:
                    cur = currency_symbol(exp.currency)
                    amt = f" | {exp.amount} {cur}"
                rem = f" | time {exp.reminder_time}" if exp.reminder_time else ""
                # Multi-line titles: keep one row per record — name from line 1,
                # note lines flattened into an explicit "note" field so the
                # model sees it exists and preserves it on edit_expense.
                title_first, *note_lines = exp.title.split('\n')
                note_text = " / ".join(l.strip() for l in note_lines if l.strip())
                note = f" | note: {note_text}" if note_text else ""
                lines.append(f"ID {exp.user_seq} | {typ} | {title_first}{amt} | {date_str} | {period_str} | {days_left}d{rem}{note}")

            if truncated:
                lines.append(
                    f"... showing the first {LIST_EXPENSES_MAX_ITEMS} records by due date "
                    "(the nearest ones); the rest are hidden."
                )

            return {"success": True, "message": "\n".join(lines)}
    except Exception as e:
        logger.error(f"Error listing expenses: {e}")
        return {"success": False, "message": "❌ Could not read the list. Please try again."}


def execute_delete_expense(user_id, args):
    """Execute delete_expense function."""
    try:
        user_seq = parse_expense_id(args.get('id'))
        if user_seq is None:
            return {"success": False, "message": "Invalid record ID"}

        with get_db() as db:
            expense = get_expense_by_user_seq(db, user_id, user_seq)

            if not expense:
                return {"success": False, "message": f"❌ No record with ID {user_seq}."}

            title = html.escape(expense.title)
            expense.is_active = False
            expense.deactivated_at = utcnow_naive()
            recompute_after_mutation(db, expense_id=expense.id)

        return {"success": True, "message": f"✅ Record '{title}' (ID: {user_seq}) deleted."}
    except Exception as e:
        logger.error(f"Error deleting expense: {e}")
        return {"success": False, "message": "❌ Could not delete the record. Please try again."}


def execute_mark_done(user_id, args, user_tz):
    """Mark a task as done or payment as paid. Uses safe DB context + validation."""
    try:
        user_seq = parse_expense_id(args.get('id'))
        if user_seq is None:
            return {"success": False, "message": "Invalid record ID"}

        with get_db() as db:
            expense = get_expense_by_user_seq(db, user_id, user_seq)

            if not expense or not expense.is_active:
                return {"success": False, "message": f"❌ No record with ID {user_seq}."}
            expense_id = expense.id

            title = html.escape(expense.title)
            is_task = expense.expense_type == 'task'
            label = "Task" if is_task else "Payment"

            if expense.period == 'none':
                expense.is_active = False
                expense.deactivated_at = utcnow_naive()
                recompute_after_mutation(db, expense_id=expense_id)
                return {
                    "success": True,
                    "message": f"✅ {label} '{title}' completed and removed from the list.",
                    "expense_id": expense_id,
                }

            today = today_in_tz(user_tz)
            next_date = compute_next_due_date(
                expense.period, expense.period_days,
                expense.next_payment_date, today, expense.recur_anchor,
            )

            expense.next_payment_date = next_date
            recompute_after_mutation(db, expense_id=expense_id)

        return {
            "success": True,
            "message": f"✅ {label} '{title}' marked. Next due: {next_date.strftime('%d.%m.%y')}",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error marking done: {e}")
        return {"success": False, "message": "❌ Could not mark it done. Please try again."}


def execute_reschedule(user_id, args, user_tz):
    """Change the due date of an expense or task. With validation."""
    try:
        user_seq = parse_expense_id(args.get('id'))
        if user_seq is None or not args.get('new_date'):
            return {"success": False, "message": "Invalid arguments for rescheduling"}

        with get_db() as db:
            expense = get_expense_by_user_seq(db, user_id, user_seq)

            if not expense or not expense.is_active:
                return {"success": False, "message": f"❌ No record with ID {user_seq}."}
            expense_id = expense.id

            new_date, new_time = parse_date(args['new_date'], user_tz)
            if new_date < today_in_tz(user_tz):
                return {"success": False, "message": "The new date cannot be in the past"}

            title = html.escape(expense.title)
            is_task = expense.expense_type == 'task'
            label = "Task" if is_task else "Payment"

            expense.next_payment_date = new_date
            if new_time:
                expense.reminder_time = new_time
            recompute_after_mutation(db, expense_id=expense_id)

            due_label = new_date.strftime('%d.%m.%y')
            if new_time:
                due_label += f" {new_time}"

        return {
            "success": True,
            "message": f"✅ {label} '{title}' moved to {due_label}.",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error rescheduling: {e}")
        return {"success": False, "message": "❌ Could not move the due date. Please try again."}


def execute_edit_expense(user_id, args):
    """Edit an existing expense - change period, title, amount, etc."""
    try:
        user_seq = parse_expense_id(args.get('id'))
        if user_seq is None:
            return {"success": False, "message": "Invalid record ID"}

        with get_db() as db:
            expense = get_expense_by_user_seq(db, user_id, user_seq)

            if not expense or not expense.is_active:
                return {"success": False, "message": f"❌ No record with ID {user_seq}."}
            expense_id = expense.id

            title = html.escape(expense.title)
            changes = []
            new_title = _normalize_title(args.get('title'))
            if new_title:
                expense.title = new_title
                changes.append('title')
            if 'period' in args:
                expense.period = args['period']
                changes.append('period')
                if args['period'] == 'custom' and 'period_days' in args:
                    expense.period_days = args['period_days']
                    changes.append(f"every {args['period_days']}d")
                elif args['period'] != 'custom':
                    expense.period_days = None
            if 'amount' in args:
                expense.amount = args['amount']
                changes.append('amount')
            if 'currency' in args:
                expense.currency = args['currency']
                changes.append('currency')
            # Effective period may have just changed above in the same call;
            # apply_recur_anchor gates on it and ignores unknown/omitted values.
            if apply_recur_anchor(expense, args.get('recur_anchor')):
                changes.append('recurrence anchor')
            if changes:
                recompute_after_mutation(db, expense_id=expense_id)

        if not changes:
            return {
                "success": True,
                "message": f"✅ Record '{title}' (ID: {user_seq}) - nothing changed.",
                "expense_id": expense_id,
            }

        return {
            "success": True,
            "message": f"✅ '{title}' updated: {', '.join(changes)}.",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error editing expense: {e}")
        return {"success": False, "message": "❌ Could not change the record. Please try again."}


def execute_set_reminder_plan(user_id, args):
    """Set/change an expense's reminder schedule (plan v3, docs/reminder-plan.md).
    Partial update: only the dimensions the AI passed change. Does not touch
    the due date, so the due-date ORM reset listener never fires — the
    episode counter is preserved and the new plan just reschedules from it."""
    try:
        user_seq = parse_expense_id(args.get('id'))
        if user_seq is None:
            return {"success": False, "message": "Invalid record ID"}

        pre_due = args.get('pre_due_days')
        due_day = args.get('due_day')
        overdue = args.get('overdue')
        if pre_due is None and due_day is None and overdue is None:
            return {"success": False, "message": "No reminder change was specified"}

        with get_db() as db:
            expense = get_expense_by_user_seq(db, user_id, user_seq)
            if not expense or not expense.is_active:
                return {"success": False, "message": f"❌ No record with ID {user_seq}."}
            expense_id = expense.id
            title = html.escape(expense.title)

            current = reminder_plan.plan_from_json(expense.reminder_plan)
            try:
                new_plan = reminder_plan.apply_ai_plan_changes(
                    current, pre_due_days=pre_due, due_day=due_day, overdue=overdue,
                )
            except reminder_plan.PlanError as e:
                return {"success": False, "message": f"❌ {e}"}

            expense.reminder_plan = reminder_plan.plan_to_json(new_plan)
            recompute_after_mutation(db, expense_id=expense_id)
            summary = reminder_plan.plan_summary(new_plan)

        return {
            "success": True,
            "message": f"🔔 Reminders for '{title}' (ID: {user_seq}): {summary}",
            "expense_id": expense_id,
        }
    except Exception as e:
        logger.error(f"Error setting reminder plan: {e}")
        return {"success": False, "message": "❌ Could not change the reminders. Please try again."}


def execute_list_backups(user_id):
    """List this user's saved snapshots (read-only)."""
    try:
        with get_db() as db:
            backups = list_user_backups(db, user_id)

        if not backups:
            return {"success": True, "message": "No saved versions yet."}

        lines = []
        for b in backups:
            line = f"#{b['id']} - {b['created_at']} ({b['item_count']} records)"
            if b.get("label"):
                line += f" — {b['label']}"
            lines.append(line)

        return {"success": True, "message": "\n".join(lines), "backups": backups}
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        return {"success": False, "message": "❌ Could not read the version list. Please try again."}


def execute_restore_backup(user_id, args):
    """Restore the user's data to a previously saved snapshot. Takes a fresh
    safety snapshot of the current state first, so a restore is itself
    undoable through this same mechanism."""
    backup_id = parse_expense_id(args.get('backup_id'))
    if backup_id is None:
        return {"success": False, "message": "❌ Invalid version ID."}

    try:
        with get_db() as db:
            # protect_id=backup_id: this snapshot's own pruning must not evict
            # the exact backup the user is about to restore (see
            # AUTO_SNAPSHOT_FUNCTIONS above for the other half of this fix).
            snapshot_user_data(db, user_id, label="before_restore", protect_id=backup_id)
            result = restore_user_backup(db, user_id, backup_id)

        if result is None:
            return {"success": False, "message": f"❌ Version #{backup_id} not found."}

        return {
            "success": True,
            "message": (
                f"✅ Data restored to the version from {result['created_at']} "
                f"({result['item_count']} records)."
            ),
        }
    except Exception as e:
        logger.error(f"Error restoring backup: {e}")
        return {"success": False, "message": "❌ Could not restore the version. Please try again."}


def backup_database() -> str | None:
    """Create a backup of the database. Keep max 50 backups. Returns filename or None."""
    try:
        import sqlite3

        os.makedirs(BACKUP_DIR, exist_ok=True)

        db_path = config.DATABASE_PATH
        if not os.path.exists(db_path):
            logger.warning("No database file to backup")
            return None

        from utils import now_msk
        timestamp = now_msk().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{DB_BACKUP_PREFIX}{timestamp}.db"
        backup_path = os.path.join(BACKUP_DIR, backup_name)

        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Keep only last MAX_BACKUPS backups
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
        while len(backups) > MAX_BACKUPS:
            old_backup = os.path.join(BACKUP_DIR, backups.pop(0))
            os.remove(old_backup)
            logger.info(f"Removed old backup: {old_backup}")

        logger.info(f"Backup created: {backup_path}")
        return backup_name
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return None


def daily_backup():
    """Create a daily backup (once per day). Uses the improved backup_database."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)

        # Check if we already have a backup today
        from utils import now_msk
        today = now_msk().strftime('%Y%m%d')
        existing = [f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{DB_BACKUP_PREFIX}{today}")]

        if not existing:
            backup_database()
            logger.info("Daily backup created")

        return True
    except Exception as e:
        logger.error(f"Daily backup failed: {e}")
        return False
