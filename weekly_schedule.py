"""
weekly_schedule.py

Tracks maintenance assignments across the week so that when a new train
problem is scheduled, employees already committed to another train this
week are treated as conflicts and excluded from the candidate pool.

ASSUMPTION: the employee dataset has no day/time fields, so conflicts are
tracked at "already assigned somewhere this week" granularity, not
hour-level overlap. If a real schedule window (day/time per request) gets
added later, this module is the place to make the conflict check more
precise (e.g. only conflict if the time windows actually overlap).

The schedule is a plain list of dicts, one per train job:
    {
        "train_number": "215",
        "problem_statement": "Repair the electrical system of Train 215",
        "problem_rating": 8,
        "importance_level": 9,
        "assigned_employees": [
            {"employee_id": "EMP005", "employee_name": "William Tan",
             "job_role": "Electrical Engineer", "experience_level": "Senior",
             "reason": "..."},
            ...
        ]
    }

It can be persisted to / loaded from a CSV so the schedule survives
between separate runs of main.py during the week.
"""

from __future__ import annotations

import csv
import os
from typing import List, Optional


class WeeklyScheduleError(Exception):
    """Raised for invalid weekly schedule operations."""


CSV_FIELDNAMES = [
    "train_number",
    "problem_statement",
    "problem_rating",
    "importance_level",
    "employee_id",
    "employee_name",
    "job_role",
    "experience_level",
    "reason",
]


def create_empty_schedule() -> List[dict]:
    """Start a fresh, empty weekly schedule."""
    return []


def add_assignment(
    schedule: List[dict],
    train_number: str,
    problem_statement: str,
    problem_rating: int,
    importance_level: int,
    assigned_employees: List[dict],
) -> None:
    """
    Record a finalized (admin-approved) assignment in the weekly schedule.

    Args:
        schedule: the running list of assignments (mutated in place).
        train_number: e.g. "215".
        problem_statement: the maintenance problem text.
        problem_rating: 1-10 complexity rating used for this job.
        importance_level: 1-10 priority/importance rating used for this job.
        assigned_employees: list of employee dicts, each expected to have at
            least "employee_id" and "employee_name" (the shape returned by
            llm_client.get_employee_recommendation()'s "recommended_employees").
    """
    if not train_number or not str(train_number).strip():
        raise WeeklyScheduleError("train_number cannot be empty.")
    if not assigned_employees:
        raise WeeklyScheduleError("assigned_employees cannot be empty.")

    schedule.append(
        {
            "train_number": str(train_number).strip(),
            "problem_statement": str(problem_statement).strip(),
            "problem_rating": problem_rating,
            "importance_level": importance_level,
            "assigned_employees": assigned_employees,
        }
    )


def get_assigned_employee_names(schedule: List[dict]) -> List[str]:
    """Return the unique list of employee names already assigned to ANY job this week."""
    names = set()
    for entry in schedule:
        for emp in entry.get("assigned_employees", []):
            name = emp.get("employee_name")
            if name:
                names.add(name)
    return sorted(names)


def find_conflicts_for_employee(schedule: List[dict], employee_name: str) -> List[dict]:
    """
    Return the list of schedule entries (train jobs) that employee_name is
    already assigned to this week. Empty list = no conflict.
    """
    matches = []
    for entry in schedule:
        assigned_names = {emp.get("employee_name") for emp in entry.get("assigned_employees", [])}
        if employee_name in assigned_names:
            matches.append(entry)
    return matches


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
                        "employee_id": emp.get("employee_id", ""),
                        "employee_name": emp.get("employee_name", ""),
                        "job_role": emp.get("job_role", ""),
                        "experience_level": emp.get("experience_level", ""),
                        "reason": emp.get("reason", ""),
                    }
                )


def get_next_numbered_path(directory: str, base_name: str = "weekly_schedule_output", ext: str = ".csv") -> str:
    """
    Find the next available numbered filename in `directory`, e.g.
    weekly_schedule_output_1.csv, weekly_schedule_output_2.csv, ...

    This avoids ever writing to a filename that might already be open in
    another program (a common cause of PermissionError on Windows, e.g. the
    previous output CSV still open in Excel).

    Creates `directory` if it doesn't exist yet.
    """
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
    ext: str = ".csv",
    max_attempts: int = 20,
) -> str:
    """
    Save the schedule to a numbered CSV file, automatically skipping ahead
    to the next number if the chosen filename turns out to be locked
    (e.g. currently open in Excel) rather than failing the whole run.

    Returns:
        The path that was actually written to.

    Raises:
        WeeklyScheduleError: if no writable numbered filename is found
            within max_attempts tries.
    """
    os.makedirs(directory, exist_ok=True)
    n = 1
    attempts = 0
    while attempts < max_attempts:
        candidate = os.path.join(directory, f"{base_name}_{n}{ext}")
        if os.path.exists(candidate):
            n += 1
            continue
        try:
            save_schedule_to_csv(schedule, candidate)
            return candidate
        except PermissionError:
            # File got created/locked between our check and the write
            # (or is otherwise inaccessible) -- try the next number.
            n += 1
            attempts += 1
    raise WeeklyScheduleError(
        f"Could not find a writable numbered output file in '{directory}' "
        f"after {max_attempts} attempts. Close any open '{base_name}_*.csv' "
        f"files and try again."
    )


def load_schedule_from_csv(path: str) -> List[dict]:
    """
    Load a previously saved weekly schedule CSV back into the in-memory
    schedule format. Returns an empty schedule if the file doesn't exist yet
    (first run of the week).
    """
    if not os.path.exists(path):
        return create_empty_schedule()

    grouped: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row["train_number"]
            if key not in grouped:
                grouped[key] = {
                    "train_number": row["train_number"],
                    "problem_statement": row["problem_statement"],
                    "problem_rating": int(row["problem_rating"]) if row["problem_rating"] else None,
                    "importance_level": int(row["importance_level"]) if row["importance_level"] else None,
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


def get_next_available_output_path(directory: str, base_name: str = "weekly_schedule_output") -> str:
    """
    Build a numbered output path inside `directory` that doesn't already
    exist on disk: weekly_schedule_output_1.csv, _2.csv, _3.csv, ...

    This avoids PermissionError/overwrite issues when a previous output
    file is still open in Excel or otherwise locked — each run gets its
    own numbered file instead of trying to overwrite the last one.

    Creates `directory` if it doesn't exist yet.
    """
    os.makedirs(directory, exist_ok=True)

    counter = 1
    while True:
        candidate = os.path.join(directory, f"{base_name}_{counter}.csv")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


if __name__ == "__main__":
    # Simple manual smoke test.
    schedule = create_empty_schedule()
    add_assignment(
        schedule,
        train_number="215",
        problem_statement="Repair electrical system",
        problem_rating=8,
        importance_level=9,
        assigned_employees=[{"employee_id": "EMP005", "employee_name": "William Tan"}],
    )
    add_assignment(
        schedule,
        train_number="220",
        problem_statement="Track inspection",
        problem_rating=4,
        importance_level=5,
        assigned_employees=[{"employee_id": "EMP001", "employee_name": "Alex Tan"}],
    )

    print("Assigned this week:", get_assigned_employee_names(schedule))
    print("Conflicts for William Tan:", find_conflicts_for_employee(schedule, "William Tan"))
    print("Conflicts for Sarah Lim:", find_conflicts_for_employee(schedule, "Sarah Lim"))

    save_schedule_to_csv(schedule, "test_weekly_schedule.csv")
    reloaded = load_schedule_from_csv("test_weekly_schedule.csv")
    print("Reloaded assigned names:", get_assigned_employee_names(reloaded))