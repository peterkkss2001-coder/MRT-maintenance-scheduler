"""
Multi-Sector Maintenance Window Shift Schedule (auto-assigned) + Login.

Roles:
    - supervisor: full access. Can report problems (auto-assign), manually
      add/override a shift directly, and delete any shift.
    - worker: view-only. Sees the schedule table but no forms and no
      delete buttons.

Default accounts (CHANGE THESE before deploying anywhere public):
    supervisor / super123   (role: supervisor)
    worker     / work123    (role: worker)

- Three independent sectors: Sector A, Sector B, Sector C.
  Each sector's schedule is completely separate from the others.
- Shared employee roster across all sectors (14 staff with fixed roles).
- Supervisors submit just a Problem category + Importance (1-10); the app
  automatically picks an eligible staff member, the nearest day (based on
  importance), and the nearest free 1-hour block, respecting:
      - SECTOR EXCLUSIVITY: only one shift can be active in a sector at
        any given overlapping time.
      - Staff can't be double-booked across sectors at the same time.
- Supervisors can also manually add/override a specific shift directly.
- Data is saved to shifts_data.json so it survives restarts.

Run with:
    python shift_schedule_app.py

Then open http://localhost:8000
"""

import json
import os
from functools import wraps
from datetime import datetime, timedelta

from flask import (
    Flask, render_template_string, request, redirect, url_for, session, abort
)

app = Flask(__name__)
app.secret_key = "shift-schedule-dev-key"  # CHANGE THIS before deploying publicly

DATA_FILE = os.path.join(os.path.dirname(__file__), "shifts_data.json")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # matches datetime.weekday() order (Mon=0)
SECTORS = ["Sector A", "Sector B", "Sector C"]

# ---------------------------------------------------------------------------
# User accounts
# ---------------------------------------------------------------------------
def _username_for(staff_name: str) -> str:
    return staff_name.lower().replace(" ", "")


# Shared employee roster - no free typing needed
STAFF = [
    {"name": "Ali", "role": "Track Engineer"},
    {"name": "Min Khant", "role": "Electrical Engineering Technician"},
    {"name": "Lucas", "role": "Electrical Engineering Technician"},
    {"name": "Emily", "role": "Operations Controller"},
    {"name": "Alex Tan", "role": "Track Engineer"},
    {"name": "Daniel Lim", "role": "Operations Controller"},
    {"name": "Marcus Lee", "role": "Electrical Engineering Technician"},
    {"name": "Ryan Koh", "role": "Track Engineer"},
    {"name": "Ethan Wong", "role": "Electrical Engineering Technician"},
    {"name": "Jason Ng", "role": "Operations Controller"},
    {"name": "Kevin Chua", "role": "Electrical Engineering Technician"},
    {"name": "Aaron Teo", "role": "Track Engineer"},
    {"name": "Benjamin Ho", "role": "Operations Controller"},
    {"name": "Samuel Goh", "role": "Track Engineer"},
]

USERS = {
    "supervisor": {"password": "super123", "role": "supervisor"},
}
# One individual worker account per staff member.
# Username = name with spaces removed, lowercased. Password = username + "123".
# e.g. "Min Khant" -> username "minkhant", password "minkhant123"
for _person in STAFF:
    _uname = _username_for(_person["name"])
    USERS[_uname] = {
        "password": f"{_uname}123",
        "role": "worker",
        "staff_name": _person["name"],
    }

# Problem categories -> color mapping
PROBLEM_COLORS = {
    "Track & Infrastructure": "#3B82F6",                    # blue
    "Electrical, Signalling & Equipment": "#22A559",         # green
    "Operations & Safety": "#E5484D",                        # red
}
PROBLEMS = list(PROBLEM_COLORS.keys())

# Problem category -> the staff role eligible to handle it
ROLE_FOR_PROBLEM = {
    "Track & Infrastructure": "Track Engineer",
    "Electrical, Signalling & Equipment": "Electrical Engineering Technician",
    "Operations & Safety": "Operations Controller",
}

# Emergency types -> corner emoji tag
EMERGENCY_TYPES = [
    "Add More Time",
    "Medical Emergency",
    "Injury",
    "Fire",
    "Electrical Hazard",
    "Equipment Failure",
    "Chemical/Hazardous Material",
    "Security/Safety Threat",
    "Structural Hazard",
    "Other",
]
EMERGENCY_EMOJIS = {
    "Add More Time": "⏱️",
    "Medical Emergency": "🚑",
    "Injury": "🤕",
    "Fire": "🔥",
    "Electrical Hazard": "⚡",
    "Equipment Failure": "🛠️",
    "Chemical/Hazardous Material": "☣️",
    "Security/Safety Threat": "🚨",
    "Structural Hazard": "🏚️",
    "Other": "❗",
}

SHIFT_DURATION_MIN = 60  # every auto-assigned shift is exactly 1 hour
MIN_SHIFT_MINUTES = 60   # manual shifts must also be at least this long
EMERGENCY_EXTEND_MIN = 60  # "Add more time" extends a shift by this many minutes

# ============================================================================
# >>> SCHEDULE START DATE <<<
# Treated as "today" for every urgency calculation (i.e. the day an
# importance-10 problem gets scheduled on).
#   - Leave as datetime.now() to always anchor on the real current day.
#   - Or hard-code a specific date, e.g. datetime(2026, 8, 31)
# ============================================================================
SCHEDULE_START_DATE = datetime(2026, 8, 31)


# Allowed time slots: 1:00 AM to 4:30 AM, 30-minute steps
def generate_time_slots():
    slots = []
    t = datetime.strptime("01:00", "%H:%M")
    end = datetime.strptime("04:30", "%H:%M")
    while t <= end:
        slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=30)
    return slots


TIME_SLOTS = generate_time_slots()  # ["01:00", "01:30", ..., "04:30"]
WINDOW_END = TIME_SLOTS[-1]  # "04:30"

# Valid 1-hour shift START times: any slot where start + 1hr still fits
# inside the 1:00 AM - 4:30 AM window (so the last valid start is 3:30 AM).
CANDIDATE_STARTS = [
    t for t in TIME_SLOTS
    if datetime.strptime(t, "%H:%M") + timedelta(minutes=SHIFT_DURATION_MIN)
    <= datetime.strptime(TIME_SLOTS[-1], "%H:%M")
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def supervisor_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "supervisor":
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Simple JSON persistence
# ---------------------------------------------------------------------------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}
    for sector in SECTORS:
        data.setdefault(sector, {})
    return data


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def format_time_12h(t: str) -> str:
    dt = datetime.strptime(t, "%H:%M")
    return dt.strftime("%#I:%M %p") if os.name == "nt" else dt.strftime("%-I:%M %p")


def format_time_range(start: str, end: str) -> str:
    return f"{format_time_12h(start)} - {format_time_12h(end)}"


def normalize_paired_with(value) -> list:
    """
    paired_with used to be stored as a single string in an earlier version
    of this app. Old shifts_data.json files may still have that shape.
    This always returns a clean list regardless of which shape is on disk.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def parse_shift_key(key: str):
    """
    Shift dict keys are normally '{staff_name}|{day}' for a person's main
    shift. A person can ALSO have an extra bonus shift on the same
    day/sector (created by the "Add More Time" emergency flow) - those use
    '{staff_name}|{day}|{slot}' (slot e.g. 'extra1', 'extra2', ...).
    Returns (staff_name, day, slot) - slot is 'main' for the primary shift.
    """
    parts = key.split("|")
    if len(parts) >= 3:
        return parts[0], parts[1], "|".join(parts[2:])
    return parts[0], parts[1], "main"


def make_shift_key(staff_name: str, day: str, slot: str = "main") -> str:
    if slot == "main":
        return f"{staff_name}|{day}"
    return f"{staff_name}|{day}|{slot}"


def make_extra_key(staff_name: str, day: str, sector_data: dict) -> str:
    """Generates the next free 'extraN' key for this staff/day within a sector."""
    i = 1
    while True:
        key = make_shift_key(staff_name, day, f"extra{i}")
        if key not in sector_data:
            return key
        i += 1


def times_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    s1, e1 = datetime.strptime(start1, "%H:%M"), datetime.strptime(end1, "%H:%M")
    s2, e2 = datetime.strptime(start2, "%H:%M"), datetime.strptime(end2, "%H:%M")
    return s1 < e2 and s2 < e1


def staff_is_free_across_sectors(data, staff_name, day, start, end, ignore_key=None) -> bool:
    """
    A staff member can only be in ONE sector at a time. This checks every
    sector's shifts (not just the one being considered) to make sure the
    staff member isn't already booked somewhere else at an overlapping time.
    e.g. if Ali is working Sector A 1:00-2:00, he cannot also be assigned
    to Sector B (or C) at 1:00-2:00 - only after 2:00 is he free again.

    ignore_key: optional (sector, key) tuple to skip - used when checking
    whether a shift can be EXTENDED, so it doesn't conflict with itself.
    """
    for sector in SECTORS:
        for key, shift in data[sector].items():
            if ignore_key is not None and (sector, key) == ignore_key:
                continue
            name, shift_day, slot = parse_shift_key(key)
            if name == staff_name and shift_day == day and times_overlap(
                start, end, shift["start"], shift["end"]
            ):
                return False
    return True


def remove_pairing_references(data, sector, day, staff_name, paired_with):
    """
    When a shift is deleted, clean up any 'paired_with' tags on the
    partner's shift(s) that referenced this now-deleted person, so their
    name doesn't keep showing up on someone else's shift after removal.
    """
    for partner_name in paired_with:
        partner_key = f"{partner_name}|{day}"
        if partner_key in data[sector]:
            partner_entry = data[sector][partner_key]
            partner_list = normalize_paired_with(partner_entry.get("paired_with"))
            if staff_name in partner_list:
                partner_list.remove(staff_name)
            partner_entry["paired_with"] = partner_list


# ---------------------------------------------------------------------------
# Auto-scheduling logic
# ---------------------------------------------------------------------------
def importance_to_day_offset(importance: int) -> int:
    """
    Importance  8-10 -> offset 0 (immediate / same day)
    Importance  5-7  -> offset 1 (1 day later)
    Importance  3-4  -> offset 2 (2 days later)
    Importance  1-2  -> offset 3 (3 days later)
    """
    importance = max(1, min(10, importance))
    if importance >= 8:
        return 0
    elif importance >= 5:
        return 1
    elif importance >= 3:
        return 2
    else:
        return 3


def auto_schedule(data, requested_sector, problem, importance):
    """
    Finds the nearest available sector / day / 1-hour time slot / eligible
    staff member. Searches ALL sectors (A, B and C); the problem can land
    in a different sector than the one the supervisor was viewing.
    """
    role = ROLE_FOR_PROBLEM.get(problem)
    eligible = [p["name"] for p in STAFF if p["role"] == role]

    if not eligible:
        return None, f"No staff with the role needed for '{problem}'."

    today_index = SCHEDULE_START_DATE.weekday()
    start_offset = importance_to_day_offset(importance)

    for day_offset in range(start_offset, start_offset + 7):
        day = DAYS[(today_index + day_offset) % 7]

        for search_sector in SECTORS:
            day_shifts = {
                k: s for k, s in data[search_sector].items()
                if parse_shift_key(k)[1] == day
            }

            for start in CANDIDATE_STARTS:
                end = (
                    datetime.strptime(start, "%H:%M")
                    + timedelta(minutes=SHIFT_DURATION_MIN)
                ).strftime("%H:%M")

                slot_free = all(
                    not times_overlap(start, end, s["start"], s["end"])
                    for s in day_shifts.values()
                )
                if not slot_free:
                    continue

                for staff_name in eligible:
                    key = f"{staff_name}|{day}"
                    if key in data[search_sector]:
                        continue
                    if not staff_is_free_across_sectors(data, staff_name, day, start, end):
                        continue
                    return {
                        "staff_name": staff_name,
                        "sector": search_sector,
                        "day": day,
                        "start": start,
                        "end": end,
                        "problem": problem,
                        "importance": importance,
                        "requested_sector": requested_sector,
                    }, None

    return None, (
        "No available slot found in Sector A, Sector B or Sector C "
        "within the next 7 days."
    )


def get_personal_week(data, staff_name):
    """
    Returns {day: [ {time_range, problem, importance, color, sector, start}, ... ]}
    for one staff member, across ALL sectors. A day can have more than one
    shift (e.g. different sectors at non-overlapping times), listed in
    start-time order.
    """
    result = {}
    for sector in SECTORS:
        for key, s in data[sector].items():
            name, day, slot = parse_shift_key(key)
            if name == staff_name:
                result.setdefault(day, []).append({
                    "time_range": format_time_range(s["start"], s["end"]),
                    "problem": s["problem"],
                    "importance": s.get("importance", "-"),
                    "color": PROBLEM_COLORS.get(s["problem"], "#999999"),
                    "sector": sector,
                    "start": s["start"],
                    "paired_with": normalize_paired_with(s.get("paired_with")),
                    "emergency": s.get("emergency"),
                })

    for day in result:
        result[day].sort(key=lambda shift: shift["start"])

    return result


def auto_pair_day(data, day):
    """
    For a given day: every staff member with NO shift at all that day (in
    any sector) is paired up with a same-role staff member who DOES have
    a shift that day - at the exact SAME sector/time as that partner, so
    they work the shift together. Searches ALL sectors and ALL roles.

    Conflict avoidance: only staff with zero shifts that day are eligible
    to be paired. Anyone who already has their own shift that day (any
    time, any sector) is left alone and is never double-booked or moved.

    This actually creates a new shift entry for the paired person (same
    sector/day/start/end/problem/importance as their partner) - it's a
    real schedule change, separate from and independent of auto_schedule().
    BOTH entries (the newly-paired person's and their partner's original
    one) get a "paired_with" list so the pairing shows on both shift cells.

    Returns (paired, unpaired):
        paired   -> list of {staff_name, partner_name, sector, time_range}
        unpaired -> list of staff names that had no shift and no same-role
                    partner working that day, so nothing could be done
    """
    has_any_shift_today = set()  # staff names with ANY shift (main or extra) this day
    main_shift_by_name = {}      # staff_name -> {sector, key, start, end, problem, importance} - MAIN slot only

    for sector in SECTORS:
        for key, shift in data[sector].items():
            name, shift_day, slot = parse_shift_key(key)
            if shift_day != day:
                continue
            has_any_shift_today.add(name)
            if slot == "main":
                main_shift_by_name[name] = {"sector": sector, "key": key, **shift}

    paired = []
    unpaired = []

    for person in STAFF:
        if person["name"] in has_any_shift_today:
            continue  # already has their own shift this day - skip entirely

        match_name = None
        for other in STAFF:
            if (
                other["name"] != person["name"]
                and other["role"] == person["role"]
                and other["name"] in main_shift_by_name
            ):
                match_name = other["name"]
                break

        if match_name is None:
            unpaired.append(person["name"])
            continue

        info = main_shift_by_name[match_name]
        sector, start, end = info["sector"], info["start"], info["end"]

        # Create the new entry for the person being paired in
        person_key = f"{person['name']}|{day}"
        data[sector][person_key] = {
            "start": start,
            "end": end,
            "problem": info["problem"],
            "importance": info.get("importance", "-"),
            "paired_with": [match_name],
        }

        # Tag the partner's existing entry too, so their shift cell also
        # shows the pairing (not just the newly-added person's cell).
        partner_entry = data[sector][info["key"]]
        partner_names = normalize_paired_with(partner_entry.get("paired_with"))
        if person["name"] not in partner_names:
            partner_names.append(person["name"])
        partner_entry["paired_with"] = partner_names

        paired.append({
            "staff_name": person["name"],
            "partner_name": match_name,
            "sector": sector,
            "day": day,
            "time_range": format_time_range(start, end),
        })

    return paired, unpaired


def get_sector_week(data, sector):
    """
    Returns {"{staff}|{day}": [shift-dicts...]} for a single sector,
    shifts sorted by start time. Supports a staff member having more than
    one shift on the same day in this sector (main shift + an "Add More
    Time" extra shift).
    """
    result = {}
    for key, s in data[sector].items():
        staff_name, day, slot = parse_shift_key(key)
        display_key = f"{staff_name}|{day}"
        result.setdefault(display_key, []).append({
            "time_range": format_time_range(s["start"], s["end"]),
            "problem": s["problem"],
            "importance": s.get("importance", "-"),
            "color": PROBLEM_COLORS.get(s["problem"], "#999999"),
            "paired_with": normalize_paired_with(s.get("paired_with")),
            "emergency": s.get("emergency"),
            "start": s["start"],
            "slot": slot,
        })

    for k in result:
        result[k].sort(key=lambda shift: shift["start"])

    return result


def auto_pair_week(data):
    """
    Runs auto_pair_day() for every day of the week (Mon..Sun), across all
    sectors and all roles, in one shot. Returns the combined results:
        paired   -> list of {staff_name, partner_name, sector, day, time_range}
        unpaired -> list of {name, day} for anyone who couldn't be paired
                    on a given day
    """
    all_paired = []
    all_unpaired = []

    for day in DAYS:
        paired, unpaired = auto_pair_day(data, day)
        all_paired.extend(paired)
        all_unpaired.extend({"name": name, "day": day} for name in unpaired)

    return all_paired, all_unpaired


def list_active_shifts(data):
    """
    Returns every currently-active shift across all sectors/days, as a
    flat list, for populating the Emergency dropdown. Each entry includes
    a combined identifier ("sector||staff_name||day") used to look the
    shift back up on submit.
    """
    shifts = []
    for sector in SECTORS:
        for key, s in data[sector].items():
            staff_name, day, slot = parse_shift_key(key)
            shifts.append({
                "id": f"{sector}||{staff_name}||{day}||{slot}",
                "sector": sector,
                "staff_name": staff_name,
                "day": day,
                "slot": slot,
                "time_range": format_time_range(s["start"], s["end"]),
                "problem": s["problem"],
            })
    # Sort for a stable, readable dropdown: by day, then sector, then start time
    day_order = {d: i for i, d in enumerate(DAYS)}
    shifts.sort(key=lambda x: (day_order[x["day"]], x["sector"], x["time_range"]))
    return shifts


def try_add_extra_shift(data, sector, staff_name, day, slot):
    """
    Creates a brand-new, standalone shift for staff_name in this
    sector/day, starting exactly when their existing shift (identified by
    slot) ends. Runs up to EMERGENCY_EXTEND_MIN minutes, but is trimmed
    short by whichever comes first:
        - the 4:30 AM schedule window
        - another shift already booked in this sector that day
        - this staff member having another shift elsewhere that would
          then overlap
    The ORIGINAL shift is never modified - this only adds a new one, and
    only for this staff member (nobody else, e.g. no paired partner).
    Returns (created: bool, message: str).
    """
    key = make_shift_key(staff_name, day, slot)
    if key not in data[sector]:
        return False, "Could not add more time (original shift not found)."

    base_shift = data[sector][key]
    new_start = base_shift["end"]
    new_start_dt = datetime.strptime(new_start, "%H:%M")
    window_end_dt = datetime.strptime(WINDOW_END, "%H:%M")

    if new_start_dt >= window_end_dt:
        return False, "No free time after this shift (already at the end of the 4:30 AM schedule window)."

    desired_end_dt = min(new_start_dt + timedelta(minutes=EMERGENCY_EXTEND_MIN), window_end_dt)

    # Trim short if another shift in this sector/day starts before our desired end
    for other_key, other_shift in data[sector].items():
        if other_key == key:
            continue
        other_name, other_day, other_slot = parse_shift_key(other_key)
        if other_day != day:
            continue
        other_start_dt = datetime.strptime(other_shift["start"], "%H:%M")
        if new_start_dt <= other_start_dt < desired_end_dt:
            desired_end_dt = other_start_dt

    if desired_end_dt <= new_start_dt:
        return False, "No free time after this shift (the sector is booked immediately after)."

    new_end = desired_end_dt.strftime("%H:%M")

    if not staff_is_free_across_sectors(data, staff_name, day, new_start, new_end):
        return False, "No free time after this shift (this person has another shift elsewhere at that time)."

    extra_key = make_extra_key(staff_name, day, data[sector])
    data[sector][extra_key] = {
        "start": new_start,
        "end": new_end,
        "problem": base_shift["problem"],
        "importance": base_shift.get("importance", "-"),
        "emergency": "Add More Time",
    }
    return True, f"Added a new shift for {staff_name}: {format_time_range(new_start, new_end)}."


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login &mdash; Sector Shift Schedule</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: Arial, Helvetica, sans-serif;
            background: #f7f4ef;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .login-box {
            background: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            width: 100%;
            max-width: 340px;
        }
        h1 { font-size: 1.3rem; margin-top: 0; }
        label { display: block; font-size: 0.85rem; color: #444; margin-bottom: 1rem; }
        input {
            width: 100%;
            padding: 0.55rem;
            margin-top: 0.3rem;
            border: 1px solid #ccc;
            border-radius: 4px;
        }
        button {
            width: 100%;
            background: #2b2b33;
            color: white;
            border: none;
            padding: 0.6rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.95rem;
        }
        button:hover { background: #1a1a1f; }
        .error { background: #fdecea; color: #a3231f; padding: 0.6rem; border-radius: 5px; font-size: 0.82rem; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Sign in</h1>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <label>Username
                <input type="text" name="username" required autofocus>
            </label>
            <label>Password
                <input type="password" name="password" required>
            </label>
            <button type="submit">Sign in</button>
        </form>
    </div>
</body>
</html>
"""

PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sector Shift Schedule</title>
    <style>
        * { box-sizing: border-box; }
        html, body { width: 100%; overflow-x: hidden; }
        body {
            font-family: Arial, Helvetica, sans-serif;
            background: #f7f4ef;
            margin: 0;
            padding: 0.8rem 1.2rem;
            color: #222;
        }
        .container { width: 100%; margin: 0; }
        .topbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }
        h1 { font-size: clamp(1.2rem, 2vw, 1.9rem); margin-bottom: 0.2rem; }
        p.subtitle { color: #555; margin-top: 0; margin-bottom: 0.8rem; font-size: clamp(0.75rem, 0.9vw, 0.95rem); }
        .account-info { font-size: 0.82rem; color: #444; display: flex; align-items: center; gap: 0.8rem; }
        .account-info a { color: #2b2b33; font-weight: bold; text-decoration: none; }
        .account-info a:hover { text-decoration: underline; }
        .role-badge {
            background: #2b2b33; color: white; padding: 0.15rem 0.5rem;
            border-radius: 10px; font-size: 0.72rem; text-transform: uppercase;
        }

        .tabs { display: flex; gap: 0.4rem; margin-bottom: 1.2rem; margin-top: 1rem; }
        .tab {
            padding: 0.55rem 1.3rem;
            border-radius: 6px 6px 0 0;
            background: #e5e0d8;
            color: #444;
            text-decoration: none;
            font-weight: bold;
            font-size: 0.9rem;
        }
        .tab.active { background: #2b2b33; color: white; }

        .error-banner {
            background: #fdecea; border: 1px solid #E5484D; color: #a3231f;
            padding: 0.7rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.88rem;
        }
        .info-banner {
            background: #eaf7ee; border: 1px solid #22A559; color: #1a6b3c;
            padding: 0.7rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.88rem;
        }

        .panels-row { display: flex; gap: 1.2rem; flex-wrap: wrap; align-items: flex-start; }
        .panels-row form.panel { flex: 1 1 320px; margin-top: 1.8rem; }
        .danger-panel { border: 1px solid #f3c9c7; }
        .danger-btn { background: #a3231f; }
        .danger-btn:hover { background: #7a1a17; }
        .emergency-panel { border: 1px solid #ffd28a; }
        .emergency-btn { background: #b35c00; }
        .emergency-btn:hover { background: #8a4700; }

        .legend { display: flex; gap: 1.2rem; margin-bottom: 1rem; flex-wrap: wrap; }
        .legend-item { display: flex; align-items: center; gap: 0.4rem; font-size: 0.78rem; color: #444; }
        .legend-swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; flex-shrink: 0; }

        .table-wrap { width: 100%; max-width: 100%; }
        table {
            width: 100%; max-width: 100%; table-layout: fixed; border-collapse: collapse;
            background: white; box-shadow: 0 2px 8px rgba(0,0,0,0.06); font-size: clamp(0.72rem, 1vw, 1rem);
        }
        th, td {
            border-bottom: 1px solid #eee; padding: clamp(0.4rem, 0.7vw, 0.9rem) clamp(0.3rem, 0.5vw, 0.6rem);
            vertical-align: top; overflow-wrap: break-word; word-break: break-word;
        }
        th { background: #2b2b33; color: white; text-align: left; font-size: clamp(0.72rem, 1vw, 1rem); }
        th.staff-col, td.staff-col { width: 14%; }
        th.day-col, td.day-col { width: 12.28%; }

        .staff-name { font-weight: bold; font-size: clamp(0.75rem, 1.05vw, 1.05rem); }
        .staff-role { font-size: clamp(0.62rem, 0.85vw, 0.85rem); color: #777; line-height: 1.25; }
        .blank-cell { color: #ccc; font-size: clamp(0.9rem, 1.2vw, 1.2rem); }

        .shift-pill {
            position: relative; display: block;
            padding: clamp(0.25rem, 0.4vw, 0.5rem) clamp(0.35rem, 0.5vw, 0.6rem);
            padding-right: clamp(1.1rem, 1.6vw, 1.5rem);
            border-radius: 4px; color: white; font-size: clamp(0.68rem, 0.95vw, 0.95rem); line-height: 1.3;
            overflow: hidden;
        }
        .shift-pill .time { position: relative; z-index: 1; display: block; font-weight: bold; }
        .shift-pill .problem { position: relative; z-index: 1; display: block; font-size: clamp(0.6rem, 0.82vw, 0.82rem); opacity: 0.92; }
        .shift-pill .importance { position: relative; z-index: 1; display: block; font-size: clamp(0.58rem, 0.78vw, 0.78rem); opacity: 0.85; margin-top: 0.15rem; }
        .shift-pill .paired-tag {
            position: relative; z-index: 1;
            display: block; font-size: clamp(0.62rem, 0.86vw, 0.86rem); font-weight: bold;
            background: rgba(255,255,255,0.25); border-radius: 3px; padding: 0.1rem 0.3rem;
            margin-bottom: 0.25rem; width: fit-content;
        }
        .emergency-emoji {
            position: absolute; inset: 0;
            display: flex; align-items: center; justify-content: center;
            font-size: clamp(1.6rem, 3.4vw, 2.6rem);
            opacity: 0.32;
            z-index: 0;
            pointer-events: none;
        }
        .delete-form { position: absolute; top: 2px; right: 2px; margin: 0; z-index: 2; }
        .delete-btn {
            background: rgba(0,0,0,0.25); border: none; color: white;
            width: clamp(14px, 1.3vw, 18px); height: clamp(14px, 1.3vw, 18px);
            border-radius: 50%; font-size: clamp(0.6rem, 0.85vw, 0.75rem);
            line-height: 1; cursor: pointer; padding: 0;
        }
        .delete-btn:hover { background: rgba(0,0,0,0.5); }

        form.panel {
            margin-top: 1.8rem; background: white; padding: 1.3rem; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.06); max-width: 640px;
        }
        form.panel h2 { margin-top: 0; font-size: 1.05rem; }
        form.panel p.hint { color: #777; font-size: 0.82rem; margin-top: -0.4rem; margin-bottom: 1rem; }
        .form-row { display: flex; gap: 0.9rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: stretch; }
        .form-row label { display: flex; flex-direction: column; justify-content: flex-end; font-size: 0.8rem; color: #444; flex: 1; min-width: 160px; }
        .form-row label .label-text { display: block; min-height: 2.3em; line-height: 1.25; }
        .form-row input, .form-row select {
            padding: 0.45rem; border: 1px solid #ccc; border-radius: 4px; margin-top: 0.25rem;
        }
        button {
            background: #2b2b33; color: white; border: none; padding: 0.55rem 1.1rem;
            border-radius: 4px; cursor: pointer; font-size: 0.9rem;
        }
        button:hover { background: #1a1a1f; }
        .view-only-note { color: #777; font-size: 0.85rem; margin-top: 1.5rem; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <div class="topbar">
            <div>
                <h1>Sector Shift Schedule</h1>
                <p class="subtitle">Each sector's schedule is independent, and only one shift can run in a sector at a time.</p>
            </div>
            <div class="account-info">
                Signed in as <strong>{{ username }}</strong>
                <span class="role-badge">{{ role }}</span>
                <a href="{{ url_for('logout') }}">Log out</a>
            </div>
        </div>

        <div class="tabs">
            {% for sector in sectors %}
            <a class="tab {% if sector == current_sector %}active{% endif %}" href="{{ url_for('index', sector=sector) }}">{{ sector }}</a>
            {% endfor %}
        </div>

        {% if error %}
        <div class="error-banner">{{ error }}</div>
        {% endif %}

        {% if info %}
        <div class="info-banner">{{ info }}</div>
        {% endif %}

        <div class="legend">
            {% for problem, color in problem_colors.items() %}
            <div class="legend-item">
                <span class="legend-swatch" style="background:{{ color }}"></span>
                {{ problem }}
            </div>
            {% endfor %}
        </div>

        <div class="table-wrap">
        <table>
            <tr>
                <th class="staff-col">Staff</th>
                {% for day in days %}
                <th class="day-col">{{ day }}</th>
                {% endfor %}
            </tr>
            {% for person in staff %}
            {% for row_index in range(max_rows) %}
            <tr>
                {% if row_index == 0 %}
                <td class="staff-col" rowspan="{{ max_rows }}">
                    <div class="staff-name">{{ person.name }}</div>
                    <div class="staff-role">{{ person.role }}</div>
                </td>
                {% endif %}
                {% for day in days %}
                <td class="day-col">
                    {% set key = person.name ~ '|' ~ day %}
                    {% if key in shifts and row_index < shifts[key]|length %}
                        {% set s = shifts[key][row_index] %}
                        <span class="shift-pill" style="background:{{ s.color }}">
                            {% if s.emergency %}
                            <span class="emergency-emoji" title="{{ s.emergency }}">{{ emergency_emojis.get(s.emergency, '') }}</span>
                            {% endif %}
                            {% if role == 'supervisor' %}
                            <form class="delete-form" method="POST" action="{{ url_for('delete_shift') }}">
                                <input type="hidden" name="sector" value="{{ current_sector }}">
                                <input type="hidden" name="staff_name" value="{{ person.name }}">
                                <input type="hidden" name="day" value="{{ day }}">
                                <input type="hidden" name="slot" value="{{ s.slot }}">
                                <button type="submit" class="delete-btn" title="Delete this shift">&times;</button>
                            </form>
                            {% endif %}
                            {% if s.paired_with %}
                            <span class="paired-tag">{{ ([person.name] + s.paired_with)|join(' & ') }}</span>
                            {% endif %}
                            <span class="time">{{ s.time_range }}</span>
                            <span class="problem">{{ s.problem }}</span>
                            <span class="importance">Importance: {{ s.importance }}/10</span>
                        </span>
                    {% else %}
                        <span class="blank-cell">&mdash;</span>
                    {% endif %}
                </td>
                {% endfor %}
            </tr>
            {% endfor %}
            {% endfor %}
        </table>
        </div>

        {% if role == 'supervisor' %}
        <div class="panels-row">
        <form class="panel" method="POST" action="{{ url_for('report_problem') }}">
            <h2>Report a Problem &mdash; {{ current_sector }}</h2>
            <p class="hint">Staff, day and time are picked automatically: eligible staff by problem type, day/time by nearest available slot based on importance.</p>
            <input type="hidden" name="sector" value="{{ current_sector }}">
            <div class="form-row">
                <label><span class="label-text">Problem</span>
                    <select name="problem" required>
                        {% for problem in problems %}
                        <option value="{{ problem }}">{{ problem }}</option>
                        {% endfor %}
                    </select>
                </label>
                <label><span class="label-text">Importance (1-10, 10 = most urgent)</span>
                    <select name="importance" required>
                        {% for i in range(10, 0, -1) %}
                        <option value="{{ i }}" {% if i == 5 %}selected{% endif %}>{{ i }}{% if i == 10 %} - Immediate (today){% elif i == 8 %} - Same day{% elif i == 7 %} - 1 day later{% elif i == 4 %} - 2 days later{% elif i == 2 %} - 3 days later{% endif %}</option>
                        {% endfor %}
                    </select>
                </label>
            </div>
            <button type="submit">Auto-Assign Shift</button>
        </form>

        <form class="panel" method="POST" action="{{ url_for('auto_pair') }}">
            <h2>Pair Up Uncovered Staff</h2>
            <p class="hint">Scans every sector, every role, every day of the week (Mon-Sun). Anyone with no shift on a given day gets paired onto the SAME sector/time as a same-role staff member who already has one that day, so nobody is left with nothing to do. Anyone who already has their own shift that day is left untouched.</p>
            <input type="hidden" name="sector" value="{{ current_sector }}">
            <button type="submit">Auto-Pair Whole Week</button>
        </form>

        <form class="panel emergency-panel" method="POST" action="{{ url_for('emergency') }}">
            <h2>Report Emergency</h2>
            <p class="hint">Flags the shift with a big icon, and automatically flags a paired partner's shift too. Choosing "Add More Time" also adds a brand-new shift right after this one for this person only (if there's free time) - the original shift is never changed. Any other type only flags.</p>
            <input type="hidden" name="sector" value="{{ current_sector }}">
            <div class="form-row">
                <label><span class="label-text">Emergency type</span>
                    <select name="emergency_type" required>
                        {% for etype in emergency_types %}
                        <option value="{{ etype }}">{{ emergency_emojis.get(etype, '') }} {{ etype }}</option>
                        {% endfor %}
                    </select>
                </label>
                <label><span class="label-text">Active shift</span>
                    <select name="shift_id" required>
                        {% for s in active_shifts %}
                        <option value="{{ s.id }}">{{ s.staff_name }} &mdash; {{ s.day }}, {{ s.sector }}, {{ s.time_range }}{% if s.slot != 'main' %} (extra){% endif %}</option>
                        {% endfor %}
                    </select>
                </label>
            </div>
            <button type="submit" class="emergency-btn">Report Emergency</button>
        </form>

        <form class="panel danger-panel" method="POST" action="{{ url_for('delete_all_shifts') }}" onsubmit="return confirm('This will permanently delete ALL shifts in ALL sectors (A, B and C) for the entire week. This cannot be undone. Continue?');">
            <h2>Clear All Shifts</h2>
            <p class="hint">Deletes every shift in every sector (A, B and C) for the whole week. This cannot be undone - use it to start the schedule fresh.</p>
            <input type="hidden" name="sector" value="{{ current_sector }}">
            <button type="submit" class="danger-btn">Delete All Shifts (All Sectors)</button>
        </form>
        </div>
        {% else %}
        <p class="view-only-note">You're signed in as a worker &mdash; this view is read-only.</p>
        {% endif %}
    </div>
</body>
</html>
"""

WORKER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>My Shift Schedule</title>
    <style>
        * { box-sizing: border-box; }
        html, body { width: 100%; overflow-x: hidden; }
        body {
            font-family: Arial, Helvetica, sans-serif;
            background: #f7f4ef;
            margin: 0;
            padding: 0.8rem 1.2rem;
            color: #222;
        }
        .container { width: 100%; margin: 0; }
        .topbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }
        h1 { font-size: clamp(1.2rem, 2vw, 1.9rem); margin-bottom: 0.2rem; }
        p.subtitle { color: #555; margin-top: 0; margin-bottom: 0.8rem; font-size: clamp(0.75rem, 0.9vw, 0.95rem); }
        .account-info { font-size: 0.82rem; color: #444; display: flex; align-items: center; gap: 0.8rem; }
        .account-info a { color: #2b2b33; font-weight: bold; text-decoration: none; }
        .account-info a:hover { text-decoration: underline; }
        .role-badge {
            background: #2b2b33; color: white; padding: 0.15rem 0.5rem;
            border-radius: 10px; font-size: 0.72rem; text-transform: uppercase;
        }
        .legend { display: flex; gap: 1.2rem; margin: 1rem 0; flex-wrap: wrap; }
        .legend-item { display: flex; align-items: center; gap: 0.4rem; font-size: 0.78rem; color: #444; }
        .legend-swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; flex-shrink: 0; }

        .table-wrap { width: 100%; max-width: 100%; }
        table {
            width: 100%; max-width: 100%; table-layout: fixed; border-collapse: collapse;
            background: white; box-shadow: 0 2px 8px rgba(0,0,0,0.06); font-size: clamp(0.75rem, 1.1vw, 1.05rem);
        }
        th, td {
            border-bottom: 1px solid #eee; padding: clamp(0.5rem, 0.9vw, 1.1rem) clamp(0.4rem, 0.6vw, 0.7rem);
            vertical-align: top; overflow-wrap: break-word; word-break: break-word;
        }
        th { background: #2b2b33; color: white; text-align: left; font-size: clamp(0.75rem, 1.1vw, 1.05rem); }
        .blank-cell { color: #ccc; font-size: clamp(0.95rem, 1.3vw, 1.3rem); }

        .shift-pill {
            position: relative;
            display: block;
            padding: clamp(0.35rem, 0.6vw, 0.7rem);
            border-radius: 5px;
            color: white;
            font-size: clamp(0.72rem, 1vw, 1rem);
            line-height: 1.35;
            overflow: hidden;
        }
        .shift-pill .time { position: relative; z-index: 1; display: block; font-weight: bold; }
        .shift-pill .problem { position: relative; z-index: 1; display: block; font-size: clamp(0.65rem, 0.88vw, 0.88rem); opacity: 0.92; }
        .shift-pill .importance { position: relative; z-index: 1; display: block; font-size: clamp(0.62rem, 0.84vw, 0.84rem); opacity: 0.85; margin-top: 0.15rem; }
        .shift-pill .sector { position: relative; z-index: 1; display: block; font-size: clamp(0.62rem, 0.84vw, 0.84rem); opacity: 0.85; margin-top: 0.15rem; font-weight: bold; }
        .shift-pill .paired-tag {
            position: relative; z-index: 1;
            display: block; font-size: clamp(0.68rem, 0.92vw, 0.92rem); font-weight: bold;
            background: rgba(255,255,255,0.25); border-radius: 3px; padding: 0.12rem 0.35rem;
            margin-bottom: 0.3rem; width: fit-content;
        }
        .emergency-emoji {
            position: absolute; inset: 0;
            display: flex; align-items: center; justify-content: center;
            font-size: clamp(1.7rem, 3.6vw, 2.8rem);
            opacity: 0.32;
            z-index: 0;
            pointer-events: none;
        }

        .whoami { margin: 1rem 0; font-size: clamp(0.9rem, 1.1vw, 1.1rem); }
        .whoami .name { font-weight: bold; }
        .whoami .role-title { color: #777; font-size: 0.85rem; }
        .view-only-note { color: #777; font-size: 0.85rem; margin-top: 1.5rem; font-style: italic; }
    </style>
</head>
<body>
    <div class="container">
        <div class="topbar">
            <div>
                <h1>My Shift Schedule</h1>
                <p class="subtitle">This shows only your own shifts for the week, across every sector.</p>
            </div>
            <div class="account-info">
                Signed in as <strong>{{ username }}</strong>
                <span class="role-badge">{{ role }}</span>
                <a href="{{ url_for('logout') }}">Log out</a>
            </div>
        </div>

        <div class="whoami">
            <span class="name">{{ person.name }}</span> &mdash;
            <span class="role-title">{{ person.role }}</span>
        </div>

        <div class="legend">
            {% for problem, color in problem_colors.items() %}
            <div class="legend-item">
                <span class="legend-swatch" style="background:{{ color }}"></span>
                {{ problem }}
            </div>
            {% endfor %}
        </div>

        <div class="table-wrap">
        <table>
            <tr>
                {% for day in days %}
                <th>{{ day }}</th>
                {% endfor %}
            </tr>
            {% for row_index in range(max_rows) %}
            <tr>
                {% for day in days %}
                <td>
                    {% if day in week and row_index < week[day]|length %}
                        {% set s = week[day][row_index] %}
                        <span class="shift-pill" style="background:{{ s.color }}">
                            {% if s.emergency %}
                            <span class="emergency-emoji" title="{{ s.emergency }}">{{ emergency_emojis.get(s.emergency, '') }}</span>
                            {% endif %}
                            {% if s.paired_with %}
                            <span class="paired-tag">{{ ([person.name] + s.paired_with)|join(' & ') }}</span>
                            {% endif %}
                            <span class="time">{{ s.time_range }}</span>
                            <span class="problem">{{ s.problem }}</span>
                            <span class="importance">Importance: {{ s.importance }}/10</span>
                            <span class="sector">{{ s.sector }}</span>
                        </span>
                    {% else %}
                        <span class="blank-cell">&mdash;</span>
                    {% endif %}
                </td>
                {% endfor %}
            </tr>
            {% endfor %}
        </table>
        </div>

        <p class="view-only-note">This is a read-only view of your schedule.</p>
    </div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = USERS.get(username)
        if user and user["password"] == password:
            session["username"] = username
            session["role"] = user["role"]
            if user["role"] == "worker":
                session["staff_name"] = user["staff_name"]
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    data = load_data()
    role = session.get("role")
    username = session.get("username")

    if role == "worker":
        staff_name = session.get("staff_name")
        person = next((p for p in STAFF if p["name"] == staff_name), None)
        week = get_personal_week(data, staff_name)
        max_rows = max([len(shifts) for shifts in week.values()], default=1) or 1
        return render_template_string(
            WORKER_TEMPLATE,
            days=DAYS,
            person=person,
            week=week,
            max_rows=max_rows,
            problem_colors=PROBLEM_COLORS,
            emergency_emojis=EMERGENCY_EMOJIS,
            username=username,
            role=role,
        )

    # supervisor view
    current_sector = request.args.get("sector", SECTORS[0])
    if current_sector not in SECTORS:
        current_sector = SECTORS[0]

    error = request.args.get("error")
    info = request.args.get("info")

    shifts_display = get_sector_week(data, current_sector)
    max_rows = max([len(v) for v in shifts_display.values()], default=1) or 1

    return render_template_string(
        PAGE_TEMPLATE,
        days=DAYS,
        staff=STAFF,
        shifts=shifts_display,
        max_rows=max_rows,
        problems=PROBLEMS,
        problem_colors=PROBLEM_COLORS,
        sectors=SECTORS,
        current_sector=current_sector,
        error=error,
        info=info,
        time_slots=TIME_SLOTS,
        username=username,
        role=role,
        emergency_types=EMERGENCY_TYPES,
        emergency_emojis=EMERGENCY_EMOJIS,
        active_shifts=list_active_shifts(data),
    )


@app.route("/report_problem", methods=["POST"])
@supervisor_required
def report_problem():
    data = load_data()
    requested_sector = request.form["sector"]
    problem = request.form["problem"]
    importance_raw = request.form["importance"]

    if requested_sector not in SECTORS:
        return "Invalid sector", 400
    if problem not in PROBLEM_COLORS:
        return "Invalid problem category", 400
    try:
        importance = int(importance_raw)
        if not (1 <= importance <= 10):
            raise ValueError
    except ValueError:
        return "Importance must be a whole number from 1 to 10", 400

    assignment, error = auto_schedule(data, requested_sector, problem, importance)

    if error:
        return redirect(url_for("index", sector=requested_sector, error=error))

    assigned_sector = assignment["sector"]
    key = f"{assignment['staff_name']}|{assignment['day']}"
    data[assigned_sector][key] = {
        "start": assignment["start"],
        "end": assignment["end"],
        "problem": assignment["problem"],
        "importance": assignment["importance"],
        "reported_sector": requested_sector,
    }
    save_data(data)
    return redirect(url_for("index", sector=assigned_sector))


@app.route("/delete_shift", methods=["POST"])
@supervisor_required
def delete_shift():
    data = load_data()

    sector = request.form["sector"]
    staff_name = request.form["staff_name"]
    day = request.form["day"]
    slot = request.form.get("slot", "main")

    if sector not in SECTORS:
        return "Invalid sector", 400

    key = make_shift_key(staff_name, day, slot)
    deleted_shift = data[sector].pop(key, None)

    if deleted_shift:
        paired_with = normalize_paired_with(deleted_shift.get("paired_with"))
        if paired_with:
            remove_pairing_references(data, sector, day, staff_name, paired_with)

    save_data(data)
    return redirect(url_for("index", sector=sector))


@app.route("/delete_all_shifts", methods=["POST"])
@supervisor_required
def delete_all_shifts():
    sector = request.form.get("sector", SECTORS[0])
    if sector not in SECTORS:
        sector = SECTORS[0]

    # Wipe every sector's shifts completely (not just the one being viewed).
    data = {s: {} for s in SECTORS}
    save_data(data)

    return redirect(url_for("index", sector=sector, info="All shifts have been cleared from every sector."))


@app.route("/auto_pair", methods=["POST"])
@supervisor_required
def auto_pair():
    data = load_data()

    sector = request.form.get("sector", SECTORS[0])
    if sector not in SECTORS:
        sector = SECTORS[0]

    paired, unpaired = auto_pair_week(data)
    save_data(data)

    if paired:
        details = "; ".join(
            f"{p['staff_name']} + {p['partner_name']} ({p['day']}, {p['sector']}, {p['time_range']})"
            for p in paired
        )
        info = f"Paired for the week: {details}."
    else:
        info = "No new pairings made - nobody without a shift had an eligible same-role partner working that day."

    if unpaired:
        names = ", ".join(f"{u['name']} ({u['day']})" for u in unpaired)
        info += f" Could not pair: {names}."

    return redirect(url_for("index", sector=sector, info=info))


@app.route("/emergency", methods=["POST"])
@supervisor_required
def emergency():
    data = load_data()

    sector_for_redirect = request.form.get("sector", SECTORS[0])
    if sector_for_redirect not in SECTORS:
        sector_for_redirect = SECTORS[0]

    emergency_type = request.form.get("emergency_type")
    shift_id = request.form.get("shift_id", "")

    if emergency_type not in EMERGENCY_EMOJIS:
        return redirect(url_for("index", sector=sector_for_redirect, error="Invalid emergency type."))

    try:
        sector, staff_name, day, slot = shift_id.split("||")
    except ValueError:
        return redirect(url_for("index", sector=sector_for_redirect, error="Invalid shift selected."))

    if sector not in SECTORS:
        return redirect(url_for("index", sector=sector_for_redirect, error="Invalid sector."))

    key = make_shift_key(staff_name, day, slot)
    if key not in data[sector]:
        return redirect(url_for(
            "index", sector=sector_for_redirect,
            error="That shift no longer exists (it may have just been deleted)."
        ))

    # Flag the shift itself.
    data[sector][key]["emergency"] = emergency_type
    message = f"{EMERGENCY_EMOJIS.get(emergency_type, '')} Flagged {staff_name}'s shift ({day}, {sector}) as {emergency_type}."

    # A flag always carries over to whoever this shift is paired with, so
    # both people's shift cells show it - e.g. if Emily and Jason are
    # paired and Emily's shift gets flagged, Jason's shift is flagged too.
    paired_with = normalize_paired_with(data[sector][key].get("paired_with"))
    flagged_partners = []
    for partner_name in paired_with:
        partner_key = make_shift_key(partner_name, day, "main")
        if partner_key in data[sector]:
            data[sector][partner_key]["emergency"] = emergency_type
            flagged_partners.append(partner_name)
    if flagged_partners:
        message += f" Also flagged {', '.join(flagged_partners)}'s paired shift."

    # "Add More Time" is the only type that also creates a brand-new extra
    # shift right after this one - for this person only (never the
    # partner), and the original shift is left completely untouched.
    if emergency_type == "Add More Time":
        _, extra_message = try_add_extra_shift(data, sector, staff_name, day, slot)
        message += f" {extra_message}"

    save_data(data)
    return redirect(url_for("index", sector=sector, info=message))


if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        save_data({sector: {} for sector in SECTORS})
    app.run(host="127.0.0.1", port=8000, debug=True)