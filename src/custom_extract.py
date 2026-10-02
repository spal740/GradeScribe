"""Run the custom Document Intelligence extraction model on one form."""

import os
import logging
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.ai.documentintelligence import DocumentIntelligenceClient

from src.logging_config import setup_logging  # import first (silences Azure noise)
from src.blob_reader import download_blob, list_forms

load_dotenv()
logger = logging.getLogger(__name__)


def extract_form(form_bytes: bytes) -> dict:
    """Run custom extraction model on form bytes, return fields dict.

    Returns: {field_name: {"value": str, "confidence": float}}
    """
    endpoint = os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
    key = os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
    model_id = os.getenv("CUSTOM_MODEL_ID")

    missing = [n for n, v in [
        ("DOCUMENTINTELLIGENCE_ENDPOINT", endpoint),
        ("DOCUMENTINTELLIGENCE_API_KEY", key),
        ("CUSTOM_MODEL_ID", model_id),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    client = DocumentIntelligenceClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
    )

    logger.info("Analyzing form (%d bytes) with model '%s'", len(form_bytes), model_id)
    try:
        poller = client.begin_analyze_document(model_id=model_id, body=form_bytes)
        result = poller.result()
    except HttpResponseError as e:
        logger.error("Document Intelligence request failed: %s", e)
        raise

    fields = {}
    if result.documents:
        for doc in result.documents:
            for name, field in doc.fields.items():
                value = field.get("valueString")
                if value is None:
                    value = field.content
                fields[name] = {
                    "value": value,
                    "confidence": field.confidence,
                }
    else:
        logger.warning("No documents found in extraction result")

    logger.info("Extracted %d fields", len(fields))
    return fields


if __name__ == "__main__":
    setup_logging()
    forms = list_forms()
    if not forms:
        logger.error("No forms found in blob storage.")
        raise SystemExit(1)

    form_name = forms[0]
    container = os.getenv("AZURE_CONTAINER_FORMS", "raw-forms")
    logger.info("Downloading: %s", form_name)

    form_bytes = download_blob(container, form_name)
    fields = extract_form(form_bytes)

    print(f"\n--- Extracted {len(fields)} fields ---")
    for name, info in fields.items():
        print(f"  {name}: {info['value']}  (conf: {info['confidence']:.2f})")