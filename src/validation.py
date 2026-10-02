"""Verify mandatory fields are present before a form is graded.

Two checks, combined by verify_form_and_excel:
  1. Form fields (from extraction): Student_ID, CAT score, Fitness-to-Practise,
     and all 12 CSR criteria each have at least one option selected.
  2. Excel record: the student exists in the loaded students and has a POGS
     score present.

A form is only graded if verify_form_and_excel returns complete=True. The
orchestrator gates on this BEFORE build_payload / any paid API call, and writes
incomplete forms to the needs_review section of the output.
"""

import logging
from src.logging_config import setup_logging

logger = logging.getLogger(__name__)

# The 12 CSR criteria. Note: patient_assessment uses '_some_res' for the
# "some reservations" option; the other 11 use '_reservations'. CSR_OPTIONS
# below lists every possible suffix, so a criterion is complete if ANY of its
# options is selected.
CSR_CRITERIA = [
    "patient_assessment", "clinical_decision", "communication",
    "professional_qualities", "engagement_team", "self_management",
    "clinical_knowledge", "critical_reflection", "commitment_equity",
    "cultural_safety", "disease_prevention", "health_promotion",
]

# The selection options each criterion can have (any one selected = complete).
CSR_OPTIONS = ["major", "some_res", "reservations", "good", "excellent", "not_obs"]


def _is_selected(form: dict, key: str) -> bool:
    """True if the given field exists and is selected."""
    field = form.get(key)
    if not field:
        return False
    value = field.get("value")
    return value == ":selected:"


def verify_mandatory_fields(form: dict) -> dict:
    """Check all mandatory fields are present in the extracted form.

    Args:
        form: extraction output -- {field_name: {"value", "confidence"}}

    Returns:
        {"complete": bool, "missing": [list of missing field descriptions]}
    """
    missing = []

    # --- Student ID ---
    sid = form.get("Student_ID")
    if not sid or not sid.get("value"):
        missing.append("Student_ID")

    # --- CAT score ---
    cat = form.get("cat_total_score")
    if not cat or not cat.get("value"):
        missing.append("cat_total_score")

    # --- Fitness to Practise: at least one of yes/no selected ---
    ftp_yes = _is_selected(form, "fitness_to_practise_yes")
    ftp_no = _is_selected(form, "fitness_to_practise_no")
    if not (ftp_yes or ftp_no):
        missing.append("fitness_to_practise (neither yes nor no selected)")

    # --- CSR: each of the 12 criteria must have one option selected ---
    for criterion in CSR_CRITERIA:
        selected = any(
            _is_selected(form, f"{criterion}_{opt}")
            for opt in CSR_OPTIONS
        )
        if not selected:
            missing.append(f"CSR criterion '{criterion}' (no option selected)")

    complete = len(missing) == 0
    if complete:
        logger.info("Mandatory field check: PASSED")
    else:
        logger.warning("Mandatory field check: %d missing", len(missing))
        for m in missing:
            logger.warning("  missing: %s", m)

    return {"complete": complete, "missing": missing}


def verify_excel_record(student_index: dict, student_id: str) -> dict:
    """Check the student's record exists and has a POGS score.

    Args:
        student_index: students keyed by student_id (each value is a student
            dict from read_students(), so POGS lives under 'pogs_score').
        student_id: the Student_ID extracted from the form.

    Returns:
        {"complete": bool, "missing": [list of missing descriptions]}
    """
    missing = []

    record = student_index.get(student_id)
    if record is None:
        missing.append(f"No Excel record for Student_ID '{student_id}'")
    else:
        # read_students() stores the parsed POGS under 'pogs_score'
        # (None when the Excel cell was blank).
        pogs = record.get("pogs_score")
        if pogs in (None, ""):
            missing.append("POGS score missing")

    complete = len(missing) == 0
    if complete:
        logger.info("Excel record check: PASSED for Student_ID '%s'", student_id)
    else:
        logger.warning("Excel record check: %d missing for Student_ID '%s'",
                        len(missing), student_id)
        for m in missing:
            logger.warning("  missing: %s", m)

    return {"complete": complete, "missing": missing}


def verify_form_and_excel(form: dict, student_index: dict) -> dict:
    """Run both the mandatory-field check and the Excel POGS check.

    Combines the results so the caller can gate on a single verdict before
    making any paid API call.

    Args:
        form: extraction output -- {field_name: {"value", "confidence"}}
        student_index: students keyed by student_id (from read_students()).

    Returns:
        {"complete": bool, "missing": [combined list of missing descriptions]}
    """
    form_result = verify_mandatory_fields(form)

    # Use the extracted Student_ID to look up the matching student record.
    sid_field = form.get("Student_ID") or {}
    student_id = (sid_field.get("value") or "").strip()

    if not student_id:
        # Student_ID already reported missing by verify_mandatory_fields;
        # we can't look up the record without it.
        excel_missing = ["POGS score not checked (Student_ID missing)"]
    else:
        excel_missing = verify_excel_record(student_index, student_id)["missing"]

    missing = form_result["missing"] + excel_missing
    return {"complete": len(missing) == 0, "missing": missing}


def index_students(students: list[dict]) -> dict:
    """Build a {student_id: record} lookup from the read_students() list."""
    index: dict[str, dict] = {}
    for record in students:
        sid = str(record.get("student_id", "")).strip()
        if sid and sid not in index:
            index[sid] = record
    return index


if __name__ == "__main__":
    setup_logging()
    import os
    from src.blob_reader import download_blob, list_forms
    from src.custom_extract import extract_form
    from src.excel_reader import read_students

    forms = list_forms()
    if not forms:
        logger.error("No forms found.")
        raise SystemExit(1)

    container = os.getenv("AZURE_CONTAINER_FORMS", "raw-forms")
    form_bytes = download_blob(container, forms[0])
    fields = extract_form(form_bytes)

    students = read_students()
    student_index = index_students(students)

    result = verify_form_and_excel(fields, student_index)
    print(f"\ncomplete: {result['complete']}")
    print(f"missing:  {result['missing']}")