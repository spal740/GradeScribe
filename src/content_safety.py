"""Screen form free-text using Azure AI Content Safety.

A separate component from the agent. Called from orchestration AFTER
aggregation: the agent does not see free-text, Content Safety does.

Reads CONTENT_SAFETY_ENDPOINT and CONTENT_SAFETY_KEY from .env. Returns a
flag + the categories that tripped the configured threshold, suitable for
merging into the final structured output.
"""

import logging
import os

from dotenv import load_dotenv
from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError

load_dotenv()
logger = logging.getLogger(__name__)

# Severity threshold for flagging. Azure AI Content Safety returns severity
# 0-7 per category. >=3 catches "approaching moderate" upward; mild casual
# language (severity 1-2) does not trigger.
SEVERITY_THRESHOLD = 3

# Form free-text fields screened by Content Safety. Covers:
#   - overall report comment (page 1)
#   - fitness-to-practise comment (page 2)
#   - all 12 per-domain comments (Clinical/Communication, Personal/Professional,
#     Hauora Maori, Population Health, Applied Science, Self-Management)
#   - MiniCEX free-text (aspects done well, areas for improvement) -- out of
#     scope for grading but still in scope for safety screening
# All fields are concatenated into one string per student.
FREE_TEXT_FIELDS = [
    "overall_comments",
    "fitness_comment",
    # Key CSR domain comments
    "clinical_knowledge_comments",
    "patient_assessment_comments",
    "clinical_decision_comments",
    "communication_comments",
    "engagement_team_comments",
    "professional_qualities_comments",
    # Non-key domain comments
    "commitment_equity_comments",
    "critical_reflection_comments",
    "cultural_safety_comments",
    "disease_prevention_comments",
    "health_promotion_comments",
    "self_management_comments",
    # CAT free-text
    "cat_specific_comments",
    # MiniCEX free-text
    "minicex_aspects_done_well",
    "minicex_areas_for_improvement",
]

# Empty-result default. Returned when there is nothing to screen, or when
# the screening call fails (failure must not block the pipeline).
EMPTY_RESULT: dict = {"flagged": False, "categories": [], "max_severity": 0}


def _get_client() -> ContentSafetyClient:
    endpoint = os.getenv("CONTENT_SAFETY_ENDPOINT")
    key = os.getenv("CONTENT_SAFETY_KEY")
    if not endpoint:
        raise RuntimeError("CONTENT_SAFETY_ENDPOINT not set in .env")
    if not key:
        raise RuntimeError("CONTENT_SAFETY_KEY not set in .env")
    return ContentSafetyClient(endpoint, AzureKeyCredential(key))


def extract_free_text(extracted_fields: dict) -> str:
    """Concatenate the form's free-text fields into a single string.

    Returns "" if no free-text content found across the named fields.
    """
    parts: list[str] = []
    for field_name in FREE_TEXT_FIELDS:
        entry = extracted_fields.get(field_name)
        if entry is None:
            continue
        raw = entry.get("value")
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def check_text(text: str) -> dict:
    """Screen text with Azure AI Content Safety. Returns a result dict.

    Result shape:
      {
        "flagged": bool,
        "categories": [str, ...],
        "max_severity": int,
      }

    Empty text -> EMPTY_RESULT (no API call).
    API failure -> EMPTY_RESULT (logged; does not block the pipeline).
    """
    if not text or not text.strip():
        logger.info("No free-text to screen; returning empty result")
        return dict(EMPTY_RESULT)

    try:
        client = _get_client()
        request = AnalyzeTextOptions(
            text=text,
            categories=[
                TextCategory.HATE,
                TextCategory.SELF_HARM,
                TextCategory.SEXUAL,
                TextCategory.VIOLENCE,
            ],
        )
        response = client.analyze_text(request)
    except HttpResponseError as e:
        logger.exception("Content Safety call failed (HTTP): %s", e)
        return dict(EMPTY_RESULT)
    except Exception:
        logger.exception("Content Safety call failed (unexpected)")
        return dict(EMPTY_RESULT)

    flagged_categories: list[str] = []
    max_severity = 0
    for analysis in response.categories_analysis:
        severity = int(analysis.severity or 0)
        max_severity = max(max_severity, severity)
        if severity >= SEVERITY_THRESHOLD:
            flagged_categories.append(str(analysis.category))

    result = {
        "flagged": len(flagged_categories) > 0,
        "categories": flagged_categories,
        "max_severity": max_severity,
    }
    logger.info(
        "Content Safety result: flagged=%s, categories=%s, max_severity=%d",
        result["flagged"], result["categories"], result["max_severity"],
    )
    return result


if __name__ == "__main__":
    from src.logging_config import setup_logging
    import json
    setup_logging()

    print("--- Empty text ---")
    print(json.dumps(check_text(""), indent=2))

    print("\n--- Benign text ---")
    print(json.dumps(
        check_text("Student demonstrated strong clinical reasoning and worked well in the team."),
        indent=2,
    ))

    print("\n--- Concerning text ---")
    print(json.dumps(
        check_text("Student was aggressive toward team members and used hostile language repeatedly."),
        indent=2,
    ))