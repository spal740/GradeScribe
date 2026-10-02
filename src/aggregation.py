"""Aggregate component grades into a final assessment using a Foundry Agent.

Uses Structured Outputs (json_schema, strict) on the agent definition so the
agent's response is constrained to AGGREGATION_SCHEMA at the API level.

Split into:
  - setup_aggregator()  -> creates project client + agent ONCE. Call once.
  - aggregate_assessment(project, agent_name, scored, student) -> dict.
    Per-student invoke. Call inside the loop.
"""

import json
import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)

load_dotenv()
logger = logging.getLogger(__name__)

AGENT_NAME = "mbchb-aggregator"
INSTRUCTIONS_PATH = Path(__file__).parent.parent / "prompts" / "aggregation_prompt.md"

GRADE_ENUM = ["Distinction", "Pass", "Borderline", "Fail"]
DOMAIN_RATING_ENUM = [
    "Excellent", "Good", "Some Reservations",
    "Major Deficiency", "Not Observed", "Unknown",
]

# JSON Schema constraining the aggregation agent's structured output.
# Mirrors the OUTPUT section of prompts/aggregation_prompt.md. If a field is
# added/renamed in the prompt, update here and the prompt together.
AGGREGATION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "student_id",
        "csr_ratings_per_domain",
        "csr_counts",
        "cat_score",
        "pogs_score",
        "component_grades",
        "Final_Overall_Grade",
        "Final_reasoning",
        "fitness_to_practise",
        "escalation",
        "escalation_reasons",
        "review_required",
        "content_safety",
    ],
    "properties": {
        "student_id": {"type": "string"},
        "csr_ratings_per_domain": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "clinical_knowledge", "patient_assessment", "clinical_decision",
                "communication", "engagement_team", "professional_qualities",
            ],
            "properties": {
                "clinical_knowledge": {"type": "string", "enum": DOMAIN_RATING_ENUM},
                "patient_assessment": {"type": "string", "enum": DOMAIN_RATING_ENUM},
                "clinical_decision": {"type": "string", "enum": DOMAIN_RATING_ENUM},
                "communication": {"type": "string", "enum": DOMAIN_RATING_ENUM},
                "engagement_team": {"type": "string", "enum": DOMAIN_RATING_ENUM},
                "professional_qualities": {"type": "string", "enum": DOMAIN_RATING_ENUM},
            },
        },
        "csr_counts": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "key_excellents_count",
                "some_reservations_count",
                "major_deficiencies_count",
            ],
            "properties": {
                "key_excellents_count": {"type": "integer"},
                "some_reservations_count": {"type": "integer"},
                "major_deficiencies_count": {"type": "integer"},
            },
        },
        "cat_score": {"type": "integer"},
        "pogs_score": {"type": "integer"},
        "component_grades": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "CSR_Grade", "CSR_reasoning",
                "CAT_Grade", "CAT_reasoning",
                "POGS_Grade", "POGS_reasoning",
            ],
            "properties": {
                "CSR_Grade": {"type": "string", "enum": GRADE_ENUM},
                "CSR_reasoning": {"type": "string"},
                "CAT_Grade": {"type": "string", "enum": GRADE_ENUM},
                "CAT_reasoning": {"type": "string"},
                "POGS_Grade": {"type": "string", "enum": GRADE_ENUM},
                "POGS_reasoning": {"type": "string"},
            },
        },
        "Final_Overall_Grade": {"type": "string", "enum": GRADE_ENUM},
        "Final_reasoning": {"type": "string"},
        "fitness_to_practise": {
            "type": "object",
            "additionalProperties": False,
            "required": ["concern", "reason"],
            "properties": {
                "concern": {"type": "boolean"},
                "reason": {"type": "string"},
            },
        },
        "escalation": {"type": "boolean"},
        "escalation_reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "review_required": {"type": "boolean"},
        "content_safety": {
            "type": "object",
            "additionalProperties": False,
            "required": ["flagged", "categories"],
            "properties": {
                "flagged": {"type": "boolean"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    },
}


def _get_project_client() -> AIProjectClient:
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT not set in .env")
    return AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )


def _ensure_agent_exists(project: AIProjectClient) -> str:
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not deployment:
        raise RuntimeError("AZURE_OPENAI_DEPLOYMENT not set in .env")

    try:
        instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Aggregation prompt not found at %s", INSTRUCTIONS_PATH)
        raise

    logger.info("Ensuring aggregator agent '%s' exists (deployment: %s)",
                AGENT_NAME, deployment)

    response_format = TextResponseFormatJsonSchema(
        name="aggregation_result",
        schema=AGGREGATION_SCHEMA,
        strict=True,
    )
    text_options = PromptAgentDefinitionTextOptions(format=response_format)

    try:
        agent = project.agents.create_version(
            agent_name=AGENT_NAME,
            definition=PromptAgentDefinition(
                model=deployment,
                instructions=instructions,
                text=text_options,
            ),
        )
    except Exception:
        logger.exception("Failed to create/update agent '%s'", AGENT_NAME)
        raise

    logger.info("Aggregator agent ready: %s", agent.name)
    return agent.name


def setup_aggregator() -> tuple[AIProjectClient, str]:
    """Create project client and ensure aggregator agent exists. Call ONCE."""
    logger.info("Setting up aggregator (once)")
    project = _get_project_client()
    agent_name = _ensure_agent_exists(project)
    return project, agent_name


def aggregate_assessment(
    project: AIProjectClient,
    agent_name: str,
    scored: dict,
    student: dict,
) -> dict:
    """Per-student aggregation. Returns the agent's structured output dict."""
    student_id = scored.get("student_id") or student.get("student_id")
    logger.info("Aggregating student %s", student_id)

    openai_client = project.get_openai_client()

    payload = {"scored": scored, "student": student}
    payload_json = json.dumps(payload, indent=2)

    try:
        conversation = openai_client.conversations.create(
            items=[{"type": "message", "role": "user", "content": payload_json}]
        )
        response = openai_client.responses.create(
            conversation=conversation.id,
            input=(
                "Apply the rubric rules to the input above and return the "
                "final assessment JSON. Return ONLY the JSON object, no commentary."
            ),
            extra_body={
                "agent_reference": {
                    "name": agent_name,
                    "type": "agent_reference",
                }
            },
        )
    except Exception:
        logger.exception("Aggregator call failed for student %s", student_id)
        raise

    text_parts = []
    for item in response.output:
        if item.type == "message":
            for block in item.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
    full_text = "".join(text_parts).strip()

    if full_text.startswith("```"):
        lines = full_text.split("\n")
        full_text = "\n".join(lines[1:-1]).strip()

    try:
        result = json.loads(full_text)
    except json.JSONDecodeError:
        logger.exception(
            "Aggregator returned non-JSON for student %s; raw text: %r",
            student_id, full_text,
        )
        raise

    logger.info(
        "Aggregated student %s -> Final=%s, escalation=%s",
        student_id, result.get("Final_Overall_Grade"), result.get("escalation"),
    )
    return result


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()

    project, agent_name = setup_aggregator()

    fake_scored = {
        "student_id": "100099",
        "CSR_Grade": "Pass",
        "CSR_reasoning": "key_excellents=6, some_reservations=0, major_deficiencies=0 -> Pass.",
        "CAT_Grade": "Pass",
        "CAT_reasoning": "cat_score=18 -> 14-18 range -> Pass.",
        "POGS_Grade": "Distinction",
        "POGS_reasoning": "pogs_score=10 -> Distinction.",
    }
    fake_student = {
        "student_id": "100099",
        "csr": {"key_excellents_count": 6, "some_reservations_count": 0, "major_deficiencies_count": 0},
        "csr_ratings_per_domain": {
            "clinical_knowledge": "Excellent",
            "patient_assessment": "Excellent",
            "clinical_decision": "Excellent",
            "communication": "Excellent",
            "engagement_team": "Excellent",
            "professional_qualities": "Excellent",
        },
        "cat_score": 18,
        "pogs_score": 10,
        "fitness_concern": False,
        "fitness_concern_reason": "",
    }
    result = aggregate_assessment(project, agent_name, fake_scored, fake_student)
    print(json.dumps(result, indent=2, ensure_ascii=False))