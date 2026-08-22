"""
Reminder plan v3 — pure plan model (schema `n0 n1 n2 n3 d f`).

See docs/reminder-plan.md. A plan is up to 4 pre-due "waves" (n0..n3, sorted by
offset_days descending after normalization), a due-day slot (d) and an
overdue slot (f). Each slot: on/off, times (int or None=infinity),
every_hours, optional at_time "HH:MM" (n/d only).

This module is deliberately import-free of database/telegram — everything
here is pure data + math so it can be unit-tested without a DB and shared
verbatim between the engine (reminder_engine.py) and the create-screen UI
(bot.py). Timezone-aware datetime work stays in reminder_engine.py.

Storage contract: expenses.reminder_plan holds JSON (plan_to_json), where
NULL means "the v2 default preset" (default_plan) — existing rows need no
data migration and a plan equal to the default is stored back as NULL.
"""

from __future__ import annotations

import copy
import json
import logging

logger = logging.getLogger(__name__)

PLAN_VERSION = 1

N_KEYS = ("n0", "n1", "n2", "n3")
SLOT_KEYS = N_KEYS + ("d", "f")

MAX_N_SLOTS = 4
OFFSET_MIN, OFFSET_MAX = 1, 365
TIMES_MIN, TIMES_MAX = 1, 20
EVERY_MIN_HOURS, EVERY_MAX_HOURS = 0.5, 168.0

# Offsets already present as fixed buttons on the create screen; the
# "+ Another day" input must pick something else (§3.5).
UI_FIXED_OFFSETS = (3, 2, 1)
UI_EXTRA_OFFSET_MIN, UI_EXTRA_OFFSET_MAX = 4, 365


class PlanError(ValueError):
    """Invalid plan structure or values (the message is safe to show)."""


# --- Presets -----------------------------------------------------------------

# v2 behavior expressed as a plan (§4): one ping per pre-due day at 3/2/1
# days out, then every 2h on the due day and every 2h overdue (engine adds
# the hidden 7-day backoff to 24h for f — see reminder_engine).
# every_hours is 24 on all waves (the doc's literal canon says 24/12/6, but
# with times=1 the interval never applies — a uniform 24 keeps this preset
# byte-identical to plan_from_ui(ui_default()), so "Done" with defaults
# stores NULL as the column contract requires).
_DEFAULT_PLAN = {
    "v": PLAN_VERSION,
    "n": [
        {"on": True, "offset_days": 3, "times": 1, "every_hours": 24.0, "at_time": None},
        {"on": True, "offset_days": 2, "times": 1, "every_hours": 24.0, "at_time": None},
        {"on": True, "offset_days": 1, "times": 1, "every_hours": 24.0, "at_time": None},
        {"on": False},
    ],
    "d": {"on": True, "times": None, "every_hours": 2.0, "at_time": None},
    "f": {"on": True, "times": None, "every_hours": 2.0},
}

# "Due day only": quiet before the due date, one ping on the day, then a
# daily nudge if it was missed (O1: d=1x, f=daily).
_DAY_ONLY_PLAN = {
    "v": PLAN_VERSION,
    "n": [{"on": False}, {"on": False}, {"on": False}, {"on": False}],
    "d": {"on": True, "times": 1, "every_hours": 24.0, "at_time": None},
    "f": {"on": True, "times": None, "every_hours": 24.0},
}

_ALL_OFF_PLAN = {
    "v": PLAN_VERSION,
    "n": [{"on": False}, {"on": False}, {"on": False}, {"on": False}],
    "d": {"on": False},
    "f": {"on": False},
}


def default_plan() -> dict:
    return copy.deepcopy(_DEFAULT_PLAN)


def day_only_plan() -> dict:
    return copy.deepcopy(_DAY_ONLY_PLAN)


def all_off_plan() -> dict:
    return copy.deepcopy(_ALL_OFF_PLAN)


# --- Normalization / validation ---------------------------------------------

def _normalize_slot(slot: dict, kind: str) -> dict:
    """Validate one slot dict; returns a clean copy. kind: 'n' | 'd' | 'f'."""
    if not isinstance(slot, dict):
        raise PlanError("A plan slot must be an object")
    if not slot.get("on"):
        return {"on": False}

    out: dict = {"on": True}

    if kind == "n":
        offset = slot.get("offset_days")
        if not isinstance(offset, int) or isinstance(offset, bool) \
                or not (OFFSET_MIN <= offset <= OFFSET_MAX):
            raise PlanError(f"Days before due: a number from {OFFSET_MIN} to {OFFSET_MAX}")
        out["offset_days"] = offset

    times = slot.get("times")
    if times is None:
        if kind == "n":
            raise PlanError("A pre-due wave needs a finite number of repeats")
        out["times"] = None  # infinity (d/f only)
    else:
        if not isinstance(times, int) or isinstance(times, bool) \
                or not (TIMES_MIN <= times <= TIMES_MAX):
            raise PlanError(f"Repeats: a number from {TIMES_MIN} to {TIMES_MAX}, or unlimited")
        out["times"] = times

    every = slot.get("every_hours")
    if isinstance(every, bool) or not isinstance(every, (int, float)) \
            or not (EVERY_MIN_HOURS <= float(every) <= EVERY_MAX_HOURS):
        raise PlanError(f"Interval: {EVERY_MIN_HOURS} to {EVERY_MAX_HOURS} hours")
    out["every_hours"] = float(every)

    if kind in ("n", "d"):
        at_time = slot.get("at_time")
        if at_time is not None:
            parsed = parse_at_time(at_time)
            if parsed is None:
                raise PlanError("Time: use the HH:MM format")
            at_time = f"{parsed[0]:02d}:{parsed[1]:02d}"
        out["at_time"] = at_time

    return out


def normalize_plan(plan: dict) -> dict:
    """Validate and canonicalize a plan: n-slots sorted by offset descending,
    duplicates rejected, values bounded (§8). Raises PlanError."""
    if not isinstance(plan, dict):
        raise PlanError("A plan must be an object")

    raw_n = plan.get("n") or []
    if not isinstance(raw_n, list) or len(raw_n) > MAX_N_SLOTS:
        raise PlanError(f"Pre-due waves: at most {MAX_N_SLOTS}")

    n_slots = [_normalize_slot(s, "n") for s in raw_n]
    active_n = [s for s in n_slots if s["on"]]

    offsets = [s["offset_days"] for s in active_n]
    if len(offsets) != len(set(offsets)):
        raise PlanError("Two reminders on the same day before the due date")

    active_n.sort(key=lambda s: s["offset_days"], reverse=True)
    while len(active_n) < MAX_N_SLOTS:
        active_n.append({"on": False})

    return {
        "v": PLAN_VERSION,
        "n": active_n,
        "d": _normalize_slot(plan.get("d") or {"on": False}, "d"),
        "f": _normalize_slot(plan.get("f") or {"on": False}, "f"),
    }


def is_default_plan(plan: dict) -> bool:
    try:
        return normalize_plan(plan) == _DEFAULT_PLAN
    except PlanError:
        return False


def plan_to_json(plan: dict) -> str | None:
    """Serialize for the expenses.reminder_plan column. The default preset is
    stored as NULL (column contract: NULL ≡ v2 preset), everything else as
    compact JSON."""
    normalized = normalize_plan(plan)
    if normalized == _DEFAULT_PLAN:
        return None
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def plan_from_json(text: str | None) -> dict:
    """Parse the expenses.reminder_plan column. NULL/empty → default preset.
    A malformed value falls back to the default preset (never raises on the
    fire path) — logged, since it means a bug or manual DB edit."""
    if not text:
        return default_plan()
    try:
        return normalize_plan(json.loads(text))
    except (PlanError, ValueError, TypeError) as e:
        logger.error("Invalid reminder_plan %r, falling back to default: %s", text[:200], e)
        return default_plan()


def parse_at_time(value) -> tuple[int, int] | None:
    """'HH:MM' → (hour, minute), or None if malformed."""
    if not value or not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


# --- Slot math (shared by engine and tests) ----------------------------------

def slot_times(slot: dict) -> float:
    """times as a comparable number (None → +inf)."""
    times = slot.get("times")
    return float("inf") if times is None else float(times)


def slot_for_day(plan: dict, days_until: int) -> tuple[str, dict] | None:
    """Active slot for a given days_until (§6.2), ignoring exhaustion.
    Returns (slot_key, slot) or None (quiet day)."""
    if days_until < 0:
        return ("f", plan["f"]) if plan["f"]["on"] else None
    if days_until == 0:
        return ("d", plan["d"]) if plan["d"]["on"] else None
    for i, slot in enumerate(plan["n"]):
        if slot["on"] and slot["offset_days"] == days_until:
            return f"n{i}", slot
    return None


def iter_episodes(plan: dict, days_until: int) -> list[tuple[str, dict, int]]:
    """Chronological episodes from today onward: [(slot_key, slot, day_delta)].

    day_delta is days from today to the calendar day the episode starts
    (0 = today; negative only for an f episode that began in the past).
    Pre-due waves repeat only within their own calendar day (§2.2 / U9), so
    each n episode is exactly one day: due - offset_days. d = due day,
    f = day after due, open-ended.
    """
    episodes: list[tuple[str, dict, int]] = []
    if days_until > 0:
        for i, slot in enumerate(plan["n"]):
            if slot["on"] and slot["offset_days"] <= days_until:
                episodes.append((f"n{i}", slot, days_until - slot["offset_days"]))
        # plan["n"] is sorted by offset desc → deltas are already ascending
        if plan["d"]["on"]:
            episodes.append(("d", plan["d"], days_until))
        if plan["f"]["on"]:
            episodes.append(("f", plan["f"], days_until + 1))
    elif days_until == 0:
        if plan["d"]["on"]:
            episodes.append(("d", plan["d"], 0))
        if plan["f"]["on"]:
            episodes.append(("f", plan["f"], 1))
    else:
        if plan["f"]["on"]:
            episodes.append(("f", plan["f"], days_until + 1))
    return episodes


def has_any_reminders(plan: dict) -> bool:
    return any(s["on"] for s in plan["n"]) or plan["d"]["on"] or plan["f"]["on"]


# --- Create-screen UI model (§3) ----------------------------------------------
#
# The editor never exposes n0..n3/every_hours. Its state is a small dict:
#   {"n_3": "1x"|"2x"|"off",     # "3 days before"
#    "n_2": ..., "n_1": ...,     # "2 days before", "1 day before"
#    "extra": None | {"offset": int, "mode": "1x"|"2x"|"off"},   # "+ Another day"
#    "d": "1x"|"often"|"off",
#    "f": "daily"|"often"|"off"}
# plan_from_ui() maps it to the canonical plan via the §3.4 truth table.

UI_N_MODES = ("1x", "2x", "off")
UI_D_MODES = ("1x", "often", "off")
UI_F_MODES = ("daily", "often", "off")

_UI_N_SLOT = {
    "1x": {"times": 1, "every_hours": 24.0},
    "2x": {"times": 2, "every_hours": 12.0},
}
_UI_D_SLOT = {
    "1x": {"times": 1, "every_hours": 24.0},
    "often": {"times": None, "every_hours": 2.0},
}
_UI_F_SLOT = {
    "daily": {"times": None, "every_hours": 24.0},
    "often": {"times": None, "every_hours": 2.0},
}


def ui_default() -> dict:
    """The "Usual" preset - mirrors default_plan()."""
    return {"n_3": "1x", "n_2": "1x", "n_1": "1x", "extra": None, "d": "often", "f": "often"}


def ui_day_only() -> dict:
    """The "Due day only" preset."""
    return {"n_3": "off", "n_2": "off", "n_1": "off", "extra": None, "d": "1x", "f": "daily"}


def ui_all_off() -> dict:
    """The "All off" preset."""
    return {"n_3": "off", "n_2": "off", "n_1": "off", "extra": None, "d": "off", "f": "off"}


def cycle_ui(ui: dict, key: str) -> dict:
    """One tap on a slot button: advance its state in place (§3.3 hybrid
    cycle) and return the same dict. key: 'n_3'|'n_2'|'n_1'|'extra'|'d'|'f'."""
    if key in ("n_3", "n_2", "n_1"):
        modes = UI_N_MODES
        ui[key] = modes[(modes.index(ui[key]) + 1) % len(modes)]
    elif key == "extra":
        if ui.get("extra"):
            modes = UI_N_MODES
            mode = ui["extra"]["mode"]
            ui["extra"]["mode"] = modes[(modes.index(mode) + 1) % len(modes)]
    elif key == "d":
        modes = UI_D_MODES
        ui[key] = modes[(modes.index(ui[key]) + 1) % len(modes)]
    elif key == "f":
        modes = UI_F_MODES
        ui[key] = modes[(modes.index(ui[key]) + 1) % len(modes)]
    return ui


def plan_from_ui(ui: dict) -> dict:
    """§3.4 truth table: UI states → canonical plan. every_hours is never
    asked on create — sane defaults are substituted here. d.at_time is left
    None on purpose: the engine falls back to expense.reminder_time (U11),
    which keeps a default-preset plan storable as NULL."""
    n_slots = []
    for offset, mode in ((3, ui["n_3"]), (2, ui["n_2"]), (1, ui["n_1"])):
        if mode != "off":
            n_slots.append({"on": True, "offset_days": offset,
                            "at_time": None, **_UI_N_SLOT[mode]})
    extra = ui.get("extra")
    if extra and extra["mode"] != "off":
        n_slots.append({"on": True, "offset_days": extra["offset"],
                        "at_time": None, **_UI_N_SLOT[extra["mode"]]})

    d_mode, f_mode = ui["d"], ui["f"]
    plan = {
        "v": PLAN_VERSION,
        "n": n_slots,
        "d": {"on": True, "at_time": None, **_UI_D_SLOT[d_mode]} if d_mode != "off" else {"on": False},
        "f": {"on": True, **_UI_F_SLOT[f_mode]} if f_mode != "off" else {"on": False},
    }
    return normalize_plan(plan)


# --- AI / natural-language spec (set_reminder_plan tool) ---------------------
#
# The /ai tool speaks the same abstraction as the create screen — the caller
# picks WHICH days and coarse modes, every_hours is defaulted here — but over
# arbitrary offsets instead of the fixed 3/2/1 buttons.

AI_DUE_MODES = ("once", "repeat", "off")
AI_OVERDUE_MODES = ("daily", "repeat", "off")

_AI_D_SLOT = {
    "once": {"times": 1, "every_hours": 24.0},
    "repeat": {"times": None, "every_hours": 2.0},
}
_AI_F_SLOT = {
    "daily": {"times": None, "every_hours": 24.0},
    "repeat": {"times": None, "every_hours": 2.0},
}


def apply_ai_plan_changes(plan: dict, *, pre_due_days=None, due_day=None, overdue=None) -> dict:
    """Partial update of a plan from the /ai set_reminder_plan tool: only the
    provided dimensions change, the rest is kept. pre_due_days is a list of
    days-before-due (each fires once); due_day/overdue are coarse modes.
    Raises PlanError on bad input (out-of-range offset, duplicate day, >4
    waves — surfaced to the user)."""
    plan = copy.deepcopy(plan)
    if pre_due_days is not None:
        waves = []
        for d in pre_due_days:
            try:
                offset = int(d)
            except (TypeError, ValueError):
                raise PlanError("Days before due must be numbers")
            waves.append({"on": True, "offset_days": offset,
                          "times": 1, "every_hours": 24.0, "at_time": None})
        plan["n"] = waves
    if due_day is not None:
        plan["d"] = ({"on": True, "at_time": None, **_AI_D_SLOT[due_day]}
                     if due_day in _AI_D_SLOT else {"on": False})
    if overdue is not None:
        plan["f"] = ({"on": True, **_AI_F_SLOT[overdue]}
                     if overdue in _AI_F_SLOT else {"on": False})
    return normalize_plan(plan)


# --- Human summary (§3.6) -----------------------------------------------------

def plan_summary(plan: dict) -> str:
    """Short human summary of ENABLED parts of a canonical plan — for the
    editor line, the /add success message and /ai replies. Never the raw
    n0=… canon. Single source of truth for both the UI and the engine."""
    plan = normalize_plan(plan)
    parts: list[str] = []
    for slot in plan["n"]:  # already sorted by offset descending
        if not slot["on"]:
            continue
        label = f"{slot['offset_days']}d before"
        if slot.get("times") and slot["times"] > 1:
            label += f"·{slot['times']}x"
        parts.append(label)
    if plan["d"]["on"]:
        parts.append("due day" if plan["d"].get("times") == 1 else "due day (often)")
    if plan["f"]["on"]:
        parts.append("overdue (daily)" if plan["f"]["every_hours"] >= 24 else "overdue (often)")
    return " · ".join(parts) if parts else "no reminders"


def ui_summary(ui: dict) -> str:
    """Summary for the create editor — derived from the canonical plan so it
    can never drift from what actually gets saved."""
    return plan_summary(plan_from_ui(ui))


def ui_from_plan(plan: dict) -> dict | None:
    """Best-effort inverse of plan_from_ui, for re-opening the button editor
    on an EXISTING record. Returns a UI dict, or None if the plan can't be
    represented on the create screen (offset other than 3/2/1 plus at most
    one extra, a wave with times>2, or a non-standard interval). Callers
    route the None case to /ai, which handles arbitrary plans. The final
    round-trip check guarantees no silent lossy edit."""
    plan = normalize_plan(plan)
    ui = {"n_3": "off", "n_2": "off", "n_1": "off", "extra": None, "d": "off", "f": "off"}
    n_mode = {(1, 24.0): "1x", (2, 12.0): "2x"}
    for slot in plan["n"]:
        if not slot["on"]:
            continue
        mode = n_mode.get((slot["times"], slot["every_hours"]))
        if mode is None:
            return None
        off = slot["offset_days"]
        if off == 3:
            ui["n_3"] = mode
        elif off == 2:
            ui["n_2"] = mode
        elif off == 1:
            ui["n_1"] = mode
        elif ui["extra"] is None:
            ui["extra"] = {"offset": off, "mode": mode}
        else:
            return None  # more than one non-3/2/1 wave — not representable
    d = plan["d"]
    if d["on"]:
        if d.get("times") == 1 and d["every_hours"] == 24.0:
            ui["d"] = "1x"
        elif d.get("times") is None and d["every_hours"] == 2.0:
            ui["d"] = "often"
        else:
            return None
    f = plan["f"]
    if f["on"]:
        if f.get("times") is None and f["every_hours"] == 24.0:
            ui["f"] = "daily"
        elif f.get("times") is None and f["every_hours"] == 2.0:
            ui["f"] = "often"
        else:
            return None
    if plan_from_ui(ui) != plan:
        return None  # lossy — refuse rather than silently drop detail
    return ui
