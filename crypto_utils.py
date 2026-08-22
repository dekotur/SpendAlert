"""Application-level field encryption for data-at-rest (title/amount/currency
of expenses, AI session history, user backup snapshots).

Protects against: a leaked/stolen DB file or backup, or anyone with raw
filesystem access but not access to the running bot process, seeing
plaintext payment/task content. Does NOT protect against the bot process
itself, which holds the key (in .env, like TELEGRAM_BOT_TOKEN/
OPENROUTER_API_KEY) and must decrypt to build reminders/AI context — that is
an inherent limit of any architecture where the server computes reminders
and calls a third-party LLM, not a gap in this module.

Dates are deliberately left unencrypted (see database.py) — the reminder
scheduler needs an indexed SQL range query across all users, which encrypted
values can't support without decrypting every row on every scheduler tick.
"""
import logging

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.DB_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "DB_ENCRYPTION_KEY is not set in .env; it is required to "
                "read and write expense data. Generate one with: "
                "python scripts/generate_key.py"
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_str(value: str | None) -> str | None:
    """Encrypt a string for storage. None passes through unchanged."""
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(value: str | None) -> str | None:
    """Decrypt a value produced by encrypt_str. None passes through unchanged."""
    if value is None:
        return None
    return _get_fernet().decrypt(value.encode("ascii")).decode("utf-8")


def try_decrypt_str(value: str | None) -> str | None:
    """Like decrypt_str, but returns None instead of raising when `value`
    isn't a valid ciphertext (used only by the one-time legacy-data
    migration to tell already-encrypted rows apart from old plaintext)."""
    if value is None:
        return None
    try:
        return decrypt_str(value)
    except (InvalidToken, ValueError):
        return None
