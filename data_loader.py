"""
data_loader.py

Handles reading and loading the employee dataset (CSV) into a clean,
validated pandas DataFrame that later stages (LLM prompt builder,
conflict checker, etc.) can rely on.

This is intentionally the FIRST building block: no LLM calls, no web
framework — just get trustworthy data into memory.
"""

from __future__ import annotations

import os
from typing import List

import pandas as pd

# Columns we currently expect in the dataset. Keeping this as a constant
# means that if the CSV schema grows later (skills, certifications,
# location, available_from/until, etc.) we only need to update this list
# and REQUIRED_COLUMNS below, not every function that touches the data.
EXPECTED_COLUMNS: List[str] = [
    "Employee_ID",
    "Employee_Name",
    "Job_Role",
    "Experience_Level",
    "Availability_Status",
]

# Columns that must be present and non-empty for every row. For now this
# is the same as EXPECTED_COLUMNS, but kept separate in case optional
# columns get added later (e.g. Certification could be allowed to be blank).
REQUIRED_COLUMNS: List[str] = EXPECTED_COLUMNS

# The set of values we currently treat as valid for Availability_Status.
# Anything else in the CSV is a data-quality problem worth flagging rather
# than silently accepting.
VALID_AVAILABILITY_STATUSES = {"Available", "Busy", "Unavailable"}


class EmployeeDatasetError(Exception):
    """Raised when the employee dataset can't be loaded or fails validation."""


def load_employee_dataset(csv_path: str, strict: bool = True) -> pd.DataFrame:
    """
    Load the employee dataset from a CSV file into a validated DataFrame.

    Args:
        csv_path: Path to the employee CSV file.
        strict: If True, raise EmployeeDatasetError on validation problems
            (missing columns, empty required fields, unknown availability
            values, duplicate Employee_IDs). If False, log warnings via the
            returned DataFrame's attrs["warnings"] instead of raising.

    Returns:
        A pandas DataFrame with clean, whitespace-trimmed employee data.
        Warnings (if strict=False) are stored in df.attrs["warnings"].

    Raises:
        EmployeeDatasetError: if the file is missing, unreadable, empty,
            or fails validation while strict=True.
    """
    if not os.path.exists(csv_path):
        raise EmployeeDatasetError(f"Employee dataset not found at: {csv_path}")

    try:
        df = pd.read_csv(csv_path, dtype=str)
    except Exception as exc:  # pandas can raise several different error types
        raise EmployeeDatasetError(f"Failed to read CSV at {csv_path}: {exc}") from exc

    if df.empty:
        raise EmployeeDatasetError(f"Employee dataset at {csv_path} is empty.")

    warnings: List[str] = []

    # --- Column validation -------------------------------------------------
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        msg = f"Missing required column(s): {missing_cols}"
        if strict:
            raise EmployeeDatasetError(msg)
        warnings.append(msg)

    extra_cols = [c for c in df.columns if c not in EXPECTED_COLUMNS]
    if extra_cols:
        # Not fatal — just worth knowing about, since new columns (skills,
        # certification, location, etc.) are expected as the dataset grows.
        warnings.append(f"Unexpected extra column(s) found (kept as-is): {extra_cols}")

    # --- Clean whitespace ----------------------------------------------------
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    # --- Row-level validation -------------------------------------------
    present_required = [c for c in REQUIRED_COLUMNS if c in df.columns]
    for col in present_required:
        empty_mask = df[col].eq("") | df[col].isna()
        if empty_mask.any():
            bad_ids = df.loc[empty_mask, "Employee_ID"].tolist() if "Employee_ID" in df.columns else empty_mask.sum()
            msg = f"Column '{col}' has empty value(s) for row(s)/ID(s): {bad_ids}"
            if strict:
                raise EmployeeDatasetError(msg)
            warnings.append(msg)

    if "Employee_ID" in df.columns:
        dupes = df["Employee_ID"][df["Employee_ID"].duplicated()].tolist()
        if dupes:
            msg = f"Duplicate Employee_ID(s) found: {dupes}"
            if strict:
                raise EmployeeDatasetError(msg)
            warnings.append(msg)

    if "Availability_Status" in df.columns:
        unknown_statuses = set(df["Availability_Status"].unique()) - VALID_AVAILABILITY_STATUSES
        if unknown_statuses:
            msg = f"Unrecognized Availability_Status value(s): {sorted(unknown_statuses)}"
            if strict:
                raise EmployeeDatasetError(msg)
            warnings.append(msg)

    df.attrs["warnings"] = warnings
    df.attrs["source_path"] = csv_path
    return df


def get_available_employees(df: pd.DataFrame) -> pd.DataFrame:
    """Return only employees currently marked as 'Available'."""
    if "Availability_Status" not in df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Availability_Status' column.")
    return df[df["Availability_Status"] == "Available"].reset_index(drop=True)


def get_employees_by_role(df: pd.DataFrame, role: str) -> pd.DataFrame:
    """Return employees matching a given Job_Role (case-insensitive, exact match)."""
    if "Job_Role" not in df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Job_Role' column.")
    return df[df["Job_Role"].str.lower() == role.lower()].reset_index(drop=True)


def exclude_employees(df: pd.DataFrame, excluded_names: List[str]) -> pd.DataFrame:
    """
    Remove employees whose Employee_Name is in excluded_names (case-insensitive).
    Used to honor admin-specified exclusions before passing data to the LLM.
    """
    if not excluded_names:
        return df
    if "Employee_Name" not in df.columns:
        raise EmployeeDatasetError("DataFrame has no 'Employee_Name' column.")
    excluded_lower = {name.strip().lower() for name in excluded_names}
    return df[~df["Employee_Name"].str.lower().isin(excluded_lower)].reset_index(drop=True)


def dataframe_to_records(df: pd.DataFrame) -> List[dict]:
    """
    Convert the DataFrame to a list of plain dicts — the format we'll want
    when building the LLM prompt payload (e.g. json.dumps(records)).
    """
    return df.to_dict(orient="records")


if __name__ == "__main__":
    # Simple manual smoke test when running this file directly.
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "employee_dataset_simple.csv"
    data = load_employee_dataset(path, strict=False)

    print(f"Loaded {len(data)} employees from {path}")
    if data.attrs.get("warnings"):
        print("\nWarnings:")
        for w in data.attrs["warnings"]:
            print(f"  - {w}")

    print("\nAvailable employees:")
    print(get_available_employees(data)[["Employee_ID", "Employee_Name", "Job_Role"]])