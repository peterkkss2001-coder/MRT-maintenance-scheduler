"""
problem_statement.py

Flask web interface for the MRT Maintenance Scheduler.

Thin UI layer over the existing modules:
    data_loader.py                     -> loads/validates the employee CSV
    employee_recommendation_prompt.py  -> builds prompts, category rules, replacement logic
    llm_client.py                      -> calls the OpenAI API, parses JSON
    weekly_schedule.py                 -> day/section/slot conflict tracking, CSV export

NOTE ON STATE: pending jobs and the schedule live in in-memory globals, same
as before — fine for one admin locally, not safe for concurrent users.

NOTE ON INTEGRATION: this file currently runs standalone (see the
`if __name__ == "__main__"` block at the bottom). Once the main
program/home page is built to bring this together with other people's
modules, the Flask routes defined here can be registered onto that
shared app instead — e.g. as a Blueprint — rather than running via
`app.run()` directly. Flag this to whoever wires up the main entry point.

RUNNING (standalone, for now):
    1. pip install flask openai python-dotenv pandas
    2. .env with OPENAI_API_KEY=sk-...
    3. python problem_statement.py
    4. Open http://127.0.0.1:5000
"""

from __future__ import annotations

import copy
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

from data_loader import load_employee_dataset, EmployeeDatasetError
from employee_recommendation_prompt import (
    build_employee_recommendation_prompt,
    build_replacement_prompt,
    find_earliest_available_slot,
    determine_priority_label,
    get_candidate_pool,
    determine_required_level,
    PromptBuildError,
    PROBLEM_STATEMENT_OPTIONS,
)
from weekly_schedule import (
    create_empty_schedule,
    add_assignment,
    save_schedule_auto_numbered,
    get_conflicting_employee_names,
    generate_daily_slots,
    DAYS_OF_WEEK,
    SECTIONS,
    MAX_EMPLOYEES_PER_PROBLEM,
    WeeklyScheduleError,
)
from llm_client import get_employee_recommendation, LLMClientError

load_dotenv()

# ============================================================
# PARAMETERS
# ============================================================
EMPLOYEE_DATASET_PATH = "employee_dataset_sample.csv"
WEEKLY_SCHEDULE_OUTPUT_DIR = "weekly_schedule_output"
LLM_MODEL = "gpt-4o-mini"
SECRET_KEY = "dev-only-change-me"
# ============================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY

employees = load_employee_dataset(EMPLOYEE_DATASET_PATH)
SLOTS = generate_daily_slots()  # e.g. [{"label": "A", "start": "00:30", "end": "01:30"}, ...]
SLOTS_BY_LABEL = {s["label"]: s for s in SLOTS}

PENDING_PROBLEMS: list = []
SORTED_PROBLEMS: list = []
ASSIGN_INDEX = 0
WEEKLY_SCHEDULE = create_empty_schedule()
UNASSIGNED: list = []
_EXPORT_PATHS = None  # cached (csv_path, json_path) so both download buttons reuse one export

# ============================================================
# >>> LIVE SCHEDULE (JSON) <<<
# Kept in sync with WEEKLY_SCHEDULE on every approved assignment (see
# assign_action() below) — NOT just when a download button is clicked.
# Other functions or modules can read this at any point during the
# session, even mid-week before anything has been exported to disk —
# e.g. `from problem_statement import LIVE_SCHEDULE_JSON`. It's a plain
# Python list-of-dicts (already JSON-shaped), deep-copied on each update
# so callers never hold a reference that changes under them later.
# Use get_live_schedule_json() for the same thing via a function call
# instead of a direct import.
# ============================================================
LIVE_SCHEDULE_JSON: list = []


def reset_state():
    global PENDING_PROBLEMS, SORTED_PROBLEMS, ASSIGN_INDEX, WEEKLY_SCHEDULE, UNASSIGNED, LIVE_SCHEDULE_JSON, _EXPORT_PATHS
    PENDING_PROBLEMS = []
    SORTED_PROBLEMS = []
    ASSIGN_INDEX = 0
    WEEKLY_SCHEDULE = create_empty_schedule()
    UNASSIGNED = []
    LIVE_SCHEDULE_JSON = []
    _EXPORT_PATHS = None


def get_live_schedule_json() -> list:
    """
    Getter for the >>> LIVE SCHEDULE (JSON) <<< variable above — returns
    the weekly schedule AS OF THE MOST RECENT APPROVED ASSIGNMENT, as a
    plain Python list-of-dicts (empty list if nothing has been approved
    yet this session). Unlike the old export-only snapshot, this updates
    immediately, before any file has been downloaded.
    """
    return LIVE_SCHEDULE_JSON


def _refresh_live_schedule_json():
    """Called after every approved assignment to keep LIVE_SCHEDULE_JSON in sync."""
    global LIVE_SCHEDULE_JSON
    LIVE_SCHEDULE_JSON = copy.deepcopy(WEEKLY_SCHEDULE)


@app.context_processor
def inject_globals():
    return {"job_count": len(WEEKLY_SCHEDULE)}


def _current_problem():
    if ASSIGN_INDEX >= len(SORTED_PROBLEMS):
        return None
    return SORTED_PROBLEMS[ASSIGN_INDEX]


# ============================================================
# PHASE 1 — INPUT
# ============================================================

@app.route("/")
def index():
    return render_template(
        "index.html",
        phase="input",
        phase_index=0,
        employees=employees.to_dict(orient="records"),
        categories=PROBLEM_STATEMENT_OPTIONS,
        pending=PENDING_PROBLEMS,
        days=DAYS_OF_WEEK,
        sections=SECTIONS,
        slots=SLOTS,
    )


@app.route("/add_problem", methods=["POST"])
def add_problem():
    train_number = request.form.get("train_number", "").strip()
    problem_statement = int(request.form.get("problem_statement", 1))
    problem_rating = int(request.form.get("problem_rating", 5))
    importance_level = int(request.form.get("importance_level", 5))
    raw_excluded = request.form.get("excluded", "").strip()
    excluded_names = [n.strip() for n in raw_excluded.split(",") if n.strip()] if raw_excluded else []

    # "auto_assign" checkbox present (checked) -> let the AI pick day/section/slot.
    # Unchecked -> admin filled in the manual day/section/slot fields themselves.
    auto_assign = request.form.get("auto_assign") == "1"

    if not train_number:
        flash("Train number is required.")
        return redirect(url_for("index"))

    entry = {
        "train_number": train_number,
        "problem_statement": problem_statement,
        "problem_rating": problem_rating,
        "importance_level": importance_level,
        "excluded_names": excluded_names,
        "auto_assign": auto_assign,
    }

    if auto_assign:
        # Determined later, once this job is actually processed in priority
        # order — see _build_recommendation_for(). Left unset for now so the
        # pending list can show "AI will choose" instead of a guess.
        entry.update({"day": None, "section": None, "slot_label": None, "slot_start": None, "slot_end": None})
    else:
        day = request.form.get("day", DAYS_OF_WEEK[0])
        section = request.form.get("section", SECTIONS[0])
        slot_label = request.form.get("slot", SLOTS[0]["label"])
        slot = SLOTS_BY_LABEL[slot_label]
        entry.update({
            "day": day, "section": section, "slot_label": slot_label,
            "slot_start": slot["start"], "slot_end": slot["end"],
        })

    PENDING_PROBLEMS.append(entry)
    return redirect(url_for("index"))


@app.route("/remove_problem/<int:idx>", methods=["POST"])
def remove_problem(idx):
    if 0 <= idx < len(PENDING_PROBLEMS):
        PENDING_PROBLEMS.pop(idx)
    return redirect(url_for("index"))


# ============================================================
# PHASE 2 — ASSIGN
# ============================================================

@app.route("/start_assignment", methods=["POST"])
def start_assignment():
    global SORTED_PROBLEMS, ASSIGN_INDEX
    if not PENDING_PROBLEMS:
        flash("Add at least one train job before starting assignment.")
        return redirect(url_for("index"))

    SORTED_PROBLEMS = sorted(PENDING_PROBLEMS, key=lambda p: (-p["importance_level"], -p["problem_rating"]))
    ASSIGN_INDEX = 0
    return redirect(url_for("assign"))


def _build_recommendation_for(problem):
    """Build the prompt + call the LLM for `problem`, caching the result on it."""
    if "recommendation" in problem or "error" in problem:
        return

    # Auto-assign mode: figure out the earliest feasible day/section/slot now,
    # right before this job is actually processed — not at input time — so
    # it reflects everyone already assigned ahead of it in priority order.
    if problem.get("auto_assign") and problem.get("day") is None:
        try:
            slot_info = find_earliest_available_slot(
                train_number=problem["train_number"],
                problem_statement=problem["problem_statement"],
                problem_rating=problem["problem_rating"],
                employee_df=employees,
                excluded_names=problem["excluded_names"],
                weekly_schedule=WEEKLY_SCHEDULE,
            )
        except PromptBuildError as e:
            problem["error"] = str(e)
            return
        problem.update(slot_info)

    try:
        prompt = build_employee_recommendation_prompt(
            train_number=problem["train_number"],
            problem_statement=problem["problem_statement"],
            problem_rating=problem["problem_rating"],
            importance_level=problem["importance_level"],
            employee_df=employees,
            day=problem["day"],
            section=problem["section"],
            slot_label=problem["slot_label"],
            slot_start=problem["slot_start"],
            slot_end=problem["slot_end"],
            excluded_names=problem["excluded_names"],
            weekly_schedule=WEEKLY_SCHEDULE,
        )
    except (PromptBuildError, EmployeeDatasetError) as e:
        problem["error"] = str(e)
        return

    try:
        result = get_employee_recommendation(prompt, model=LLM_MODEL)
    except LLMClientError as e:
        problem["error"] = str(e)
        return

    # Defensive cap in case the model ignores the team-size rule.
    team = result.get("recommended_employees", [])
    if len(team) > MAX_EMPLOYEES_PER_PROBLEM:
        result["recommended_employees"] = team[:MAX_EMPLOYEES_PER_PROBLEM]
        note = result.get("notes", "")
        result["notes"] = (note + " " if note else "") + (
            f"Note: AI suggested more than {MAX_EMPLOYEES_PER_PROBLEM} employees; truncated to the first "
            f"{MAX_EMPLOYEES_PER_PROBLEM}."
        )
    problem["recommendation"] = result


def _available_replacement_candidates(problem):
    """Candidates eligible to replace ANY member of the current team (same filters, minus current team)."""
    category_info = PROBLEM_STATEMENT_OPTIONS[problem["problem_statement"]]
    required_level = determine_required_level(problem["problem_rating"])
    conflicting_names = get_conflicting_employee_names(WEEKLY_SCHEDULE, problem["day"], problem["slot_start"], problem["slot_end"])
    current_team_names = [e["employee_name"] for e in problem.get("recommendation", {}).get("recommended_employees", [])]
    excluded = list(problem["excluded_names"]) + current_team_names

    candidates = get_candidate_pool(
        employees, required_level, allowed_roles=category_info["roles"],
        excluded_names=excluded, conflicting_names=conflicting_names,
    )
    return candidates.to_dict(orient="records")


@app.route("/assign")
def assign():
    problem = _current_problem()
    if problem is None:
        return redirect(url_for("summary"))

    _build_recommendation_for(problem)

    priority_label = determine_priority_label(problem["importance_level"])
    priority_class = priority_label.lower() if priority_label != "Critical" else "critical"

    replacement_candidates = []
    if problem.get("recommendation"):
        replacement_candidates = _available_replacement_candidates(problem)

    return render_template(
        "assign.html",
        phase="assign",
        phase_index=1,
        problem=problem,
        categories=PROBLEM_STATEMENT_OPTIONS,
        error=problem.get("error"),
        recommendation=problem.get("recommendation"),
        priority_label=priority_label,
        priority_class=priority_class,
        position=ASSIGN_INDEX + 1,
        total=len(SORTED_PROBLEMS),
        replacement_candidates=replacement_candidates,
    )


@app.route("/assign/action", methods=["POST"])
def assign_action():
    global ASSIGN_INDEX

    problem = _current_problem()
    if problem is None:
        return redirect(url_for("summary"))

    decision = request.form.get("decision")

    if decision == "approve" and problem.get("recommendation"):
        try:
            add_assignment(
                WEEKLY_SCHEDULE,
                train_number=problem["train_number"],
                problem_statement=PROBLEM_STATEMENT_OPTIONS[problem["problem_statement"]]["label"],
                problem_rating=problem["problem_rating"],
                importance_level=problem["importance_level"],
                day=problem["day"],
                section=problem["section"],
                slot_label=problem["slot_label"],
                slot_start=problem["slot_start"],
                slot_end=problem["slot_end"],
                assigned_employees=problem["recommendation"]["recommended_employees"],
            )
            # >>> KEEP THE LIVE SCHEDULE (JSON) VARIABLE IN SYNC <<<
            # Updated here, immediately on approval — not just at export —
            # so other functions can read the current schedule mid-session.
            _refresh_live_schedule_json()
        except WeeklyScheduleError as e:
            UNASSIGNED.append({"train_number": problem["train_number"], "reason": str(e)})
    elif decision == "skip" and problem.get("error"):
        UNASSIGNED.append({"train_number": problem["train_number"], "reason": problem["error"]})
    else:
        UNASSIGNED.append({"train_number": problem["train_number"], "reason": "Admin rejected the AI recommendation."})

    ASSIGN_INDEX += 1
    return redirect(url_for("assign"))


@app.route("/assign/replace", methods=["POST"])
def assign_replace():
    """Swap one team member on the CURRENT job's cached recommendation — either via
    a fresh AI suggestion, or a manually picked employee from the filtered dropdown."""
    problem = _current_problem()
    if problem is None or not problem.get("recommendation"):
        return redirect(url_for("assign"))

    employee_index = int(request.form.get("employee_index"))
    method = request.form.get("method")
    team = problem["recommendation"]["recommended_employees"]

    if employee_index < 0 or employee_index >= len(team):
        flash("Could not find that team member to replace.")
        return redirect(url_for("assign"))

    old_employee = team[employee_index]

    if method == "manual":
        manual_id = request.form.get("manual_employee_id")
        match = employees[employees["Employee_ID"] == manual_id]
        if match.empty:
            flash("Selected employee not found or no longer available.")
            return redirect(url_for("assign"))
        row = match.iloc[0]
        team[employee_index] = {
            "employee_id": row["Employee_ID"],
            "employee_name": row["Employee_Name"],
            "job_role": row["Job_Role"],
            "experience_level": row["Experience_Level"],
            "reason": f"Manually selected by admin, replacing {old_employee['employee_name']}.",
        }

    elif method == "ai":
        current_team_names = [e["employee_name"] for e in team]
        try:
            prompt = build_replacement_prompt(
                train_number=problem["train_number"],
                problem_statement=problem["problem_statement"],
                problem_rating=problem["problem_rating"],
                importance_level=problem["importance_level"],
                employee_df=employees,
                day=problem["day"],
                section=problem["section"],
                slot_label=problem["slot_label"],
                slot_start=problem["slot_start"],
                slot_end=problem["slot_end"],
                employee_to_replace_name=old_employee["employee_name"],
                current_team_names=current_team_names,
                excluded_names=problem["excluded_names"],
                weekly_schedule=WEEKLY_SCHEDULE,
            )
        except PromptBuildError as e:
            flash(f"Could not find a replacement: {e}")
            return redirect(url_for("assign"))

        try:
            result = get_employee_recommendation(prompt, model=LLM_MODEL)
        except LLMClientError as e:
            flash(f"AI replacement call failed: {e}")
            return redirect(url_for("assign"))

        new_employees = result.get("recommended_employees", [])
        if new_employees:
            team[employee_index] = new_employees[0]

    return redirect(url_for("assign"))


# ============================================================
# PHASE 3 — EXPORT
# ============================================================

@app.route("/summary")
def summary():
    return render_template(
        "summary.html",
        phase="export",
        phase_index=2,
        schedule=WEEKLY_SCHEDULE,
        unassigned=UNASSIGNED,
    )


def _ensure_exported():
    """Write the CSV+JSON pair once per 'export session', reusing the same
    files if the admin clicks both download buttons rather than creating a
    fresh numbered pair each time. (LIVE_SCHEDULE_JSON is already kept in
    sync as assignments are approved — see _refresh_live_schedule_json —
    so no update is needed here.)"""
    global _EXPORT_PATHS
    if _EXPORT_PATHS is None:
        _EXPORT_PATHS = save_schedule_auto_numbered(WEEKLY_SCHEDULE, WEEKLY_SCHEDULE_OUTPUT_DIR)
    return _EXPORT_PATHS


@app.route("/export")
def export():
    csv_path, json_path = _ensure_exported()
    return send_file(csv_path, as_attachment=True)


@app.route("/export_json")
def export_json():
    csv_path, json_path = _ensure_exported()
    return send_file(json_path, as_attachment=True)


@app.route("/reset", methods=["POST"])
def reset():
    reset_state()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)