"""
Shared data, constants, and JSON persistence for the shift schedule app.
"""

import json
import os
from datetime import datetime, timedelta

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
