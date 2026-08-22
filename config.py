import logging
import os
import sys

# =============================================================================
# Project Configuration
# =============================================================================

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a bounded integer from the environment with a safe default."""
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


_state_dir = os.getenv("SPENDALERT_DATA_DIR", PROJECT_DIR)
if not os.path.isabs(_state_dir):
    _state_dir = os.path.join(PROJECT_DIR, _state_dir)
STATE_DIR = os.path.abspath(_state_dir)

# Runtime paths are independent of the shell's current directory.
DATABASE_FILE = "spendalert.db"

# Installs created before the project settled on the SpendAlert name wrote
# their database under the old working name. Keep reading that file when it is
# the only one present: an upgrade must never silently start on an empty
# database. Fresh installs, and anyone setting DATABASE_URL, are unaffected.
LEGACY_DATABASE_FILE = "finance_guardian.db"
if not os.path.exists(os.path.join(STATE_DIR, DATABASE_FILE)) and os.path.exists(
    os.path.join(STATE_DIR, LEGACY_DATABASE_FILE)
):
    DATABASE_FILE = LEGACY_DATABASE_FILE

DATABASE_PATH = os.path.join(STATE_DIR, DATABASE_FILE)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///" + DATABASE_PATH.replace("\\", "/"),
)

LOG_FILE = os.path.join(STATE_DIR, "spendalert.log")

BACKUP_DIR = os.path.join(STATE_DIR, "backups")
MAX_BACKUPS = _int_env("MAX_BACKUPS", 50, minimum=1)
MAX_USER_BACKUPS_PER_USER = _int_env("MAX_USER_BACKUPS_PER_USER", 20, minimum=1)

# User rollback snapshots and inactive records are retained for this window.
UNDO_WINDOW_DAYS = _int_env("UNDO_WINDOW_DAYS", 7, minimum=1)

# Default local hour reminders fire when the plan slot has no explicit time
# (docs/reminder-plan.md §6.3). It used to be implicitly 00:00 — the start of
# the local day - so a "3 days before" wave pinged at midnight; a daytime default
# is far friendlier. Per-user override lives in users.reminder_hour; the
# choices offered in /settings are REMINDER_HOUR_CHOICES.
DEFAULT_FIRE_HOUR = _int_env("DEFAULT_FIRE_HOUR", 10)
if DEFAULT_FIRE_HOUR > 23:
    raise RuntimeError("DEFAULT_FIRE_HOUR must be between 0 and 23")
REMINDER_HOUR_CHOICES = (9, 10, 12, 15, 18, 21)

# Quiet hours: reminders whose slot has no explicit time are never fired
# between QUIET_START_HOUR and QUIET_END_HOUR local — a fire that would land
# in that window is pushed to QUIET_END_HOUR. Suppresses the round-the-clock
# "often" (every-2h) due-day/overdue pings at night. Per-user on/off in
# /settings (users.quiet_hours), default on. Fixed window keeps it simple;
# an explicit reminder time or a user-tapped snooze is always honored as-is.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 8

# Blocking DB/file work runs in this dedicated executor. The workload is I/O-
# bound, so the default is intentionally larger than a typical CPU count.
DB_THREAD_POOL_WORKERS = _int_env("DB_THREAD_POOL_WORKERS", 32, minimum=1)

# Optional footprint budget. Zero disables the budget alert.
BOT_DISK_BUDGET_MB = _int_env("BOT_DISK_BUDGET_MB", 0)

# Field-level encryption key for expense title/amount/currency, AI session
# history, and user backup snapshots (crypto_utils.py) — a leaked DB file or
# backup is unreadable without it. Generate with:
#   python scripts/generate_key.py
DB_ENCRYPTION_KEY = os.getenv("DB_ENCRYPTION_KEY")

CACHE_FILE = os.path.join(STATE_DIR, "exchange_rate_cache.json")

DATA_DIR = os.path.join(STATE_DIR, "data")
LOGS_DIR = os.path.join(STATE_DIR, "logs")

# =============================================================================
# Logging setup (safe to call multiple times)
# =============================================================================

def setup_logging():
    """Configure rotating file logs plus concise console output."""
    if not logging.getLogger().handlers:  # avoid duplicate handlers
        from logging.handlers import RotatingFileHandler

        os.makedirs(STATE_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)
        # Windows consoles still default to a legacy code page (cp437, cp1251,
        # ...) that cannot encode every character a log record may carry —
        # typographic dashes from our own messages, Cyrillic from user data in
        # an exception string. Without this, logging raises UnicodeEncodeError
        # and prints its own traceback instead of the record. The file handler
        # is explicitly UTF-8, so only the console stream needs the fallback.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="backslashreplace")

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logging.basicConfig(
            level=logging.INFO,
            handlers=[file_handler, console_handler],
        )

        # Third-party libraries log an INFO line per HTTP call (every getUpdates
        # poll, every OpenRouter/currency/timezone request) — this alone produced
        # 15MB/90k lines of log for 2 users. Keep only warnings and above.
        for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

setup_logging()

# =============================================================================
# Directory initialization
# =============================================================================

def ensure_directories():
    """Create required directories if they don't exist."""
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

ensure_directories()

# =============================================================================
# Database initialization helpers (call explicitly at startup)
# =============================================================================

# Import here to avoid circular imports during module load
def init_database():
    """Initialize DB tables and run migrations. Call this at application startup."""
    from database import init_db, migrate_db
    init_db()
    migrate_db()
    logging.getLogger(__name__).info("Database initialized and migrated.")

# Note: Do NOT call init_database() at import time.
# Call it explicitly from bot.py or entry points.
