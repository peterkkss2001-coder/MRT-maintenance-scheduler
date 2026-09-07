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

Project layout:
    app.py            <- this file: Flask routes only
    data.py           <- constants, staff/user roster, JSON load/save
    scheduling.py     <- auto-scheduling, auto-pairing, formatting helpers
    templates/
        login.html
        schedule.html  <- supervisor view
        worker.html    <- worker (read-only) view

Run with:
    python app.py

Then open http://127.0.0.1:8000
"""

from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, abort

from data import (
    DATA_FILE, DAYS, SECTORS, STAFF, USERS, PROBLEM_COLORS, PROBLEMS,
    EMERGENCY_TYPES, EMERGENCY_EMOJIS, TIME_SLOTS,
    load_data, save_data,
)
from scheduling import (
    auto_schedule, get_personal_week, get_sector_week, auto_pair_week,
    list_active_shifts, try_add_extra_shift, make_shift_key,
    normalize_paired_with, remove_pairing_references,
)

app = Flask(__name__)
app.secret_key = "shift-schedule-dev-key"  # CHANGE THIS before deploying publicly


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
    return render_template("login.html", error=error)


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
        return render_template(
            "worker.html",
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

    return render_template(
        "schedule.html",
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
    import os
    if not os.path.exists(DATA_FILE):
        save_data({sector: {} for sector in SECTORS})
    app.run(host="127.0.0.1", port=8000, debug=True)
