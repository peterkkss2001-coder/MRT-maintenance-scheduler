"""
employee_recommendation_prompt.py

Builds the prompt sent to the LLM to recommend a team of employees
(up to weekly_schedule.MAX_EMPLOYEES_PER_PROBLEM) for a maintenance problem
at a specific DAY + SECTION + TIME SLOT.

Flow this module supports:
    Admin enters train number + problem category (1/2/3) + complexity (1-10)
    + importance (1-10) + day + section + slot
        -> problem category determines the allowed Job_Role family
        -> complexity rating maps to a required experience level
        -> day determines who's already committed to another job that same
           day (in any section) — employees get one job per day, not just
           no literal time overlap — see weekly_schedule.get_employees_assigned_on_day
        -> a structured prompt is built asking for up to MAX_EMPLOYEES_PER_PROBLEM
           qualified, available, non-conflicting employees
        -> main.py / app.py sends the prompt to the LLM and parses the result
        -> once approved, the assignment is recorded in the weekly schedule

This module also supports building a REPLACEMENT prompt for swapping out a
single team member (see build_replacement_prompt), used by the "AI
suggest a replacement" feature on the review page.

This module does NOT call the LLM itself — it only builds prompts.
"""

from __future__ import annotations

import json
from typing import List, Optional, Union

import pandas as pd

from data_loader import (
    EmployeeDatasetError,
    exclude_employees,
    get_available_employees,
)
from weekly_schedule import (
    get_employees_assigned_on_day,
    is_section_occupied,
    generate_daily_slots,
    DAYS_OF_WEEK,
    SECTIONS,
    MAX_EMPLOYEES_PER_PROBLEM,
)

# --------------------------------------------------------------------------
# Problem statement categories -> allowed Job_Role family
# --------------------------------------------------------------------------
PROBLEM_STATEMENT_OPTIONS = {
    1: {
        "label": "Track & Infrastructure",
        "roles": ["Track Engineer", "Track Technician"],
    },
    2: {
        "label": "Electrical, Signalling & Equipment",
        "roles": ["Electrical Engineer", "Electrical Technician"],
    },
    3: {
        "label": "Operations & Safety",
        "roles": ["Safety Officer", "Safety Inspector"],
    },
}

EXPERIENCE_LEVELS_ORDER = ["Junior", "Intermediate", "Senior"]

RATING_MIN = 1
RATING_MAX = 10
IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 10


class PromptBuildError(Exception):
    """Raised when the recommendation prompt can't be built (bad input, no candidates, etc.)."""


def _validate_rating(value: int, min_val: int, max_val: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PromptBuildError(f"{field_name} must be an integer, got: {value!r}")
    if value < min_val or value > max_val:
        raise PromptBuildError(f"{field_name} must be between {min_val} and {max_val}, got: {value}")
    return value


def _validate_day(day: str) -> str:
    if day not in DAYS_OF_WEEK:
        raise PromptBuildError(f"day must be one of {DAYS_OF_WEEK}, got: {day!r}")
    return day


def _validate_section(section: str) -> str:
    if section not in SECTIONS:
        raise PromptBuildError(f"section must be one of {SECTIONS}, got: {section!r}")
    return section


def resolve_problem_statement(problem_statement: Union[int, str]) -> dict:
    """Resolve a problem statement choice (1/2/3, numeric string, or exact label) into its category info."""
    try:
        choice_num = int(problem_statement)
        if choice_num in PROBLEM_STATEMENT_OPTIONS:
            return PROBLEM_STATEMENT_OPTIONS[choice_num]
    except (ValueError, TypeError):
        pass

    if isinstance(problem_statement, str):
        for info in PROBLEM_STATEMENT_OPTIONS.values():
            if info["label"].lower() == problem_statement.strip().lower():
                return info

    valid_options = ", ".join(f"{k}={v['label']}" for k, v in PROBLEM_STATEMENT_OPTIONS.items())
    raise PromptBuildError(
        f"problem_statement must be one of: {valid_options}. Got: {problem_statement!r}"
    )


def determine_required_level(problem_rating: int) -> str:
    """1-3 -> Junior, 4-7 -> Intermediate, 8-10 -> Senior."""
    _validate_rating(problem_rating, RATING_MIN, RATING_MAX, "problem_rating")
    if problem_rating <= 3:
        return "Junior"
    elif problem_rating <= 7:
        return "Intermediate"
    else:
        return "Senior"


def determine_priority_label(importance_level: int) -> str:
    """1-3 -> Low, 4-7 -> Medium, 8-10 -> Critical."""
    _validate_rating(importance_level, IMPORTANCE_MIN, IMPORTANCE_MAX, "importance_level")
    if importance_level <= 3:
        return "Low"
    elif importance_level <= 7:
        return "Medium"
    else:
        return "Critical"


def get_candidate_pool(
    employee_df: pd.DataFrame,
    required_level: str,
    allowed_roles: Optional[List[str]] = None,
    excluded_names: Optional[List[str]] = None,
    conflicting_names: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Narrow the full employee dataset down to a candidate pool:
      1. Only 'Available' employees.
      2. Only Job_Role values in allowed_roles (the category's role family).
      3. Excludes admin-specified names.
      4. Excludes anyone already assigned to another job the same day (one job per employee per day).
      5. Only employees at or above the required experience level.
    """
    if required_level not in EXPERIENCE_LEVELS_ORDER:
        raise PromptBuildError(f"required_level must be one of {EXPERIENCE_LEVELS_ORDER}, got: {required_level!r}")
    if "Experience_Level" not in employee_df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Experience_Level' column.")
    if "Job_Role" not in employee_df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Job_Role' column.")

    candidates = get_available_employees(employee_df)

    if allowed_roles:
        allowed_roles_lower = {r.lower() for r in allowed_roles}
        candidates = candidates[candidates["Job_Role"].str.lower().isin(allowed_roles_lower)].reset_index(drop=True)

    all_excluded = list(excluded_names or []) + list(conflicting_names or [])
    candidates = exclude_employees(candidates, all_excluded)

    min_rank = EXPERIENCE_LEVELS_ORDER.index(required_level)
    candidates = candidates[
        candidates["Experience_Level"].apply(
            lambda lvl: EXPERIENCE_LEVELS_ORDER.index(lvl) >= min_rank if lvl in EXPERIENCE_LEVELS_ORDER else False
        )
    ].reset_index(drop=True)

    return candidates


def build_employee_recommendation_prompt(
    train_number: str,
    problem_statement: Union[int, str],
    problem_rating: int,
    importance_level: int,
    employee_df: pd.DataFrame,
    day: str,
    section: str,
    slot_label: str,
    slot_start: str,
    slot_end: str,
    excluded_names: Optional[List[str]] = None,
    weekly_schedule: Optional[List[dict]] = None,
) -> str:
    """
    Build the full text prompt asking the LLM to recommend a team (up to
    MAX_EMPLOYEES_PER_PROBLEM) of qualified employees for a specific
    train job at a specific day/section/time slot.

    Raises:
        PromptBuildError: on invalid inputs, or an empty candidate pool.
    """
    if not train_number or not str(train_number).strip():
        raise PromptBuildError("train_number cannot be empty.")

    _validate_day(day)
    _validate_section(section)

    if weekly_schedule and is_section_occupied(weekly_schedule, day, section, slot_start, slot_end):
        raise PromptBuildError(
            f"{section} is already occupied on {day} during an overlapping time window "
            f"({slot_start}-{slot_end}) by another job. Choose a different day, section, or slot."
        )

    category_info = resolve_problem_statement(problem_statement)
    category_label = category_info["label"]
    allowed_roles = category_info["roles"]

    required_level = determine_required_level(problem_rating)
    priority_label = determine_priority_label(importance_level)

    conflicting_names = (
        get_employees_assigned_on_day(weekly_schedule, day) if weekly_schedule else []
    )

    candidates = get_candidate_pool(
        employee_df,
        required_level,
        allowed_roles=allowed_roles,
        excluded_names=excluded_names,
        conflicting_names=conflicting_names,
    )

    if candidates.empty:
        raise PromptBuildError(
            f"No available candidates found for category '{category_label}' (roles: {allowed_roles}) "
            f"at or above '{required_level}' level for Train {train_number} on {day} {slot_label} "
            f"({slot_start}-{slot_end}) (after applying exclusions: {excluded_names or 'none'}, "
            f"and same-day conflicts: {conflicting_names or 'none'})."
        )

    candidate_records = candidates.to_dict(orient="records")

    prompt = f"""You are an AI maintenance scheduling assistant. Recommend a team of qualified employees (as few as needed, never more than {MAX_EMPLOYEES_PER_PROBLEM}) for the maintenance problem below.

TRAIN NUMBER: {train_number}
DAY: {day}
SECTION: {section}
TIME SLOT: {slot_label} ({slot_start}-{slot_end})

PROBLEM STATEMENT (CATEGORY): {category_label}
ELIGIBLE JOB ROLES FOR THIS CATEGORY: {allowed_roles}

PROBLEM COMPLEXITY RATING: {problem_rating}/10
REQUIRED MINIMUM EXPERIENCE LEVEL: {required_level}

IMPORTANCE / PRIORITY RATING: {importance_level}/10 ({priority_label} priority)

EXCLUDED EMPLOYEES (admin-specified, must not be selected): {excluded_names if excluded_names else "None"}

EMPLOYEES EXCLUDED — ALREADY ASSIGNED TO ANOTHER JOB ON {day} (employees are limited to one job per day, regardless of section or slot): {conflicting_names if conflicting_names else "None"}

CANDIDATE EMPLOYEES (already filtered to available, correct role category, not excluded, not time-conflicted, and at or above the required experience level):
{json.dumps(candidate_records, indent=2)}

RULES:
1. Only select from the candidate employees listed above.
2. Do not select any employee whose experience level is below "{required_level}".
3. Do not select any employee outside the eligible job roles for this category: {allowed_roles}.
4. Recommend a team of {MAX_EMPLOYEES_PER_PROBLEM} or fewer — pick the smallest team that can safely handle a job of this complexity, not automatically the maximum.
5. Take the importance/priority rating into account: for higher-priority (Critical) jobs, prefer the most experienced qualified employees available; for lower-priority (Low) jobs, more junior-but-qualified employees can be preferred to preserve senior staff for more critical work.
6. Explain the reason for every recommended employee.
7. If the candidate pool is thin (e.g. few qualified employees, or strong options were excluded due to conflicts), mention this in "notes".

RESPONSE FORMAT (JSON only, no extra text):
{{
  "train_number": "{train_number}",
  "recommended_employees": [
    {{
      "employee_id": "...",
      "employee_name": "...",
      "job_role": "...",
      "experience_level": "...",
      "reason": "..."
    }}
  ],
  "notes": "any caveats or warnings, e.g. limited candidate pool, conflicts avoided"
}}
"""
    return prompt


def build_replacement_prompt(
    train_number: str,
    problem_statement: Union[int, str],
    problem_rating: int,
    importance_level: int,
    employee_df: pd.DataFrame,
    day: str,
    section: str,
    slot_label: str,
    slot_start: str,
    slot_end: str,
    employee_to_replace_name: str,
    current_team_names: List[str],
    excluded_names: Optional[List[str]] = None,
    weekly_schedule: Optional[List[dict]] = None,
) -> str:
    """
    Build a prompt asking the LLM for exactly ONE alternative employee to
    replace `employee_to_replace_name` on this job's team. The rest of the
    current team (current_team_names) is excluded from the candidate pool
    too, so the same person can't be suggested twice for one job.

    Raises:
        PromptBuildError: on invalid inputs, or an empty candidate pool
            (no qualified alternative available).
    """
    if not train_number or not str(train_number).strip():
        raise PromptBuildError("train_number cannot be empty.")

    _validate_day(day)
    _validate_section(section)

    category_info = resolve_problem_statement(problem_statement)
    category_label = category_info["label"]
    allowed_roles = category_info["roles"]

    required_level = determine_required_level(problem_rating)
    priority_label = determine_priority_label(importance_level)

    conflicting_names = (
        get_employees_assigned_on_day(weekly_schedule, day) if weekly_schedule else []
    )

    # Exclude everyone else already on this job's team (minus the one being
    # replaced, who by definition isn't a candidate for their own slot).
    rest_of_team = [n for n in current_team_names if n != employee_to_replace_name]
    all_excluded = list(excluded_names or []) + rest_of_team

    candidates = get_candidate_pool(
        employee_df,
        required_level,
        allowed_roles=allowed_roles,
        excluded_names=all_excluded,
        conflicting_names=conflicting_names,
    )
    # The employee being replaced shouldn't be re-suggested as their own replacement.
    candidates = candidates[candidates["Employee_Name"] != employee_to_replace_name].reset_index(drop=True)

    if candidates.empty:
        raise PromptBuildError(
            f"No alternative candidate available to replace {employee_to_replace_name} for Train "
            f"{train_number} on {day} {slot_label} ({slot_start}-{slot_end})."
        )

    candidate_records = candidates.to_dict(orient="records")

    prompt = f"""You are an AI maintenance scheduling assistant. The admin wants to replace one team member on an existing assignment. Recommend exactly ONE alternative employee.

TRAIN NUMBER: {train_number}
DAY: {day}
SECTION: {section}
TIME SLOT: {slot_label} ({slot_start}-{slot_end})

PROBLEM STATEMENT (CATEGORY): {category_label}
REQUIRED MINIMUM EXPERIENCE LEVEL: {required_level}
IMPORTANCE / PRIORITY RATING: {importance_level}/10 ({priority_label} priority)

EMPLOYEE BEING REPLACED: {employee_to_replace_name}

CANDIDATE EMPLOYEES (already filtered to available, correct role category, not already on this job's team, not time-conflicted, and at or above the required experience level):
{json.dumps(candidate_records, indent=2)}

RULES:
1. Recommend exactly ONE employee from the candidates above.
2. Explain why this employee is a suitable replacement.

RESPONSE FORMAT (JSON only, no extra text):
{{
  "train_number": "{train_number}",
  "recommended_employees": [
    {{
      "employee_id": "...",
      "employee_name": "...",
      "job_role": "...",
      "experience_level": "...",
      "reason": "..."
    }}
  ],
  "notes": ""
}}
"""
    return prompt


def find_earliest_available_slot(
    train_number: str,
    problem_statement: Union[int, str],
    problem_rating: int,
    employee_df: pd.DataFrame,
    excluded_names: Optional[List[str]] = None,
    weekly_schedule: Optional[List[dict]] = None,
) -> dict:
    """
    Search every (day, section, slot) combination — in that priority order,
    day outermost, then section, then slot — and return the FIRST one where:
      1. the section isn't already occupied by an overlapping job that day, and
      2. at least one qualified, available, non-conflicting employee exists.

    Because jobs are processed in importance order before this is called,
    giving each job the earliest feasible combo means higher-importance
    jobs naturally land on earlier days and earlier sections — later, lower-
    priority jobs only get pushed later once earlier slots are taken.

    Returns:
        {"day": ..., "section": ..., "slot_label": ..., "slot_start": ..., "slot_end": ...}

    Raises:
        PromptBuildError: if no feasible combination exists anywhere this week.
    """
    category_info = resolve_problem_statement(problem_statement)
    required_level = determine_required_level(problem_rating)
    schedule = weekly_schedule or []
    slots = generate_daily_slots()

    for day in DAYS_OF_WEEK:
        for section in SECTIONS:
            for slot in slots:
                if is_section_occupied(schedule, day, section, slot["start"], slot["end"]):
                    continue
                conflicting_names = get_employees_assigned_on_day(schedule, day)
                candidates = get_candidate_pool(
                    employee_df,
                    required_level,
                    allowed_roles=category_info["roles"],
                    excluded_names=excluded_names,
                    conflicting_names=conflicting_names,
                )
                if not candidates.empty:
                    return {
                        "day": day,
                        "section": section,
                        "slot_label": slot["label"],
                        "slot_start": slot["start"],
                        "slot_end": slot["end"],
                    }

    raise PromptBuildError(
        f"No feasible day/section/slot found anywhere this week for Train {train_number} "
        f"(category '{category_info['label']}', required level '{required_level}'). "
        f"Every combination is either already occupied or has no qualified employee left."
    )


if __name__ == "__main__":
    import sys
    from data_loader import load_employee_dataset
    from weekly_schedule import create_empty_schedule, add_assignment

    path = sys.argv[1] if len(sys.argv) > 1 else "employee_dataset_sample.csv"
    df = load_employee_dataset(path)

    schedule = create_empty_schedule()
    add_assignment(
        schedule, train_number="210", problem_statement="Electrical, Signalling & Equipment",
        problem_rating=5, importance_level=4, day="Monday", section="Section 1",
        slot_label="B", slot_start="01:30", slot_end="02:30",
        assigned_employees=[{"employee_id": "EMP005", "employee_name": "William Tan"}],
    )

    # Overlapping slot on the SAME day, DIFFERENT section -> should still conflict.
    test_prompt = build_employee_recommendation_prompt(
        train_number="215",
        problem_statement=2,
        problem_rating=8,
        importance_level=9,
        employee_df=df,
        day="Monday",
        section="Section 2",
        slot_label="C",
        slot_start="02:00",
        slot_end="03:00",
        weekly_schedule=schedule,
    )
    print(test_prompt)