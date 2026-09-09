# Changelog

All notable public changes are documented here.

## 1.2.1 - 2026-09-09

### Fixed

- `/edit ID` did nothing at all when the field menu of another record was
  still open - the bot simply went quiet. A conversation's waiting states
  match inline buttons or plain text but never a command, so the repeated
  command matched neither them nor `/cancel` and was dropped in silence.
  `/add` and `/task` had the same defect; all conversations are now
  re-entrant, as `/settings` already was.
- A tap on the field menu of an earlier `/edit` would have changed whichever
  record the newest `/edit` opened, while the menu on screen named a
  different ID. The older menu is now retired with a note instead.

## 1.2.0 - 2026-09-09

### Added

- A 🗑 Delete option in the `/edit` menu. It runs the same mechanics as
  `/delete`: one confirmation screen showing the record, then the same
  confirm/cancel buttons and the same soft delete. Deleting no longer needs a
  second command and a second look at the id.

### Changed

- The regression suite is 58 groups: one drives the `/edit` menu tap through
  confirmation, cancellation and the ownership check; the other proves a
  repeated command re-enters its conversation and that a stale menu is
  retired.

## 1.1.0 - 2026-09-08

### Added

- Voice messages. With the assistant on, a Telegram voice note is
  transcribed through OpenRouter Whisper and handled exactly like the same
  words typed: same AI-mode gate, same conversation gate, same rate limit
  (charged before any audio is downloaded), and one dialogue turn per note.
  Audio is streamed to the provider and never written to disk. Configurable
  with `OPENROUTER_STT_MODEL` and `OPENROUTER_TRANSCRIBE_URL`; it reuses
  `OPENROUTER_API_KEY`, so nothing new is required to keep the assistant off.

### Fixed

- A second `/settings` while the settings menu was still open did nothing.
  The menu's waiting state matches inline buttons only, so the repeated
  command matched neither the state handlers nor `/cancel` and was dropped
  in silence until the user cancelled or changed a setting.
- Text sent while a step with buttons was open (the settings menu, the
  reminder-plan editor) vanished without a word, which looked like a broken
  assistant. The bot now says the step is still open and how to leave it.
- The assistant could answer in the wrong language when the input arrived in
  another one - a forwarded message, a quote, or a transcribed voice note.
  The interface language is now restated on every call instead of relying on
  a single line inside the static prompt.

### Changed

- First-contact copy (`/start`, `/help`, `/ai`, the empty `/list`, the
  command menu and the short description) now says "write as in a normal
  chat" instead of naming internal concepts like free text, and mentions
  voice messages where they are relevant.
- The regression suite is 56 groups: voice transcription contract, voice
  routing and rate limiting, the reply-language block, and `/settings`
  reentry.

## 1.0.0 - 2026-08-22

First public release.

- Recurring and one-off payments and tasks.
- Flexible per-record reminder plans: days before the due date, on the day, and
  after it, with quiet hours and one-tap snooze.
- Per-user time zones, reminder hour, currency and list sorting.
- RUB, USD, EUR, GBP and CNY with converted totals.
- Calendar import from `.ics` files.
- Fernet encryption for titles, amounts, AI sessions and rollback snapshots.
- Optional OpenRouter assistant, off by default, with per-user snapshots so any
  AI change can be undone.
- English interface throughout: messages, buttons, command menu, AI prompt and
  error text.
- Python and Docker quick starts, both verified end to end from a clean clone.
- Published reminder-plan design contract in `docs/reminder-plan.md`, cited by
  section number from the code.
- CI on Python 3.10 and 3.12: linter, publication-content audit, and the full
  52-group regression suite.
- Issue forms, pull-request checklist and grouped Dependabot updates.
