"""Build the per-student scoring payload.

Counts and ratings are derived in Python from the extracted form so they are
deterministic. The LLM (scoring + aggregation) receives clean structured
inputs and is not asked to count or parse raw checkbox data.

Mandatory-field validation (raises PayloadError, form is skipped before any
OpenAI call):
  - student_id present and resolvable to an Excel record
  - cat_score present and parseable from the form
  - pogs_score present in the Excel record

CSR counting rule:
  - key_excellents_count   : Excellents counted from the 6 KEY fields only.
  - some_reservations_count: Some Reservations counted across ALL 12 fields.
  - major_deficiencies_count: Major Deficiencies counted across ALL 12 fields.
  An Excellent in a non-key field is recorded as a rating but does NOT count
  toward key_excellents_count.

CSR quality gating (Unknown domains) is logged as a warning only; partial
CSR data is still graded. Stricter CSR gating is noted as future work.

Payload shape:
  {
    "student_id": str,
    "csr": {
      "key_excellents_count": int,
      "some_reservations_count": int,
      "major_deficiencies_count": int,
    },
    "csr_ratings_per_domain": { <domain>: "Excellent"|"Good"|"Some Reservations"
                                          |"Major Deficiency"|"Not Observed"|"Unknown" },
    "cat_score": int,
    "pogs_score": int,
    "fitness_concern": bool,
    "fitness_concern_reason": str,
  }
"""

import logging

logger = logging.getLogger(__name__)


FIELD_MAP = {
    "student_id": "Student_ID",
    "cat_score": "cat_total_score",
    "fitness_yes": "fitness_to_practise_yes",
    "fitness_no": "fitness_to_practise_no",
    "fitness_comment": "fitness_comment",
}

# The six "key" CSR domains. Excellents are counted from these only.
CSR_KEY_FIELDS = {
    "clinical_knowledge": {
        "excellent": "clinical_knowledge_excellent",
        "good": "clinical_knowledge_good",
        "some_reservations": "clinical_knowledge_reservations",
        "major": "clinical_knowledge_major",
        "not_observed": "clinical_knowledge_not_obs",
    },
    "patient_assessment": {
        "excellent": "patient_assessment_excellent",
        "good": "patient_assessment_good",
        "some_reservations": "patient_assessment_some_res",
        "major": "patient_assessment_major",
        "not_observed": "patient_assessment_not_obs",
    },
    "clinical_decision": {
        "excellent": "clinical_decision_excellent",
        "good": "clinical_decision_good",
        "some_reservations": "clinical_decision_reservations",
        "major": "clinical_decision_major",
        "not_observed": "clinical_decision_not_obs",
    },
    "communication": {
        "excellent": "communication_excellent",
        "good": "communication_good",
        "some_reservations": "communication_reservations",
        "major": "communication_major",
        "not_observed": "communication_not_obs",
    },
    "engagement_team": {
        "excellent": "engagement_team_excellent",
        "good": "engagement_team_good",
        "some_reservations": "engagement_team_reservations",
        "major": "engagement_team_major",
        "not_observed": "engagement_team_not_obs",
    },
    "professional_qualities": {
        "excellent": "professional_qualities_excellent",
        "good": "professional_qualities_good",
        "some_reservations": "professional_qualities_reservations",
        "major": "professional_qualities_major",
        "not_observed": "professional_qualities_not_obs",
    },
}

# The six NON-KEY CSR domains. Only Some Reservations and Major Deficiencies
# are counted from these (Excellents here do NOT count toward key excellents).
CSR_NONKEY_FIELDS = {
    "self_management": {
        "excellent": "self_management_excellent",
        "good": "self_management_good",
        "some_reservations": "self_management_reservations",
        "major": "self_management_major",
        "not_observed": "self_management_not_obs",
    },
    "critical_reflection": {
        "excellent": "critical_reflection_excellent",
        "good": "critical_reflection_good",
        "some_reservations": "critical_reflection_reservations",
        "major": "critical_reflection_major",
        "not_observed": "critical_reflection_not_obs",
    },
    "commitment_equity": {
        "excellent": "commitment_equity_excellent",
        "good": "commitment_equity_good",
        "some_reservations": "commitment_equity_reservations",
        "major": "commitment_equity_major",
        "not_observed": "commitment_equity_not_obs",
    },
    "cultural_safety": {
        "excellent": "cultural_safety_excellent",
        "good": "cultural_safety_good",
        "some_reservations": "cultural_safety_reservations",
        "major": "cultural_safety_major",
        "not_observed": "cultural_safety_not_obs",
    },
    "disease_prevention": {
        "excellent": "disease_prevention_excellent",
        "good": "disease_prevention_good",
        "some_reservations": "disease_prevention_reservations",
        "major": "disease_prevention_major",
        "not_observed": "disease_prevention_not_obs",
    },
    "health_promotion": {
        "excellent": "health_promotion_excellent",
        "good": "health_promotion_good",
        "some_reservations": "health_promotion_reservations",
        "major": "health_promotion_major",
        "not_observed": "health_promotion_not_obs",
    },
}

RATING_LABEL = {
    "excellent": "Excellent",
    "good": "Good",
    "some_reservations": "Some Reservations",
    "major": "Major Deficiency",
    "not_observed": "Not Observed",
}

LOW_CONFIDENCE_THRESHOLD = 0.70


class PayloadError(Exception):
    """Raised when a payload cannot be built or rubric inputs are missing."""


def _get_field(fields: dict, model_key: str) -> dict | None:
    entry = fields.get(model_key)
    if entry is None:
        logger.warning("Extracted fields have no key '%s'", model_key)
        return None
    return entry


def _warn_low_confidence(model_key: str, entry: dict) -> None:
    conf = entry.get("confidence")
    if conf is not None and conf < LOW_CONFIDENCE_THRESHOLD:
        logger.warning(
            "Low confidence (%.2f) for field '%s' -> value %r",
            conf, model_key, entry.get("value"),
        )


def _is_selected(fields: dict, model_key: str) -> bool:
    entry = fields.get(model_key)
    if entry is None:
        return False
    raw = entry.get("value")
    token = str(raw).strip().lower() if raw is not None else ""
    if token == ":selected:":
        return True
    if token == ":unselected:":
        return False
    if raw is not None:
        logger.warning(
            "Checkbox '%s' has unrecognised value %r; treating as unselected",
            model_key, raw,
        )
    return False


def _parse_student_id(fields: dict) -> str:
    entry = _get_field(fields, FIELD_MAP["student_id"])
    if entry is None:
        raise PayloadError(
            f"Student ID field '{FIELD_MAP['student_id']}' not found in extraction"
        )
    _warn_low_confidence(FIELD_MAP["student_id"], entry)
    raw = entry.get("value")
    # Remove ALL whitespace, including internal spaces. Student IDs are digits
    # with no spaces, so a space is always an extraction misread (e.g.
    # "1000 16" -> "100016"); stripping it can only recover the true ID, never
    # corrupt a valid one. Note this does NOT fix misread *characters* (e.g.
    # "10002R"), which remain unresolvable and will fail the Excel lookup.
    sid = "".join(str(raw).split()) if raw is not None else ""
    if not sid:
        raise PayloadError("Extracted student ID is empty")
    return sid


def _parse_cat_score(fields: dict) -> int | None:
    entry = _get_field(fields, FIELD_MAP["cat_score"])
    if entry is None:
        logger.warning("CAT score field absent in extraction")
        return None
    _warn_low_confidence(FIELD_MAP["cat_score"], entry)
    raw = entry.get("value")
    if raw is None:
        return None
    text = str(raw).strip()
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        logger.warning("Could not parse CAT score from %r", raw)
        return None
    score = int(digits)
    if not 0 <= score <= 20:
        logger.warning("CAT score %d outside expected 0-20 range", score)
    return score


def _resolve_domain_rating(fields: dict, boxes: dict, domain: str) -> str | None:
    """Return the single selected rating key for a domain, or None if 0 or 2+
    options are selected (ambiguous -> recorded as Unknown by the caller).
    """
    selected = [
        rating_key for rating_key, field_name in boxes.items()
        if _is_selected(fields, field_name)
    ]
    if len(selected) == 1:
        return selected[0]
    logger.warning(
        "CSR domain '%s' has %d ratings selected (%s); recording Unknown",
        domain, len(selected), selected,
    )
    return None


def _count_csr_and_ratings(fields: dict) -> tuple[dict, dict]:
    """Count CSR categories per the rubric:
      - Excellents: from the 6 KEY fields only.
      - Some Reservations & Major Deficiencies: across ALL 12 fields.
    Returns (counts, ratings_per_domain).
    """
    counts = {
        "key_excellents_count": 0,
        "some_reservations_count": 0,
        "major_deficiencies_count": 0,
    }
    ratings: dict[str, str] = {}

    # 6 KEY fields: count Excellents + Some Reservations + Major Deficiencies
    for domain, boxes in CSR_KEY_FIELDS.items():
        rating_key = _resolve_domain_rating(fields, boxes, domain)
        if rating_key is None:
            ratings[domain] = "Unknown"
            continue
        ratings[domain] = RATING_LABEL[rating_key]
        if rating_key == "excellent":
            counts["key_excellents_count"] += 1
        elif rating_key == "some_reservations":
            counts["some_reservations_count"] += 1
        elif rating_key == "major":
            counts["major_deficiencies_count"] += 1

    # 6 NON-KEY fields: count ONLY Some Reservations + Major Deficiencies
    # (Excellents in non-key fields are recorded but NOT counted).
    for domain, boxes in CSR_NONKEY_FIELDS.items():
        rating_key = _resolve_domain_rating(fields, boxes, domain)
        if rating_key is None:
            ratings[domain] = "Unknown"
            continue
        ratings[domain] = RATING_LABEL[rating_key]
        if rating_key == "some_reservations":
            counts["some_reservations_count"] += 1
        elif rating_key == "major":
            counts["major_deficiencies_count"] += 1

    logger.info("CSR counts: %s", counts)
    logger.info("CSR ratings per domain: %s", ratings)
    return counts, ratings


def _fitness_is_answered(fields: dict) -> bool:
    """True only if exactly one of fitness yes/no is selected.

    Returns False when neither is selected (blank) or both are selected
    (contradictory) -- i.e. the answer is malformed and the form should be
    skipped rather than defaulting to 'no concern'.
    """
    yes_sel = _is_selected(fields, FIELD_MAP["fitness_yes"])
    no_sel = _is_selected(fields, FIELD_MAP["fitness_no"])
    return yes_sel != no_sel  # exactly one selected


def _parse_fitness(fields: dict) -> tuple[bool, str]:
    """Read fitness-to-practise checkboxes and comment.

    Form question: "Any fitness-to-practise concerns? Yes / No"
      - yes selected -> concern = True
      - no  selected -> concern = False
    Defaults to False on ambiguity (both / neither selected), with a warning.
    """
    yes_sel = _is_selected(fields, FIELD_MAP["fitness_yes"])
    no_sel = _is_selected(fields, FIELD_MAP["fitness_no"])

    if yes_sel and no_sel:
        logger.warning(
            "Both fitness_to_practise_yes and _no selected; treating as no concern"
        )
        return False, ""
    if not yes_sel and not no_sel:
        logger.warning(
            "Neither fitness_to_practise_yes nor _no selected; treating as no concern"
        )
        return False, ""

    concern = yes_sel
    reason = ""
    if concern:
        entry = fields.get(FIELD_MAP["fitness_comment"])
        if entry is not None:
            raw = entry.get("value")
            reason = str(raw).strip() if raw is not None else ""
        if not reason:
            logger.warning(
                "fitness_concern is True but fitness_comment is empty"
            )
    return concern, reason


def _index_students(students: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for record in students:
        sid = str(record.get("student_id", "")).strip()
        if not sid:
            continue
        if sid in index:
            logger.warning("Duplicate student_id '%s' in Excel; keeping first", sid)
            continue
        index[sid] = record
    return index


def build_payload(extracted_fields: dict, students: list[dict]) -> dict:
    """Build one student's scoring payload from the form + Excel record.

    Raises PayloadError on any mandatory-field failure: missing student id,
    no Excel match, missing CAT score, missing POGS score. Orchestration
    catches PayloadError and skips the form, avoiding wasted OpenAI calls.
    """
    student_id = _parse_student_id(extracted_fields)
    logger.info("Building payload for student '%s'", student_id)

    index = _index_students(students)
    record = index.get(student_id)
    if record is None:
        raise PayloadError(
            f"No Excel record for student_id '{student_id}' "
            f"(Excel has {len(index)} students)"
        )

    csr_counts, csr_ratings = _count_csr_and_ratings(extracted_fields)
    cat_score = _parse_cat_score(extracted_fields)
    pogs_score = record.get("pogs_score")

    # Mandatory rubric inputs -- skip the form (no OpenAI call) if missing.
    if cat_score is None:
        raise PayloadError(
            f"Cannot score student '{student_id}': "
            f"cat_score missing or unparseable from form"
        )
    if pogs_score is None:
        raise PayloadError(
            f"Cannot score student '{student_id}': "
            f"pogs_score missing from Excel record"
        )

    # CSR completeness -- every one of the 12 criteria must be read cleanly
    # (exactly one option selected). A domain that is blank (zero selected) or
    # ambiguous (two or more selected) is recorded as "Unknown" by
    # _count_csr_and_ratings; if any are Unknown we cannot grade the form, so
    # skip it for human review rather than grading around the gap.
    unknown_domains = [d for d, r in csr_ratings.items() if r == "Unknown"]
    if unknown_domains:
        raise PayloadError(
            f"Cannot score student '{student_id}': CSR criteria not read "
            f"cleanly (blank or multiple options selected): "
            f"{', '.join(unknown_domains)}"
        )

    # Fitness-to-practise must be answered (exactly one of yes/no selected).
    # A blank or contradictory (both selected) answer is malformed; we skip
    # rather than defaulting to 'no concern' on a safety-critical field.
    if not _fitness_is_answered(extracted_fields):
        raise PayloadError(
            f"Cannot score student '{student_id}': fitness-to-practise not "
            f"answered cleanly (neither or both of yes/no selected)"
        )

    fitness_concern, fitness_reason = _parse_fitness(extracted_fields)

    payload = {
        "student_id": student_id,
        "csr": csr_counts,
        "csr_ratings_per_domain": csr_ratings,
        "cat_score": cat_score,
        "pogs_score": pogs_score,
        "fitness_concern": fitness_concern,
        "fitness_concern_reason": fitness_reason,
    }

    logger.info(
        "Payload built for '%s': csr=%s, cat=%s, pogs=%s, fitness_concern=%s",
        student_id, csr_counts, cat_score, pogs_score, fitness_concern,
    )
    return payload


if __name__ == "__main__":
    from src.logging_config import setup_logging
    import json

    setup_logging()

    def cb(v: bool):
        return {"value": ":selected:" if v else ":unselected:",
                "confidence": 0.96 if v else 0.10}

    fake_fields = {
        FIELD_MAP["student_id"]: {"value": "100011", "confidence": 0.89},
        FIELD_MAP["cat_score"]: {"value": "20", "confidence": 0.93},
        FIELD_MAP["fitness_yes"]: cb(False),
        FIELD_MAP["fitness_no"]: cb(True),
        FIELD_MAP["fitness_comment"]: {"value": None, "confidence": 0.99},
        "clinical_knowledge_excellent": cb(True),
        "clinical_knowledge_good": cb(False),
        "clinical_knowledge_reservations": cb(False),
        "clinical_knowledge_major": cb(False),
        "clinical_knowledge_not_obs": cb(False),
        "patient_assessment_excellent": cb(True),
        "patient_assessment_good": cb(False),
        "patient_assessment_some_res": cb(False),
        "patient_assessment_major": cb(False),
        "patient_assessment_not_obs": cb(False),
        "clinical_decision_excellent": cb(False),
        "clinical_decision_good": cb(False),
        "clinical_decision_reservations": cb(True),
        "clinical_decision_major": cb(False),
        "clinical_decision_not_obs": cb(False),
        "communication_excellent": cb(True),
        "communication_good": cb(False),
        "communication_reservations": cb(False),
        "communication_major": cb(False),
        "communication_not_obs": cb(False),
        "engagement_team_excellent": cb(True),
        "engagement_team_good": cb(False),
        "engagement_team_reservations": cb(False),
        "engagement_team_major": cb(False),
        "engagement_team_not_obs": cb(False),
        "professional_qualities_excellent": cb(True),
        "professional_qualities_good": cb(False),
        "professional_qualities_reservations": cb(False),
        "professional_qualities_major": cb(False),
        "professional_qualities_not_obs": cb(False),
        # Non-key domains (example: one major deficiency in self_management)
        "self_management_excellent": cb(False),
        "self_management_good": cb(False),
        "self_management_reservations": cb(False),
        "self_management_major": cb(True),
        "self_management_not_obs": cb(False),
        "critical_reflection_excellent": cb(True),
        "critical_reflection_good": cb(False),
        "critical_reflection_reservations": cb(False),
        "critical_reflection_major": cb(False),
        "critical_reflection_not_obs": cb(False),
        "commitment_equity_excellent": cb(False),
        "commitment_equity_good": cb(True),
        "commitment_equity_reservations": cb(False),
        "commitment_equity_major": cb(False),
        "commitment_equity_not_obs": cb(False),
        "cultural_safety_excellent": cb(False),
        "cultural_safety_good": cb(True),
        "cultural_safety_reservations": cb(False),
        "cultural_safety_major": cb(False),
        "cultural_safety_not_obs": cb(False),
        "disease_prevention_excellent": cb(False),
        "disease_prevention_good": cb(True),
        "disease_prevention_reservations": cb(False),
        "disease_prevention_major": cb(False),
        "disease_prevention_not_obs": cb(False),
        "health_promotion_excellent": cb(False),
        "health_promotion_good": cb(True),
        "health_promotion_reservations": cb(False),
        "health_promotion_major": cb(False),
        "health_promotion_not_obs": cb(False),
    }
    fake_students = [{
        "student_id": "100011",
        "pogs_score": 10,
        "_truth": {},
    }]
    print(json.dumps(build_payload(fake_fields, fake_students), indent=2))