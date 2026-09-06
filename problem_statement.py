"""
app.py

Flask web interface for the MRT Maintenance Scheduler.

This is a thin UI layer over the existing, already-tested modules:
    data_loader.py                 -> loads/validates the employee CSV
    employee_recommendation_prompt.py -> builds the LLM prompt, category rules
    llm_client.py                  -> calls the OpenAI API, parses JSON
    weekly_schedule.py             -> tracks assignments, conflict checks, CSV export

No scheduling logic lives in this file — it only collects input, calls the
existing functions in the right order, and renders the result as HTML.

NOTE ON STATE: this prototype keeps the week's pending jobs and schedule in
simple in-memory Python variables (PENDING_PROBLEMS, WEEKLY_SCHEDULE, etc.),
the same way main.py did. That's fine for a single admin using the tool
locally, but it means state resets if the server restarts, and it is NOT
safe for multiple people using the app at the same time (they'd share the
same in-memory state). A production version would move this into a
database or per-session storage.

RUNNING THIS APP:
    1. pip install flask openai python-dotenv pandas
    2. Make sure .env has OPENAI_API_KEY=sk-...
    3. python app.py
    4. Open http://127.0.0.1:5000 in your browser
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

from data_loader import load_employee_dataset, EmployeeDatasetError
from employee_recommendation_prompt import (
    build_employee_recommendation_prompt,
    determine_priority_label,
    PromptBuildError,
    PROBLEM_STATEMENT_OPTIONS,
)
from weekly_schedule import (
    create_empty_schedule,
    add_assignment,
    save_schedule_auto_numbered,
    WeeklyScheduleError,
)
from llm_client import get_employee_recommendation, LLMClientError

load_dotenv()

# ============================================================
# PARAMETERS — modify as needed
# ============================================================
EMPLOYEE_DATASET_PATH = "employee_dataset_sample.csv"
WEEKLY_SCHEDULE_OUTPUT_DIR = "weekly_schedule_output"
LLM_MODEL = "gpt-4o-mini"
SECRET_KEY = "dev-only-change-me"  # only used for flash messages, not auth
# ============================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Loaded once at startup — reused across all requests.
employees = load_employee_dataset(EMPLOYEE_DATASET_PATH)

# ---- In-memory session state (see NOTE ON STATE above) ----
PENDING_PROBLEMS: list = []       # jobs entered, not yet processed
SORTED_PROBLEMS: list = []        # PENDING_PROBLEMS sorted by priority, once "Start assignment" is pressed
ASSIGN_INDEX = 0                  # pointer into SORTED_PROBLEMS during the assign phase
WEEKLY_SCHEDULE = create_empty_schedule()
UNASSIGNED: list = []             # jobs that ended up without an assignment, with a reason


def reset_state():
    global PENDING_PROBLEMS, SORTED_PROBLEMS, ASSIGN_INDEX, WEEKLY_SCHEDULE, UNASSIGNED
    PENDING_PROBLEMS = []
    SORTED_PROBLEMS = []
    ASSIGN_INDEX = 0
    WEEKLY_SCHEDULE = create_empty_schedule()
    UNASSIGNED = []


@app.context_processor
def inject_globals():
    """Values every template needs (job counter in the header)."""
    return {"job_count": len(WEEKLY_SCHEDULE)}


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
    )


@app.route("/add_problem", methods=["POST"])
def add_problem():
    train_number = request.form.get("train_number", "").strip()
    problem_statement = int(request.form.get("problem_statement", 1))
    problem_rating = int(request.form.get("problem_rating", 5))
    importance_level = int(request.form.get("importance_level", 5))
    raw_excluded = request.form.get("excluded", "").strip()
    excluded_names = [n.strip() for n in raw_excluded.split(",") if n.strip()] if raw_excluded else []

    if not train_number:
        flash("Train number is required.")
        return redirect(url_for("index"))

    PENDING_PROBLEMS.append(
        {
            "train_number": train_number,
            "problem_statement": problem_statement,
            "problem_rating": problem_rating,
            "importance_level": importance_level,
            "excluded_names": excluded_names,
        }
    )
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

    # Highest importance first, complexity as tiebreaker — see
    # employee_recommendation_prompt.py for why this ordering matters:
    # it guarantees the most critical jobs get first pick of qualified staff.
    SORTED_PROBLEMS = sorted(
        PENDING_PROBLEMS, key=lambda p: (-p["importance_level"], -p["problem_rating"])
    )
    ASSIGN_INDEX = 0
    return redirect(url_for("assign"))


@app.route("/assign")
def assign():
    global ASSIGN_INDEX

    if ASSIGN_INDEX >= len(SORTED_PROBLEMS):
        return redirect(url_for("summary"))

    problem = SORTED_PROBLEMS[ASSIGN_INDEX]

    # Cache the LLM result on the problem dict so refreshing this page
    # doesn't re-call the API.
    if "recommendation" not in problem and "error" not in problem:
        try:
            prompt = build_employee_recommendation_prompt(
                train_number=problem["train_number"],
                problem_statement=problem["problem_statement"],
                problem_rating=problem["problem_rating"],
                importance_level=problem["importance_level"],
                employee_df=employees,
                excluded_names=problem["excluded_names"],
                weekly_schedule=WEEKLY_SCHEDULE,
            )
        except (PromptBuildError, EmployeeDatasetError) as e:
            problem["error"] = str(e)
        else:
            try:
                problem["recommendation"] = get_employee_recommendation(prompt, model=LLM_MODEL)
            except LLMClientError as e:
                problem["error"] = str(e)

    priority_label = determine_priority_label(problem["importance_level"])
    priority_class = priority_label.lower() if priority_label != "Critical" else "critical"

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
    )


@app.route("/assign/action", methods=["POST"])
def assign_action():
    global ASSIGN_INDEX

    if ASSIGN_INDEX >= len(SORTED_PROBLEMS):
        return redirect(url_for("summary"))

    problem = SORTED_PROBLEMS[ASSIGN_INDEX]
    decision = request.form.get("decision")

    if decision == "approve" and problem.get("recommendation"):
        try:
            add_assignment(
                WEEKLY_SCHEDULE,
                train_number=problem["train_number"],
                problem_statement=PROBLEM_STATEMENT_OPTIONS[problem["problem_statement"]]["label"],
                problem_rating=problem["problem_rating"],
                importance_level=problem["importance_level"],
                assigned_employees=problem["recommendation"]["recommended_employees"],
            )
        except WeeklyScheduleError as e:
            UNASSIGNED.append({"train_number": problem["train_number"], "reason": str(e)})
    elif decision == "skip" and problem.get("error"):
        UNASSIGNED.append({"train_number": problem["train_number"], "reason": problem["error"]})
    else:  # reject
        UNASSIGNED.append(
            {"train_number": problem["train_number"], "reason": "Admin rejected the AI recommendation."}
        )

    ASSIGN_INDEX += 1
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


@app.route("/export")
def export():
    output_path = save_schedule_auto_numbered(WEEKLY_SCHEDULE, WEEKLY_SCHEDULE_OUTPUT_DIR)
    return send_file(output_path, as_attachment=True)


@app.route("/reset", methods=["POST"])
def reset():
    reset_state()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)