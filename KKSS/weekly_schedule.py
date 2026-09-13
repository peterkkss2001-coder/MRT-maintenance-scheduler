"""
weekly_schedule.py

Tracks maintenance assignments across the week at DAY + TIME SLOT + SECTION
granularity, so two jobs that overlap in actual clock time can never share
an employee — even if they're in different sections.

CONFIGURATION (edit these to match your real setup):
    WORKING_WINDOW_START / END — the daily maintenance window (currently a
        late-night engineering window, after trains stop running).
    NUM_SLOTS_PER_DAY — how many slots that window is evenly divided into.
    SECTIONS — the work zones/locations admin can assign a job to. This is
        a placeholder list ("Section 1".."Section 5") — replace with your
        real section/sector/platform names.
    MAX_EMPLOYEES_PER_PROBLEM — hard cap on team size for a single job.

Slots are generated once from the working window (see generate_daily_slots)
and given letter labels (A, B, C...). Conflict checking compares actual
start/end times, not just slot labels — so it stays correct even if slots
are ever made uneven, or a future job is allowed to span more than one slot.

The schedule is a plain list of dicts, one per train job:
    {
        "train_number": "215",
        "problem_statement": "Electrical, Signalling & Equipment",
        "problem_rating": 8,
        "importance_level": 9,
        "day": "Monday",
        "section": "Section 2",
        "slot_label": "B",
        "slot_start": "01:30",
        "slot_end": "02:30",
        "assigned_employees": [
            {"employee_id": "EMP005", "employee_name": "William Tan",
             "job_role": "Electrical Engineer", "experience_level": "Senior",
             "reason": "..."},
            ...  (up to MAX_EMPLOYEES_PER_PROBLEM)
        ]
    }
"""

from __future__ import annotations

import csv
import json
import os
import string
from typing import List, Optional


class WeeklyScheduleError(Exception):
    """Raised for invalid weekly schedule operations."""


# --------------------------------------------------------------------------
# Configuration — edit to match your real schedule.
# --------------------------------------------------------------------------
DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Placeholder section list — rename these to your actual work zones/sectors.
SECTIONS = ["Section 1", "Section 2", "Section 3", "Section 4", "Section 5"]

WORKING_WINDOW_START = "00:30"  # late-night engineering window, after service hours
WORKING_WINDOW_END = "05:30"
# 3 slots over this 5-hour window gives each slot ~1h40m — closer to a
# realistic single MRT maintenance job duration than the previous 5-slot
# split (1h each, often too short for anything beyond a quick inspection).
# Adjust this (or the window above) if your actual average job duration
# differs — the split stays even automatically either way.
NUM_SLOTS_PER_DAY = 3

MAX_EMPLOYEES_PER_PROBLEM = 5

CSV_FIELDNAMES = [
    "train_number",
    "problem_statement",
    "problem_rating",
    "importance_level",
    "day",
    "section",
    "slot_label",
    "start_time",
    "end_time",
    "employee_id",
    "employee_name",
    "job_role",
    "experience_level",
    "reason",
]


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _hhmm_to_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' to minutes since midnight."""
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def _minutes_to_hhmm(minutes: int) -> str:
    """Convert minutes since midnight back to 'HH:MM'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def times_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    """
    True if time range [start1, end1) overlaps [start2, end2), comparing
    actual clock times rather than slot labels. This is what makes the
    conflict check correct even for uneven or overlapping slot definitions.
    """
    s1, e1 = _hhmm_to_minutes(start1), _hhmm_to_minutes(end1)
    s2, e2 = _hhmm_to_minutes(start2), _hhmm_to_minutes(end2)
    return s1 < e2 and s2 < e1


def generate_daily_slots(
    start_time: str = WORKING_WINDOW_START,
    end_time: str = WORKING_WINDOW_END,
    num_slots: int = NUM_SLOTS_PER_DAY,
) -> List[dict]:
    """
    Evenly divide the working window into `num_slots` labeled slots.

    Returns:
        [{"label": "A", "start": "00:30", "end": "01:30"}, ...]
    """
    start_min = _hhmm_to_minutes(start_time)
    end_min = _hhmm_to_minutes(end_time)
    total = end_min - start_min
    if total <= 0:
        raise WeeklyScheduleError(f"end_time ({end_time}) must be after start_time ({start_time}).")
    if num_slots < 1:
        raise WeeklyScheduleError("num_slots must be at least 1.")

    boundaries = [start_min + round(i * total / num_slots) for i in range(num_slots + 1)]
    boundaries[-1] = end_min  # avoid rounding drift on the final boundary

    labels = string.ascii_uppercase
    if num_slots > len(labels):
        raise WeeklyScheduleError(f"num_slots ({num_slots}) exceeds available letter labels (26).")

    return [
        {"label": labels[i], "start": _minutes_to_hhmm(boundaries[i]), "end": _minutes_to_hhmm(boundaries[i + 1])}
        for i in range(num_slots)
    ]


# --------------------------------------------------------------------------
# Schedule operations
# --------------------------------------------------------------------------

def create_empty_schedule() -> List[dict]:
    """Start a fresh, empty weekly schedule."""
    return []


def add_assignment(
    schedule: List[dict],
    train_number: str,
    problem_statement: str,
    problem_rating: int,
    importance_level: int,
    day: str,
    section: str,
    slot_label: str,
    slot_start: str,
    slot_end: str,
    assigned_employees: List[dict],
) -> None:
    """
    Record a finalized (admin-approved) assignment in the weekly schedule.

    Raises:
        WeeklyScheduleError: on missing required fields, an empty employee
            list, or more than MAX_EMPLOYEES_PER_PROBLEM employees.
    """
    if not train_number or not str(train_number).strip():
        raise WeeklyScheduleError("train_number cannot be empty.")
    if not assigned_employees:
        raise WeeklyScheduleError("assigned_employees cannot be empty.")
    if len(assigned_employees) > MAX_EMPLOYEES_PER_PROBLEM:
        raise WeeklyScheduleError(
            f"Cannot assign more than {MAX_EMPLOYEES_PER_PROBLEM} employees to a single problem "
            f"(got {len(assigned_employees)})."
        )
    if day not in DAYS_OF_WEEK:
        raise WeeklyScheduleError(f"day must be one of {DAYS_OF_WEEK}, got: {day!r}")

    schedule.append(
        {
            "train_number": str(train_number).strip(),
            "problem_statement": str(problem_statement).strip(),
            "problem_rating": problem_rating,
            "importance_level": importance_level,
            "day": day,
            "section": section,
            "slot_label": slot_label,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "assigned_employees": assigned_employees,
        }
    )


def get_conflicting_employee_names(schedule: List[dict], day: str, slot_start: str, slot_end: str) -> List[str]:
    """
    Return employee names already assigned to a job on `day` whose time
    overlaps [slot_start, slot_end). Kept for reference — the actual
    employee-eligibility filter now uses get_employees_assigned_on_day()
    below (one job per employee per day, not just no literal overlap).
    """
    names = set()
    for entry in schedule:
        if entry.get("day") != day:
            continue
        if times_overlap(slot_start, slot_end, entry["slot_start"], entry["slot_end"]):
            for emp in entry.get("assigned_employees", []):
                name = emp.get("employee_name")
                if name:
                    names.add(name)
    return sorted(names)


def get_employees_assigned_on_day(schedule: List[dict], day: str) -> List[str]:
    """
    Return every employee name assigned to ANY job on `day`, regardless of
    section or slot. This is the actual conflict source used when building
    a candidate pool: employees are limited to ONE job per day. A/B/C slots
    are back-to-back with zero gap between them, so allowing the same
    person to fill multiple non-overlapping slots in different sections
    would mean them working the entire overnight window with no rest and
    no travel time between physically separate work sites — unrealistic,
    even though it technically passes a strict time-overlap check.
    """
    names = set()
    for entry in schedule:
        if entry.get("day") != day:
            continue
        for emp in entry.get("assigned_employees", []):
            name = emp.get("employee_name")
            if name:
                names.add(name)
    return sorted(names)


def get_assigned_employee_names(schedule: List[dict]) -> List[str]:
    """Return every employee name assigned to ANY job this week, regardless of time. Kept for reference/reporting."""
    names = set()
    for entry in schedule:
        for emp in entry.get("assigned_employees", []):
            name = emp.get("employee_name")
            if name:
                names.add(name)
    return sorted(names)


def is_section_occupied(schedule: List[dict], day: str, section: str, slot_start: str, slot_end: str) -> bool:
    """
    True if `section` already has a job on `day` whose time overlaps
    [slot_start, slot_end) — a physical location can't host two different
    maintenance jobs at once, even with entirely different employees.
    Applies to both auto-assigned and manually-entered jobs.
    """
    for entry in schedule:
        if entry.get("day") != day or entry.get("section") != section:
            continue
        if times_overlap(slot_start, slot_end, entry["slot_start"], entry["slot_end"]):
            return True
    return False


def save_schedule_to_csv(schedule: List[dict], path: str) -> None:
    """Flatten and write the weekly schedule to a CSV file (one row per assigned employee)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for entry in schedule:
            for emp in entry.get("assigned_employees", []):
                writer.writerow(
                    {
                        "train_number": entry["train_number"],
                        "problem_statement": entry["problem_statement"],
                        "problem_rating": entry["problem_rating"],
                        "importance_level": entry["importance_level"],
                        "day": entry.get("day", ""),
                        "section": entry.get("section", ""),
                        "slot_label": entry.get("slot_label", ""),
                        "start_time": entry.get("slot_start", ""),
                        "end_time": entry.get("slot_end", ""),
                        "employee_id": emp.get("employee_id", ""),
                        "employee_name": emp.get("employee_name", ""),
                        "job_role": emp.get("job_role", ""),
                        "experience_level": emp.get("experience_level", ""),
                        "reason": emp.get("reason", ""),
                    }
                )


def save_schedule_to_json(schedule: List[dict], path: str) -> None:
    """
    Write the weekly schedule to a JSON file, preserving the full nested
    structure (each job's day/section/slot plus its whole team of employees
    in one object) — unlike the CSV, which flattens to one row per employee.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)


def get_next_numbered_path(directory: str, base_name: str = "weekly_schedule_output", ext: str = ".csv") -> str:
    """Find the next available numbered filename in `directory`."""
    os.makedirs(directory, exist_ok=True)
    n = 1
    while True:
        candidate = os.path.join(directory, f"{base_name}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def save_schedule_auto_numbered(
    schedule: List[dict],
    directory: str,
    base_name: str = "weekly_schedule_output",
    max_attempts: int = 20,
) -> tuple:
    """
    Save the schedule as BOTH a numbered CSV and a numbered JSON file,
    sharing the same number (e.g. weekly_schedule_output_3.csv AND
    weekly_schedule_output_3.json), skipping ahead if either filename
    turns out to be locked (e.g. open in Excel) rather than failing the run.

    Returns:
        (csv_path, json_path) — the paths actually written to.

    Raises:
        WeeklyScheduleError: if no writable numbered pair is found within
            max_attempts tries.
    """
    os.makedirs(directory, exist_ok=True)
    n = 1
    attempts = 0
    while attempts < max_attempts:
        csv_candidate = os.path.join(directory, f"{base_name}_{n}.csv")
        json_candidate = os.path.join(directory, f"{base_name}_{n}.json")
        if os.path.exists(csv_candidate) or os.path.exists(json_candidate):
            n += 1
            continue
        try:
            save_schedule_to_csv(schedule, csv_candidate)
            save_schedule_to_json(schedule, json_candidate)
            return csv_candidate, json_candidate
        except PermissionError:
            n += 1
            attempts += 1
    raise WeeklyScheduleError(
        f"Could not find a writable numbered output file in '{directory}' "
        f"after {max_attempts} attempts. Close any open '{base_name}_*' files and try again."
    )


def load_schedule_from_csv(path: str) -> List[dict]:
    """Load a previously saved weekly schedule CSV back into the in-memory schedule format."""
    if not os.path.exists(path):
        return create_empty_schedule()

    grouped: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["train_number"], row.get("day", ""), row.get("slot_label", ""))
            if key not in grouped:
                grouped[key] = {
                    "train_number": row["train_number"],
                    "problem_statement": row["problem_statement"],
                    "problem_rating": int(row["problem_rating"]) if row["problem_rating"] else None,
                    "importance_level": int(row["importance_level"]) if row["importance_level"] else None,
                    "day": row.get("day", ""),
                    "section": row.get("section", ""),
                    "slot_label": row.get("slot_label", ""),
                    "slot_start": row.get("start_time", ""),
                    "slot_end": row.get("end_time", ""),
                    "assigned_employees": [],
                }
            grouped[key]["assigned_employees"].append(
                {
                    "employee_id": row["employee_id"],
                    "employee_name": row["employee_name"],
                    "job_role": row["job_role"],
                    "experience_level": row["experience_level"],
                    "reason": row["reason"],
                }
            )
    return list(grouped.values())


if __name__ == "__main__":
    # Manual smoke test.
    slots = generate_daily_slots()
    print("Generated slots:", slots)

    # Verify overlap detection against the exact example from the spec:
    # Section 1 Slot B 1:30-3:00 vs Section 2 Slot C 2:00-3:30 -> should conflict.
    assert times_overlap("01:30", "03:00", "02:00", "03:30") is True
    # Two back-to-back, non-overlapping slots -> should NOT conflict.
    assert times_overlap("00:30", "01:30", "01:30", "02:30") is False
    print("Overlap detection verified against spec example.")

    schedule = create_empty_schedule()
    add_assignment(
        schedule, train_number="215", problem_statement="Electrical, Signalling & Equipment",
        problem_rating=8, importance_level=9, day="Monday", section="Section 1",
        slot_label="B", slot_start="01:30", slot_end="02:30",
        assigned_employees=[{"employee_id": "EMP005", "employee_name": "William Tan"}],
    )
    conflicts = get_conflicting_employee_names(schedule, "Monday", "02:00", "03:00")
    print("Conflicts for Monday 02:00-03:00 (should include William Tan):", conflicts)