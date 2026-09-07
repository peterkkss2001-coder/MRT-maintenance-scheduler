"""
Auto-scheduling, auto-pairing, and time/formatting helper functions for
the shift schedule app. Everything here is pure logic - no Flask, no
routes - so it can be tested or reused on its own.
"""

import os
from datetime import datetime, timedelta

from data import (
    SECTORS, DAYS, STAFF, PROBLEM_COLORS, ROLE_FOR_PROBLEM,
    CANDIDATE_STARTS, SHIFT_DURATION_MIN, WINDOW_END, EMERGENCY_EXTEND_MIN,
    SCHEDULE_START_DATE,
)


# ---------------------------------------------------------------------------
# Time / key formatting helpers
# ---------------------------------------------------------------------------
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
