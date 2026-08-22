"""Optional Telegram alerts for SQLite lock errors and disk pressure.

Everything silently no-ops unless ADMIN_CHAT_ID is configured and
register_alert_bot() has been called at startup.
"""

import asyncio
import logging
import os
import shutil
import time

import config

logger = logging.getLogger(__name__)

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


DISK_FREE_ALERT_THRESHOLD_MB = _env_int("DISK_FREE_ALERT_THRESHOLD_MB", 1024)
DISK_BUDGET_ALERT_PCT = _env_int("DISK_BUDGET_ALERT_PCT", 80, minimum=1)
ALERT_COOLDOWN_SECONDS = _env_int("ALERT_COOLDOWN_SECONDS", 600, minimum=1)

_bot = None
_loop: "asyncio.AbstractEventLoop | None" = None
_last_alert_at: dict = {}


def register_alert_bot(application, loop) -> None:
    """Call once at startup (from an async context, e.g. post_init) with the
    running Application and its event loop — alerts can then be sent safely
    from any thread, including worker threads spawned via asyncio.to_thread,
    via asyncio.run_coroutine_threadsafe."""
    global _bot, _loop
    _bot = application.bot
    _loop = loop
    if ADMIN_CHAT_ID:
        logger.info("Admin alerting enabled (ADMIN_CHAT_ID set)")
    else:
        logger.info("Admin alerting disabled (ADMIN_CHAT_ID not set)")


def send_admin_alert(text: str, *, key: str | None = None) -> None:
    """Best-effort Telegram alert to ADMIN_CHAT_ID. Safe to call from any
    thread; a no-op if not configured. `key` cools down repeated alerts of
    the same kind (e.g. "database is locked" firing on every request during
    an incident would otherwise spam the admin every time it recurs)."""
    if not ADMIN_CHAT_ID or _bot is None or _loop is None:
        return

    dedup_key = key or text
    now = time.monotonic()
    last = _last_alert_at.get(dedup_key)
    if last is not None and now - last < ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[dedup_key] = now

    async def _send():
        try:
            await _bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ {text}")
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")

    try:
        asyncio.run_coroutine_threadsafe(_send(), _loop)
    except Exception as e:
        logger.error(f"Failed to schedule admin alert: {e}")


class DatabaseLockAlertHandler(logging.Handler):
    """Watches every log record for 'database is locked' and fires an admin
    alert. Attach to the root logger once at startup."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        if "database is locked" in message.lower():
            send_admin_alert(
                f'"database is locked" in the logs:\n{message[:300]}',
                key="database_is_locked",
            )


def check_disk_space(path: str | None = None) -> None:
    """Alert if free disk space drops below DISK_FREE_ALERT_THRESHOLD_MB.
    Meant to be registered as a periodic scheduler job."""
    target = path or config.STATE_DIR
    try:
        free_mb = shutil.disk_usage(target).free / (1024 * 1024)
    except OSError as e:
        logger.warning(f"Disk space check failed: {e}")
        return
    if free_mb < DISK_FREE_ALERT_THRESHOLD_MB:
        send_admin_alert(
            f"Low disk space: {free_mb:.0f} MB free "
            f"(threshold {DISK_FREE_ALERT_THRESHOLD_MB} MB).",
            key="low_disk_space",
        )


def check_bot_disk_budget(path: str | None = None) -> None:
    """Alert when runtime data crosses the configured application budget.

    A zero BOT_DISK_BUDGET_MB disables this independent footprint check.
    """
    budget_mb = config.BOT_DISK_BUDGET_MB
    if budget_mb <= 0:
        return

    target = path or config.STATE_DIR
    total_bytes = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(target):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    total_bytes += os.path.getsize(fpath)
                except OSError:
                    continue  # file removed/rotated mid-walk — skip, not fatal
    except OSError as e:
        logger.warning(f"Disk budget check failed: {e}")
        return

    used_mb = total_bytes / (1024 * 1024)
    pct = used_mb / budget_mb * 100
    if pct >= DISK_BUDGET_ALERT_PCT:
        send_admin_alert(
            f"The bot is using {used_mb:.0f} MB, {pct:.0f}% of the {budget_mb} MB budget "
            f"(alert threshold {DISK_BUDGET_ALERT_PCT}%).",
            key="disk_budget_exceeded",
        )
