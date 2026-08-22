# Reminder plan v3 — the `n0 n1 n2 n3 d f` schema

This is the design contract behind `reminder_plan.py` (pure model),
`reminder_engine.py` (scheduling) and the plan editor in `bot.py`. Source
comments cite it by section, so the numbering here is stable — renumber a
section only together with the comments that reference it.

Button captions and summary strings are quoted verbatim, so this document and
the screen stay comparable line by line.

## 1. One-line idea

A reminder plan describes *when* a record nags, independently of *when it is
due* and *how often it repeats*. Those are three separate axes, and users
conflate them constantly — so the model keeps them apart:

| Axis | Stored in | Changed by |
|---|---|---|
| Due date / time | `expenses.next_payment_date`, `reminder_time` | date input, `/edit`, snooze, mark-paid rollover |
| Repeat period | `expenses.period`, `period_days` | period step, `/edit` |
| Reminder plan | `expenses.reminder_plan` | plan editor, `/ai set_reminder_plan` |

## 2. Slot model

### 2.1 Shape

A plan is a fixed set of six slots: up to four pre-due waves `n0..n3`, a
due-day slot `d`, and an overdue slot `f`.

```json
{
  "v": 1,
  "n": [
    {"on": true, "offset_days": 3, "times": 1, "every_hours": 24.0, "at_time": null},
    {"on": true, "offset_days": 2, "times": 1, "every_hours": 24.0, "at_time": null},
    {"on": true, "offset_days": 1, "times": 1, "every_hours": 24.0, "at_time": null},
    {"on": false}
  ],
  "d": {"on": true, "times": null, "every_hours": 2.0, "at_time": null},
  "f": {"on": true, "times": null, "every_hours": 2.0}
}
```

| Field | Slots | Meaning |
|---|---|---|
| `on` | all | Disabled slots collapse to `{"on": false}` — no other key survives normalization |
| `offset_days` | `n` | Days before the due date, `1..365` |
| `times` | all | Sends inside one episode; `null` = unbounded, allowed for `d`/`f` only |
| `every_hours` | all | Interval between repeats inside one episode, `0.5..168.0` |
| `at_time` | `n`, `d` | Explicit local `"HH:MM"`; `null` means "use the user's reminder hour" |

### 2.2 Semantics per slot type

An **episode** is one slot's calendar span:

- `n` wave — exactly **one local day**, `due − offset_days`. Repeats live
  inside that day; whatever does not fit is dropped rather than spilling into
  the next day (**U9**).
- `d` — the due day itself.
- `f` — starts the day *after* the due date and is open-ended until the record
  is marked done, rescheduled, or deactivated.

`f` has no `at_time`: an overdue record is nagged on an interval, not at an
appointment.

### 2.3 Canonical form

`normalize_plan()` is the only gate into the model. It sorts active `n` waves
by `offset_days` **descending**, pads the list back to four with
`{"on": false}`, rejects two waves on the same day, and bounds every value
(§8). Two plans are equal iff their canonical forms are equal — that is how
"is this still the default?" is answered.

Because the sort is by offset, **the index `n0..n3` is positional, not
stable**: with a single "1 day before" wave, that wave *is* `n0`. Anything
that remembers a slot key must therefore be invalidated when the plan changes
(§6.4).

## 3. Create-screen UX

One screen, shown after the period step of `/add` and `/task`. Internal slot
keys and `every_hours` never appear on a button — only human labels.

### 3.1 Flow

```text
/add   title -> amount -> date -> period -> plan editor -> ✅ Done
/task  title -> date -> period -> plan editor -> ✅ Done
```

Nothing is written to the database until `✅ Done`. The same screen is
reachable for an existing record from `/edit`.

### 3.2 Layout

```text
🔔 When should I remind you?

📌 Internet
25.06.26 · 799 ₽ · every month

Now: 3d before · 2d before · 1d before · due day (often) · overdue (often)

Tap a button to change its mode.
✅ Done saves.

[⭐ Usual]                [📅 Due day only]
[🔕 All off]
[✓ 3 days before · 1x]   [✓ 2 days before · 1x]
[✓ 1 day before · 1x]    [+ Another day…]
[✓ Due day · often]      [✓ Overdue · often]
[🔁 Count from: the schedule]        ← recurring records only
[✅ Done]
```

### 3.3 What a tap does

A tap on a slot button advances that slot through a short cycle **in place** —
no submenu, no extra screen:

| Button | Cycle |
|---|---|
| `N days before` | `1x` → `2x` → `off` → `1x` |
| `Due day` | `1x` → `often` → `off` → `1x` |
| `Overdue` | `daily` → `often` → `off` → `daily` |

A preset button rewrites all slot buttons at once. `✅ Done` is the single
save path.

### 3.4 UI state → plan

The editor's state is a small dict (`n_3`, `n_2`, `n_1`, `extra`, `d`, `f`);
`plan_from_ui()` expands it with the intervals the screen never asks about:

| Slot | UI mode | `times` | `every_hours` |
|---|---|---:|---:|
| `n` | `1x` | 1 | 24 |
| `n` | `2x` | 2 | 12 |
| `d` | `1x` | 1 | 24 |
| `d` | `often` | ∞ | 2 |
| `f` | `daily` | ∞ | 24 |
| `f` | `often` | ∞ | 2 |

`d.at_time` is deliberately left `null` so the engine can fall back to the
record's own `reminder_time` — the clock time the user typed with the date
(**U11**). This is also what keeps a default plan byte-identical to the stored
default (§4).

Presets: `⭐ Usual` = the default plan; `📅 Due day only` = all waves off,
`d` once, `f` daily (**O1**); `🔕 All off` = every slot off.

`ui_from_plan()` is the best-effort inverse, used when re-opening the editor
for an existing record. It returns `None` — routing the user to `/ai` instead
— when a plan cannot be drawn on this screen: an offset other than 3/2/1 plus
one extra, more than one extra wave, `times > 2`, or a non-standard interval.
A final round-trip comparison guarantees the editor never silently drops
detail it could not render.

### 3.5 `+ Another day…`

One extra wave at an arbitrary offset, entered as a number. Accepted range is
`4..365`: the fixed buttons already own 3/2/1, and a duplicate offset is
rejected by normalization anyway.

Cycling the extra wave to `off` turns the button back into `+ Another day…`
(§3.5.5), so its offset can be replaced without resetting the whole plan; the
next input overwrites the old wave.

### 3.6 Summary line

`plan_summary()` renders only the enabled parts, and is the single source for
the editor line, the create-success message and `/ai` replies:

```text
3d before · 2d before · 1d before · due day (often) · overdue (often)
7d before·2x · due day · overdue (daily)
no reminders
```

The raw `n0=…` canon is never shown to a user.

## 4. Default = the v2 preset

The default plan is the pre-plan behavior expressed as a plan: one ping on
each of 3, 2 and 1 days before due, then every 2 hours on the due day and
every 2 hours while overdue.

`plan_to_json()` stores a plan equal to that default as **SQL NULL**. So:

- rows created before plans existed need no data migration — `NULL` already
  means "the default";
- `plan_from_json(None)` returns the default;
- a malformed value also falls back to the default and is logged. The fire
  path never raises because of a bad plan.

Every wave in the stored default carries `every_hours: 24` even though the
canonical description says 24/12/6, because with `times: 1` the interval never
applies — and a uniform 24 keeps `default_plan()` byte-identical to
`plan_from_ui(ui_default())`, which is what allows the `NULL` contract above.

## 5. Conversation state

The editor is a single `ConversationHandler` state (`PLAN_EDITOR`) driven
entirely by `plan_*` callbacks, plus one text state for the `+ Another day`
number. There is no multi-step wizard: every slot is reachable from the one
screen.

## 6. Engine

State on the expense row:

| Column | Meaning |
|---|---|
| `next_reminder_at` | Naive UTC; the next scheduled fire. Always points **forward**, possibly months ahead |
| `reminder_slot` | `n0..n3`/`d`/`f` — the slot of the **last sent** episode |
| `reminder_slot_sends` | Sends already made inside that episode |
| `reminder_stage` | Legacy `3_days…overdue` mirror, kept for rollback safety and used to order the send batch by urgency |

### 6.1 No fixed stage names

The v2 engine hard-coded stage names. Slots are configurable, so the engine
works from `days_until = due − today` and asks the plan which slot owns that
day. Stage names survive only as the mirror column.

### 6.2 Choosing the active slot

```text
days_until  > 0  →  the n wave whose offset_days == days_until, else quiet
days_until == 0  →  d, if enabled
days_until  < 0  →  f, if enabled
```

A day no slot claims is simply quiet.

### 6.3 Fire time inside an episode

The first fire of an episode is its `at_time`, or the user's reminder hour
(`users.reminder_hour`, default 10:00 local — offered as 9/10/12/15/18/21).
Subsequent fires are `last_sent + every_hours`.

When `times` is exhausted — or the calendar day ends for `n`/`d` — the engine
immediately precomputes the **first fire of the next episode** instead of
going `NULL` and waiting for a sweep. A minute-level cron fires whatever is
due; the hourly sweep (36 h fire horizon, 5 day due window) is only a safety
net, so a wave 90 days out needs no special handling.

Two guards shape the first fire:

- **Quiet hours.** Fires without an explicit time never land between 23:00 and
  08:00 local; such a fire is pushed to 08:00. An explicit `at_time`, or a
  time the user chose by tapping a snooze button, is honored as-is.
- **Same-day creation.** A `d` slot with no explicit time will not fire within
  2 hours of the record being created, so adding something due today does not
  ping instantly.

### 6.4 Due date, time or plan change

`reminder_slot` + `reminder_slot_sends` are the engine's **only** memory that
a finite episode is exhausted, and that memory is implicitly keyed to the due
date. Only `record_reminder_sent()` may advance it — a recompute never does,
because a recompute that moved the pair forward would let the hourly sweep
forget the exhaustion and resurrect an episode as duplicate pings.

Three ORM-level listeners in `database.py` reset the pair, so every mutation
site is covered at once (mark paid, `/edit`, `/ai` reschedule, timezone
recalculation, snapshot restore):

| Change | Why the reset is required |
|---|---|
| `next_payment_date` | A monthly record whose "3 days before" wave already fired this period would otherwise skip the same wave next period |
| `reminder_time` | The due date alone is not a complete episode key: moving `13:30 → 13:45` on the same day must fire at 13:45 rather than continuing the old cadence |
| `reminder_plan` | Slot keys are positional (§2.3) — editing the plan re-indexes the waves, so a stored key would map to a *different* wave: a duplicate ping or a lost one |

The plan listener must **not** skip a `NULL` old value: `NULL` means "the
default preset", a real prior plan, and `NULL → custom` is exactly the case
that re-indexes waves.

### 6.5 Overdue backoff (mandatory)

An abandoned record with an unbounded, frequent `f` slot would resend every
two hours forever. After **7 days** unacknowledged, the effective `f` interval
is floored at **24 hours**, regardless of the configured value. This applies
to custom plans too — it is a safety limit, not a preference.

### 6.6 Send ordering

The fire batch is ordered by urgency over the legacy stage mirror
(`overdue → due day → 1 → 2 → 3 days`), which is an exact `days_until` proxy.
Slot keys cannot be used for this: `n0` may be a 1-day wave in one plan and a
90-day wave in another.

## 7. Storage

`expenses.reminder_plan` holds compact JSON, or `NULL` for the default (§4).
The column is **not encrypted**: it holds no user text or amounts, and the
scheduler needs it on every sweep. `next_reminder_at` and the slot columns are
likewise queryable by design — see `SECURITY.md` for what the encryption does
and does not cover.

## 8. Input bounds

| Value | Range | Rationale |
|---|---|---|
| Waves per plan | ≤ 4 | Fits one screen; bounds the per-record fan-out |
| `offset_days` | 1–365 | A wave further out than a year is a different feature |
| `times` | 1–20, or ∞ for `d`/`f` | A finite pre-due wave cannot be unbounded — its day would never end |
| `every_hours` | 0.5–168 | Below 30 min is spam; above a week is not a reminder |
| Extra-wave offset | 4–365 | 3/2/1 already have fixed buttons (§3.5) |

Violations raise `PlanError`, whose message is user-safe and shown as-is.

## 9. Natural-language edits

The `/ai` tool `set_reminder_plan` speaks the same abstraction as the create
screen, over arbitrary offsets: a list of days-before-due (each firing once),
plus coarse due-day (`once`/`repeat`/`off`) and overdue (`daily`/`repeat`/
`off`) modes. It is a **partial** update — dimensions the model did not
mention keep their current value — and it never touches the due date or the
repeat period.

## 10. Decision tags used in code comments

| Tag | Decision |
|---|---|
| **U9** | A pre-due wave repeats only inside its own calendar day; overflow is dropped, not carried over |
| **U11** | `d.at_time` stays `null` so the engine falls back to the record's `reminder_time` |
| **O1** | `📅 Due day only` = all waves off, `d` once, `f` once per day |

## 11. Test coverage

`test_bot.py` covers this document directly: the §3.3 tap cycles, the §3.4
truth table, quiet-day handling, exhausted waves handing over to the next
episode, repeated recomputes *not* resurrecting an exhausted wave, due-date
change resetting episode memory, the §6.5 backoff, and the quiet-hours shift.
A change to reminder behavior without a matching test there is incomplete.
