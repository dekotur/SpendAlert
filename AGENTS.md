# AGENTS.md — public contributor and setup guide

This file is the authoritative operational guide for coding agents working in
the public SpendAlert repository. It intentionally contains no private server
details, credentials, user identifiers, local filesystem paths, or production
data.

## Mission and boundaries

SpendAlert is a self-hosted Telegram bot for recurring bills, tasks, and
flexible reminders. The interface is English throughout. Preserve the complete user-facing behavior while
keeping the repository safe to publish.

Never add:

- real `.env` values, provider tokens, chat IDs, passwords, private keys, or
  database encryption keys;
- databases, WAL files, logs, backups, cache files, exported chats, or real
  calendar invitations;
- machine-specific absolute paths, usernames, hostnames, IP addresses, remote
  server scripts, or private deployment notes;
- screenshots or test fixtures containing real user data.

Use synthetic IDs, dates, titles, amounts, and `.ics` events in every test and
example. If a credential is exposed, stop, rotate it at the provider, and
follow `SECURITY.md`; deleting the latest line is not enough.

## Repository map

| Path | Responsibility |
|---|---|
| `bot.py` | Telegram application, commands, conversations, callbacks, AI routing |
| `database.py` | SQLAlchemy models, migrations, user isolation, snapshots |
| `crypto_utils.py` | Fernet encryption for sensitive SQLite fields |
| `scheduler.py` | APScheduler jobs and Telegram reminder delivery |
| `reminder_plan.py` | Pure reminder-plan model and validation |
| `reminder_engine.py` | Reminder episode calculation and persisted schedule state |
| `ai_handler.py` | Optional OpenRouter tool loop, voice transcription, and user-scoped AI context |
| `ics_import.py` | Bounded `.ics` parsing and event conversion |
| `currency.py` | Exchange-rate cache and currency conversion |
| `utils.py` | Dates, time zones, message context, recurrence helpers |
| `tz_rollback.py` | Time-zone change snapshots and rollback |
| `monitoring.py` | Optional Telegram alerts for lock and disk conditions |
| `config.py` | Environment contract, portable state paths, logging |
| `test_bot.py` | Self-contained 58-group regression suite |
| `scripts/security_audit.py` | Publication-content secret and local-data guard |
| `scripts/generate_key.py` | Prints a fresh Fernet key for `DB_ENCRYPTION_KEY` |
| `docs/reminder-plan.md` | Reminder-plan design contract; code comments cite its section numbers |
| `pyproject.toml` | Ruff configuration, with each disabled rule justified in place |

## Secret-free bootstrap for an agent

Tests do not need a Telegram token, OpenRouter key, `.env`, or existing
database. `test_bot.py` creates a temporary SQLite file and a throwaway Fernet
key before importing the database layer.

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
python -m pip install --requirement requirements-dev.txt
python scripts/security_audit.py
ruff check .
python test_bot.py
python -m compileall -q .
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
python -m pip install --requirement requirements-dev.txt
python scripts\security_audit.py
ruff check .
python test_bot.py
python -m compileall -q .
```

Expected test marker:

```text
[SUCCESS] All 58 test groups passed
```

Do not create `.env` merely to run tests. Do not reuse a developer's existing
virtual environment or database when an isolated environment is practical.

## Running the real bot

A real Telegram polling smoke test requires user-controlled credentials. An
agent must never invent, scrape, print, or ask the user to paste them into chat.
The user should place them in a local untracked `.env` or a deployment secret
store.

Required variables:

- `TELEGRAM_BOT_TOKEN` — created in the user's own BotFather session;
- `DB_ENCRYPTION_KEY` — generated locally with
  `python scripts/generate_key.py` and stored in a password manager.

Optional variables:

- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_URL` — AI mode;
- `OPENROUTER_STT_MODEL`, `OPENROUTER_TRANSCRIBE_URL` — voice transcription,
  which reuses `OPENROUTER_API_KEY`;
- `ADMIN_CHAT_ID` — operational alerts;
- `SPENDALERT_DATA_DIR` — external runtime-state directory;
- `DATABASE_URL` — SQLite URL override;
- resource and retention settings documented in `.env.example` and `README.md`.

The real launch command is:

```bash
python bot.py
```

Without the required variables, `bot.py` must exit clearly before opening a
Telegram connection. The core payment, task, reminder, currency, and `.ics`
features must continue to work when OpenRouter is not configured.

## Docker path

The Docker image copies only runtime Python files and dependencies. Compose
mounts a named volume at `/data` and reads secrets from the local `.env`.

```bash
docker compose build
docker compose up -d
docker compose logs -f spendalert
```

Do not add credentials to `Dockerfile`, `compose.yaml`, image layers, build
arguments, or committed override files. Do not run `docker compose down -v`
unless the user explicitly wants to delete the persisted database volume.

## Code invariants

1. Scope every user-owned record lookup by `user_id`; do not reveal whether a
   different user's record or backup exists.
2. Keep synchronous database and filesystem work out of async Telegram
   handlers. Put it in a small synchronous helper and call it with
   `asyncio.to_thread`.
3. Preserve `_PerChatUpdateProcessor`: different chats may run concurrently,
   but updates within one chat must stay ordered for `ConversationHandler` and
   `context.user_data` safety.
4. Load `DB_ENCRYPTION_KEY` before importing models that read encrypted fields.
   Never replace a stable key for an existing database.
5. Keep `next_payment_date` and reminder scheduling metadata queryable. Do not
   claim the database is fully encrypted or zero-knowledge.
6. Only `record_reminder_sent` advances `reminder_slot` and
   `reminder_slot_sends`; schedule recomputation must not resurrect exhausted
   reminder episodes. `docs/reminder-plan.md` is the contract — its section
   numbers are cited from code comments, so keep both in step.
7. Escape every user-controlled string before including it in Telegram HTML.
   Preserve safe fallback behavior for malformed external input.
8. Keep `.ics` size/event limits and parse untrusted calendar data away from
   the event loop.
9. The optional AI path may receive only the current user's scoped context and
   must retain the `/privacy` disclosure.
10. Tests must point `DATABASE_URL` at a disposable file before importing
    `database.py`; never test against a local or live database.
11. A voice message is only an input format, never a second set of rules:
    `voice_ai_handler` applies the same AI-mode and active-conversation
    gates as typed text, charges the per-user rate limit *before* any audio
    is fetched or transcribed, and passes `already_rate_limited=True` so the
    same call is not charged twice. Keep the size cap checked against the
    reported `file_size` before the download; stream audio through and never
    persist or log it.
12. Every AI call appends `REPLY_LANGUAGE_CONTEXT` to the system content.
    The static prompt's language line is only the fallback: a forwarded
    message, a quote, or a transcribed voice note can arrive in another
    language, and without the live block the model answers in that language
    instead of the bot's interface language.
13. Keep `allow_reentry=True` on every `ConversationHandler`. Their states
    match inline callbacks or plain text, never a command, so without the
    flag a repeated `/add`, `/task`, `/edit` or `/settings` matches neither
    the state handlers nor `/cancel`, and PTB drops it in silence — the bot
    looks dead. Re-entrancy in turn means an earlier screen can still be on
    the user's display: `/edit` records the message id of its current menu
    and retires an older one instead of applying its taps to the newest ID.
    Keep every conversation state pattern-scoped for the same reason.
14. The 🗑 Delete option in the `/edit` menu must reuse the `confirm_delete` /
    `cancel_delete` callbacks and end the conversation as it shows the
    confirmation. Ending it keeps `EDIT_FIELD` from answering later stray
    taps with a field prompt, and the confirmation then reaches
    `button_callback` the same way `/delete`'s does.

## Change workflow

1. Read the relevant module and nearby tests before editing.
2. Make the smallest complete change that preserves the invariants above.
3. Add or update a regression test for behavior changes.
4. Run the complete verification gate, not only the new test.
5. Inspect the exact Git publication set before committing.

Verification gate:

```bash
python scripts/security_audit.py
ruff check .
python test_bot.py
python -m compileall -q .
git diff --check
git status --short
```

Review `git status --short` manually. A clean secret scan does not authorize
committing unexpected files. Keep runtime artifacts ignored and out of Git.

## Definition of done

A change is ready only when:

- its requested behavior is implemented and covered by evidence;
- all 58 test groups pass with an honest process exit code;
- `ruff check .`, source compilation and `scripts/security_audit.py` pass;
- README, `.env.example`, privacy text, and agent instructions remain accurate;
- no real secret, local-machine fingerprint, private infrastructure reference,
  or runtime data is present in tracked files or new Git history;
- the working tree contains only intentional source/documentation changes.
