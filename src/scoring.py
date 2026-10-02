"""Apply rubric rules to a student's data using Azure OpenAI.

Uses Structured Outputs (json_schema, strict mode) so the model is constrained
to produce JSON matching the SCORING_SCHEMA exactly. Grade values are
constrained to the four-value enum at the API level (not only via the prompt).
"""

import json
import logging
from pathlib import Path

from src.client import get_openai_client, get_deployment_name

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "scoring_prompt.md"

# JSON Schema for the scoring response. Used with Azure OpenAI Structured
# Outputs to enforce the exact response shape and grade vocabulary at the API
# level. If a grade label or field name changes, update both the schema below
# and scoring_prompt.md.
GRADE_ENUM = ["Distinction", "Pass", "Borderline", "Fail"]

SCORING_SCHEMA = {
    "name": "scoring_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "student_id",
            "CSR_Grade", "CSR_reasoning",
            "CAT_Grade", "CAT_reasoning",
            "POGS_Grade", "POGS_reasoning",
        ],
        "properties": {
            "student_id": {"type": "string"},
            "CSR_Grade": {"type": "string", "enum": GRADE_ENUM},
            "CSR_reasoning": {"type": "string"},
            "CAT_Grade": {"type": "string", "enum": GRADE_ENUM},
            "CAT_reasoning": {"type": "string"},
            "POGS_Grade": {"type": "string", "enum": GRADE_ENUM},
            "POGS_reasoning": {"type": "string"},
        },
    },
}


def score_student(student_data: dict) -> dict:
    """Send student data + rubric to Azure OpenAI, return scored JSON."""
    student_id = student_data.get("student_id", "<unknown>")
    logger.info("Scoring student %s", student_id)

    try:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Scoring prompt not found at %s", PROMPT_PATH)
        raise

    client = get_openai_client()
    try:
        response = client.chat.completions.create(
            model=get_deployment_name(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(student_data, indent=2)},
            ],
            response_format={"type": "json_schema", "json_schema": SCORING_SCHEMA},
        )
    except Exception:
        logger.exception("Scoring call failed for student %s", student_id)
        raise

    content = response.choices[0].message.content
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        logger.exception(
            "Scoring returned non-JSON for student %s; raw text: %r",
            student_id, content,
        )
        raise

    logger.info(
        "Scored student %s -> CSR=%s, CAT=%s, POGS=%s",
        student_id, result.get("CSR_Grade"),
        result.get("CAT_Grade"), result.get("POGS_Grade"),
    )
    return result


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()

    mock_path = Path(__file__).parent.parent / "samples" / "mock_extraction.json"
    with open(mock_path, encoding="utf-8") as f:
        student = json.load(f)

    print("Input:")
    print(json.dumps(student, indent=2))
    print()

    result = score_student(student)

    print("Scored output:")
    print(json.dumps(result, indent=2))