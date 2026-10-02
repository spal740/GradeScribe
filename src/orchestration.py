"""Pipeline orchestrator.

Per student:
    download -> extract -> build_payload -> score -> aggregate
                                                  -> screen free-text (Content Safety)
                                                  -> merge content_safety + review_required
                                                  -> attach supervisor_comments (passthrough)
                                                  -> attach minicex_form_data (passthrough)
    collect -> write results.json (local + blob)

Content Safety runs as a separate branch off the extracted form's free-text
fields. supervisor_comments and minicex_form_data are pure Python passthroughs
of the form's extracted values -- no agent, no LLM, no screening.
"""

import json
import logging
from pathlib import Path

from src.logging_config import setup_logging
from src.blob_reader import list_forms, download_blob
from src.blob_writer import upload_results, upload_needs_review
from src.excel_reader import read_students
from src.custom_extract import extract_form
from src.payload_builder import build_payload, PayloadError
from src.scoring import score_student
from src.aggregation import setup_aggregator, aggregate_assessment
from src.content_safety import check_text, extract_free_text

logger = logging.getLogger(__name__)

RESULTS_PATH = Path(__file__).parent.parent / "output" / "results.json"
NEEDS_REVIEW_PATH = Path(__file__).parent.parent / "output" / "needs_review.json"
FORMS_CONTAINER = "raw-forms"

CSR_DOMAIN_COMMENT_FIELDS = {
    "clinical_knowledge": "clinical_knowledge_comments",
    "patient_assessment": "patient_assessment_comments",
    "clinical_decision": "clinical_decision_comments",
    "communication": "communication_comments",
    "engagement_team": "engagement_team_comments",
    "professional_qualities": "professional_qualities_comments",
    "commitment_equity": "commitment_equity_comments",
    "critical_reflection": "critical_reflection_comments",
    "cultural_safety": "cultural_safety_comments",
    "disease_prevention": "disease_prevention_comments",
    "health_promotion": "health_promotion_comments",
    "self_management": "self_management_comments",
}

MINICEX_RATING_FIELDS = {
    "history_taking": {
        "Excellent": "history_taking_excellent",
        "Good": "history_taking_good",
        "Some Reservations": "history_taking_some_res",
        "Major Deficiency": "history_taking_major",
    },
    "physical_exam": {
        "Excellent": "physical_exam_excellent",
        "Good": "physical_exam_good",
        "Some Reservations": "physical_exam_some_res",
        "Major Deficiency": "physical_exam_major",
    },
    "clinical_judgement": {
        "Excellent": "clinical_judgement_excellent",
        "Good": "clinical_judgement_good",
        "Some Reservations": "clinical_judgement_some_res",
        "Major Deficiency": "clinical_judgement_major",
    },
    "humanistic": {
        "Excellent": "humanistic_excellent",
        "Good": "humanistic_good",
        "Some Reservations": "humanistic_some_res",
        "Major Deficiency": "humanistic_major",
    },
}


def _agent_student_block(payload: dict) -> dict:
    return {
        "student_id": payload["student_id"],
        "csr": payload["csr"],
        "csr_ratings_per_domain": payload["csr_ratings_per_domain"],
        "cat_score": payload["cat_score"],
        "pogs_score": payload["pogs_score"],
        "fitness_concern": payload["fitness_concern"],
        "fitness_concern_reason": payload["fitness_concern_reason"],
    }


def _text_value(extracted: dict, key: str) -> str:
    entry = extracted.get(key)
    if entry is None:
        return ""
    raw = entry.get("value")
    if raw is None:
        return ""
    return str(raw).strip()


def _is_selected(extracted: dict, key: str) -> bool:
    entry = extracted.get(key)
    if entry is None:
        return False
    raw = entry.get("value")
    return str(raw).strip().lower() == ":selected:" if raw is not None else False


def _report_discussed(extracted: dict) -> bool | None:
    """Returns True/False if the 'discussed with student' checkbox is clear,
    None if neither (or both) are selected (ambiguous extraction).
    """
    yes_sel = _is_selected(extracted, "report_discussed_yes")
    no_sel = _is_selected(extracted, "report_discussed_no")
    if yes_sel and not no_sel:
        return True
    if no_sel and not yes_sel:
        return False
    if yes_sel and no_sel:
        logger.warning("report_discussed_yes AND _no both selected; recording None")
    return None


def _build_supervisor_comments(extracted: dict) -> dict:
    """Passthrough of CSR comments + procedural fields from the form.

    Includes the overall comment, per-domain comments (only non-empty), and
    whether the report was discussed with the student.
    """
    per_domain: dict[str, str] = {}
    for domain, field in CSR_DOMAIN_COMMENT_FIELDS.items():
        text = _text_value(extracted, field)
        if text:
            per_domain[domain] = text

    return {
        "overall": _text_value(extracted, "overall_comments"),
        "per_domain": per_domain,
        "report_discussed_with_student": _report_discussed(extracted),
    }


def _normalise_competence_grade(raw: str) -> str:
    """Normalise the MiniCEX overall competence grade to a bounded value.

    The grade can only validly be one of four values. The extraction model
    sometimes returns corrupt reads for this circled field (e.g.
    'PASS\\nDISTINCTION', '0'). Since the valid set is closed, anything that
    is not exactly one of the four is recorded as 'Unknown' rather than
    passing raw junk downstream. MiniCEX is reference-only (not graded), so
    this affects display only, not any grade.
    """
    valid = {"Pass", "Fail", "Distinction", "Borderline"}
    text = (raw or "").strip()
    # Case-insensitive match, but emit the canonical capitalised form.
    for v in valid:
        if text.lower() == v.lower():
            return v
    return "Unknown"


def _build_minicex_form_data(extracted: dict) -> dict:
    ratings: dict[str, str] = {}
    for domain, boxes in MINICEX_RATING_FIELDS.items():
        selected = [label for label, field in boxes.items()
                    if _is_selected(extracted, field)]
        if len(selected) == 1:
            ratings[domain] = selected[0]
        else:
            ratings[domain] = "Unknown"

    return {
        "_note": (
            "MiniCEX is out of scope for grading by this pipeline. "
            "Form data shown for reference only; the overall_competence_grade "
            "is taken directly from the supervisor's tick on the form."
        ),
        "overall_competence_grade": _normalise_competence_grade(
            _text_value(extracted, "overall_competence_grade")
        ),
        "assessor_name": _text_value(extracted, "minicex_assessor_name"),
        "date": _text_value(extracted, "minicex_date"),
        "setting": _text_value(extracted, "minicex_setting"),
        "patient_age": _text_value(extracted, "minicex_patient_age"),
        "patient_gender": _text_value(extracted, "minicex_patient_gender"),
        "diagnosis": _text_value(extracted, "minicex_diagnosis"),
        "time_observing_minutes": _text_value(extracted, "minicex_time_observing"),
        "ratings": ratings,
        "aspects_done_well": _text_value(extracted, "minicex_aspects_done_well"),
        "areas_for_improvement": _text_value(extracted, "minicex_areas_for_improvement"),
    }


def _apply_content_safety(final: dict, extracted: dict, student_id: str) -> dict:
    free_text = extract_free_text(extracted)
    if not free_text:
        logger.info("No free-text on form for student %s; keeping placeholder",
                    student_id)
        return final

    cs_result = check_text(free_text)
    final["content_safety"] = {
        "flagged": cs_result["flagged"],
        "categories": cs_result["categories"],
    }
    if cs_result["flagged"]:
        final["review_required"] = True
        logger.info("Student %s flagged by Content Safety -> review_required=True",
                    student_id)
    return final


def _process_form(form_name: str, students: list[dict],
                  project, agent_name: str) -> dict:
    logger.info("Processing form: %s", form_name)

    form_bytes = download_blob(FORMS_CONTAINER, form_name)
    logger.info("Downloaded %s (%d bytes)", form_name, len(form_bytes))

    extracted = extract_form(form_bytes)
    logger.info("Extracted %d fields from %s", len(extracted), form_name)

    payload = build_payload(extracted, students)
    student_id = payload["student_id"]

    scored = score_student(payload)
    logger.info("Scored student %s", student_id)

    student_block = _agent_student_block(payload)
    final = aggregate_assessment(project, agent_name, scored, student_block)
    logger.info("Aggregated student %s", student_id)

    final = _apply_content_safety(final, extracted, student_id)
    supervisor_comments = _build_supervisor_comments(extracted)
    minicex_form_data = _build_minicex_form_data(extracted)

    return {
        "student_id": student_id,
        "source_form": form_name,
        "scored": scored,
        "final": final,
        "supervisor_comments": supervisor_comments,
        "cat_comment": _text_value(extracted, "cat_specific_comments"),
        "minicex_form_data": minicex_form_data,        
    }


def run_pipeline() -> list[dict]:
    logger.info("=== Pipeline run starting ===")

    try:
        students = read_students()
    except Exception:
        logger.exception("Could not load students from Excel; aborting run")
        raise
    if not students:
        logger.error("No students loaded from Excel; nothing to process")
        return []
    logger.info("Loaded %d students from Excel", len(students))

    try:
        project, agent_name = setup_aggregator()
    except Exception:
        logger.exception("Could not set up aggregator; aborting run")
        raise

    try:
        forms = list_forms()
    except Exception:
        logger.exception("Could not list forms from blob storage; aborting run")
        raise
    if not forms:
        logger.error("No forms found in blob storage; nothing to process")
        return []
    logger.info("Found %d forms to process", len(forms))

    results: list[dict] = []
    needs_review: list[dict] = []

    for form_name in forms:
        try:
            record = _process_form(form_name, students, project, agent_name)
            results.append(record)
        except PayloadError as e:
            logger.error("Skipping form '%s': %s", form_name, e)
            needs_review.append({
                "source_form": form_name,
                "status": "not_graded",
                "reason": str(e),
                "review_required": True,
            })
        except Exception as e:
            logger.exception("Skipping form '%s': unexpected error", form_name)
            needs_review.append({
                "source_form": form_name,
                "status": "error",
                "reason": str(e),
                "review_required": True,
            })

    logger.info("=== Pipeline run finished: %d graded, %d need review ===",
                len(results), len(needs_review))
    if needs_review:
        logger.warning("Forms needing review: %s",
                       ", ".join(r["source_form"] for r in needs_review))

    _write_results(results)
    _write_needs_review(needs_review)
    _upload_results_to_blob(results)
    _upload_needs_review_to_blob(needs_review)
    return results


def _write_needs_review(needs_review: list[dict]) -> None:
    """Write skipped/failed forms (with reasons) to needs_review.json.

    Written alongside results.json in the same output folder. results.json is
    left untouched (graded forms only); this is a separate file listing forms
    that could not be graded and why.
    """
    try:
        NEEDS_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        NEEDS_REVIEW_PATH.write_text(
            json.dumps(needs_review, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Wrote %d needs-review records locally to %s",
                    len(needs_review), NEEDS_REVIEW_PATH)
    except Exception:
        logger.exception("Failed to write needs_review locally to %s",
                         NEEDS_REVIEW_PATH)


def _write_results(results: list[dict]) -> None:
    try:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Wrote %d results locally to %s", len(results), RESULTS_PATH)
    except Exception:
        logger.exception("Failed to write results locally to %s", RESULTS_PATH)


def _upload_results_to_blob(results: list[dict]) -> None:
    if not results:
        logger.info("No results to upload to blob")
        return
    try:
        upload_results(results)
    except Exception:
        logger.exception("Failed to upload results to blob storage")


def _upload_needs_review_to_blob(needs_review: list[dict]) -> None:
    if not needs_review:
        logger.info("No needs-review records to upload to blob")
        return
    try:
        upload_needs_review(needs_review)
    except Exception:
        logger.exception("Failed to upload needs_review to blob storage")


if __name__ == "__main__":
    setup_logging()
    pipeline_results = run_pipeline()
    print(f"\n=== {len(pipeline_results)} students processed ===")
    print(json.dumps(pipeline_results, indent=2, ensure_ascii=False))