# Changelog

All notable public changes are documented here.

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
