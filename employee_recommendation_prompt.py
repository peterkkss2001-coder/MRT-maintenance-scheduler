"""
employee_recommendation_prompt.py

Builds the prompt sent to the LLM to recommend a qualified employee for a
maintenance problem, as part of a WEEKLY schedule.

The dataset currently has exactly three job-role families, so the problem
statement is one of three fixed categories rather than free text:

    1 -> "Track & Infrastructure"              -> Track Engineer / Track Technician
    2 -> "Electrical, Signalling & Equipment"   -> Electrical Engineer / Electrical Technician
    3 -> "Operations & Safety"                  -> Safety Officer / Safety Inspector

Flow this module supports:
    Admin enters train number + problem statement choice (1/2/3)
    + complexity rating (1-10) + importance/priority rating (1-10)
        -> problem statement choice determines the allowed Job_Role family
        -> complexity rating is mapped to a required experience level
            (Junior / Intermediate / Senior)
        -> employee dataset is filtered down to a candidate pool:
            available, correct role family, not admin-excluded, not already
            assigned elsewhere this week (weekly conflict), and at the
            required experience level
        -> a structured prompt is built and returned as text, including the
           train number and importance level as context for the LLM
        -> main.py sends that prompt to the LLM API and gets back
           a recommendation
        -> once approved, main.py records the assignment in the weekly
           schedule (see weekly_schedule.py) so future requests this week
           see it as a conflict

This module does NOT call the LLM itself — it only builds the prompt.
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
from weekly_schedule import get_assigned_employee_names

# --------------------------------------------------------------------------
# Problem statement categories -> allowed Job_Role family
# --------------------------------------------------------------------------
# Numbered 1-3 so main.py can offer a simple numbered menu for input.
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

# --------------------------------------------------------------------------
# Rating -> required experience level mapping (problem complexity)
# --------------------------------------------------------------------------
# Assumption: the admin rates the problem's COMPLEXITY on a 1-10 scale.
# Higher rating = more complex = needs a more senior worker.
# A worker of a given level is assumed able to handle problems at their own
# level and below (a Senior can do Junior-level work, not vice versa), so
# filtering keeps everyone AT OR ABOVE the required level.
EXPERIENCE_LEVELS_ORDER = ["Junior", "Intermediate", "Senior"]

RATING_MIN = 1
RATING_MAX = 10

# --------------------------------------------------------------------------
# Importance/priority rating -> readable label (for prompt + report only;
# does not currently gate WHO is selected, only gives the LLM context on
# how urgent/critical this train job is relative to others this week).
# --------------------------------------------------------------------------
IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 10


class PromptBuildError(Exception):
    """Raised when the recommendation prompt can't be built (bad input, no candidates, etc.)."""


def _validate_rating(value: int, min_val: int, max_val: int, field_name: str) -> int:
    """Shared validation for any 1-10 style rating input."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise PromptBuildError(f"{field_name} must be an integer, got: {value!r}")
    if value < min_val or value > max_val:
        raise PromptBuildError(f"{field_name} must be between {min_val} and {max_val}, got: {value}")
    return value


def resolve_problem_statement(problem_statement: Union[int, str]) -> dict:
    """
    Resolve a problem statement choice into its category info.

    Accepts either:
      - an integer (or numeric string) 1, 2, or 3, matching PROBLEM_STATEMENT_OPTIONS, or
      - the exact category label string (e.g. "Operations & Safety").

    Returns:
        {"label": str, "roles": List[str]}

    Raises:
        PromptBuildError: if the value doesn't match any known category.
    """
    # Try numeric match first (int, or a string like "2").
    try:
        choice_num = int(problem_statement)
        if choice_num in PROBLEM_STATEMENT_OPTIONS:
            return PROBLEM_STATEMENT_OPTIONS[choice_num]
    except (ValueError, TypeError):
        pass

    # Fall back to matching by label text (case-insensitive).
    if isinstance(problem_statement, str):
        for info in PROBLEM_STATEMENT_OPTIONS.values():
            if info["label"].lower() == problem_statement.strip().lower():
                return info

    valid_options = ", ".join(f"{k}={v['label']}" for k, v in PROBLEM_STATEMENT_OPTIONS.items())
    raise PromptBuildError(
        f"problem_statement must be one of: {valid_options}. Got: {problem_statement!r}"
    )


def determine_required_level(problem_rating: int) -> str:
    """
    Map a problem COMPLEXITY rating (1-10) to the minimum required employee
    experience level.

        1-3  -> Junior
        4-7  -> Intermediate
        8-10 -> Senior
    """
    _validate_rating(problem_rating, RATING_MIN, RATING_MAX, "problem_rating")

    if problem_rating <= 3:
        return "Junior"
    elif problem_rating <= 7:
        return "Intermediate"
    else:
        return "Senior"


def determine_priority_label(importance_level: int) -> str:
    """
    Map an IMPORTANCE rating (1-10) to a readable priority label, used only
    for context in the prompt/report (does not filter candidates).

        1-3  -> Low
        4-7  -> Medium
        8-10 -> Critical
    """
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
      3. Excludes any admin-specified names.
      4. Excludes any employees already assigned to another train this
         week (weekly schedule conflicts).
      5. Only employees at or above the required experience level.

    Args:
        employee_df: full employee DataFrame (from data_loader.load_employee_dataset).
        required_level: "Junior", "Intermediate", or "Senior" (from determine_required_level).
        allowed_roles: Job_Role values eligible for this problem category
            (from PROBLEM_STATEMENT_OPTIONS[choice]["roles"]). If None, no
            role filtering is applied.
        excluded_names: employee names the admin wants excluded from consideration.
        conflicting_names: employee names already committed to another train
            job this week (from weekly_schedule.get_assigned_employee_names).

    Returns:
        Filtered DataFrame of candidates. May be empty if nobody qualifies.
    """
    if required_level not in EXPERIENCE_LEVELS_ORDER:
        raise PromptBuildError(
            f"required_level must be one of {EXPERIENCE_LEVELS_ORDER}, got: {required_level!r}"
        )
    if "Experience_Level" not in employee_df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Experience_Level' column.")
    if "Job_Role" not in employee_df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Job_Role' column.")

    candidates = get_available_employees(employee_df)

    if allowed_roles:
        allowed_roles_lower = {r.lower() for r in allowed_roles}
        candidates = candidates[
            candidates["Job_Role"].str.lower().isin(allowed_roles_lower)
        ].reset_index(drop=True)

    # Combine admin exclusions + weekly-conflict exclusions into one filter pass.
    all_excluded = list(excluded_names or []) + list(conflicting_names or [])
    candidates = exclude_employees(candidates, all_excluded)

    min_rank = EXPERIENCE_LEVELS_ORDER.index(required_level)
    candidates = candidates[
        candidates["Experience_Level"].apply(
            lambda lvl: EXPERIENCE_LEVELS_ORDER.index(lvl) >= min_rank
            if lvl in EXPERIENCE_LEVELS_ORDER
            else False
        )
    ].reset_index(drop=True)

    return candidates


def build_employee_recommendation_prompt(
    train_number: str,
    problem_statement: Union[int, str],
    problem_rating: int,
    importance_level: int,
    employee_df: pd.DataFrame,
    excluded_names: Optional[List[str]] = None,
    weekly_schedule: Optional[List[dict]] = None,
) -> str:
    """
    Build the full text prompt to send to the LLM, asking it to recommend
    the best employee(s) for a maintenance problem on a specific train, as
    part of the week's overall schedule.

    Args:
        train_number: the train identifier, e.g. "215".
        problem_statement: one of the three fixed problem categories —
            pass 1, 2, or 3 (see PROBLEM_STATEMENT_OPTIONS), or the exact
            label string (e.g. "Operations & Safety").
        problem_rating: 1-10 COMPLEXITY rating, used to determine the
            minimum required experience level (Junior/Intermediate/Senior).
        importance_level: 1-10 IMPORTANCE/priority rating for this train job
            relative to others this week. Passed to the LLM as context and
            included in the report; does not filter candidates.
        employee_df: full employee dataset (already loaded/validated via
            data_loader.load_employee_dataset).
        excluded_names: optional list of employee names to exclude from
            consideration (admin-specified constraint).
        weekly_schedule: the running list of this week's assignments so far
            (from weekly_schedule.create_empty_schedule() / add_assignment()).
            Employees already assigned to another train this week are
            automatically excluded to avoid conflicts.

    Returns:
        A single string prompt, ready to be sent as the user message to the
        LLM API in main.py.

    Raises:
        PromptBuildError: if train_number is empty, problem_statement doesn't
            match a known category, either rating is invalid, or no
            candidates remain after filtering.
    """
    if not train_number or not str(train_number).strip():
        raise PromptBuildError("train_number cannot be empty.")

    category_info = resolve_problem_statement(problem_statement)
    category_label = category_info["label"]
    allowed_roles = category_info["roles"]

    required_level = determine_required_level(problem_rating)
    priority_label = determine_priority_label(importance_level)

    conflicting_names = get_assigned_employee_names(weekly_schedule) if weekly_schedule else []

    candidates = get_candidate_pool(
        employee_df,
        required_level,
        allowed_roles=allowed_roles,
        excluded_names=excluded_names,
        conflicting_names=conflicting_names,
    )

    if candidates.empty:
        raise PromptBuildError(
            f"No available candidates found for category '{category_label}' "
            f"(roles: {allowed_roles}) at or above '{required_level}' level for Train "
            f"{train_number} (after applying exclusions: {excluded_names or 'none'}, "
            f"and weekly conflicts: {conflicting_names or 'none'})."
        )

    candidate_records = candidates.to_dict(orient="records")

    prompt = f"""You are an AI maintenance scheduling assistant. Recommend the best qualified employee(s) for the maintenance problem below, as part of this week's overall train maintenance schedule.

TRAIN NUMBER: {train_number}

PROBLEM STATEMENT (CATEGORY): {category_label}
ELIGIBLE JOB ROLES FOR THIS CATEGORY: {allowed_roles}

PROBLEM COMPLEXITY RATING: {problem_rating}/10
REQUIRED MINIMUM EXPERIENCE LEVEL: {required_level}

IMPORTANCE / PRIORITY RATING: {importance_level}/10 ({priority_label} priority)

EXCLUDED EMPLOYEES (admin-specified, must not be selected): {excluded_names if excluded_names else "None"}

EMPLOYEES EXCLUDED DUE TO WEEKLY SCHEDULE CONFLICT (already assigned to another train this week): {conflicting_names if conflicting_names else "None"}

CANDIDATE EMPLOYEES (already filtered to available, correct role category, not excluded, not conflicting, and at or above the required experience level):
{json.dumps(candidate_records, indent=2)}

RULES:
1. Only select from the candidate employees listed above.
2. Do not select any employee whose experience level is below "{required_level}".
3. Do not select any employee outside the eligible job roles for this category: {allowed_roles}.
4. Within the eligible roles, if multiple employees are equally qualified, prefer the one with more relevant experience.
5. Take the importance/priority rating into account: for higher-priority (Critical) jobs, prefer the most experienced qualified employee available; for lower-priority (Low) jobs, more junior-but-qualified employees can be preferred to preserve senior staff for more critical work later in the week.
6. Explain the reason for every recommended employee.
7. If the candidate pool is thin (e.g. only one qualified employee, or several equally strong options were excluded due to conflicts), mention this in "notes".

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


if __name__ == "__main__":
    # Simple manual smoke test when running this file directly.
    import sys
    from data_loader import load_employee_dataset
    from weekly_schedule import create_empty_schedule, add_assignment

    path = sys.argv[1] if len(sys.argv) > 1 else "employee_dataset_sample.csv"
    df = load_employee_dataset(path)

    # Simulate a weekly schedule that already has one assignment this week.
    schedule = create_empty_schedule()
    add_assignment(
        schedule,
        train_number="210",
        problem_statement="Electrical, Signalling & Equipment",
        problem_rating=5,
        importance_level=4,
        assigned_employees=[{"employee_id": "EMP005", "employee_name": "William Tan"}],
    )

    test_prompt = build_employee_recommendation_prompt(
        train_number="215",
        problem_statement=2,  # Electrical, Signalling & Equipment
        problem_rating=8,
        importance_level=9,
        employee_df=df,
        excluded_names=["John Tan"],
        weekly_schedule=schedule,
    )
    print(test_prompt)