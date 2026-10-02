"""Write the pipeline's results to blob storage.

Writes a single results.json blob to RESULTS_CONTAINER. Uses the same
storage account / credentials pattern as blob_reader.
"""

import json
import logging
import os

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

load_dotenv()
logger = logging.getLogger(__name__)

RESULTS_BLOB_NAME = "results.json"
NEEDS_REVIEW_BLOB_NAME = "needs_review.json"


def _get_service_client() -> BlobServiceClient:
    account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_URL not set in .env")
    return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())


def upload_results(results: list[dict]) -> str:
    """Upload the results list as JSON to RESULTS_CONTAINER/results.json.

    Returns the blob URL on success. Raises on failure -- the orchestrator
    decides whether to treat the upload as fatal or just log it.
    """
    container = os.getenv("RESULTS_CONTAINER")
    if not container:
        raise RuntimeError("RESULTS_CONTAINER not set in .env")

    body = json.dumps(results, indent=2, ensure_ascii=False).encode("utf-8")

    service = _get_service_client()
    blob = service.get_blob_client(container=container, blob=RESULTS_BLOB_NAME)

    logger.info(
        "Uploading results to blob: container=%s blob=%s (%d bytes, %d records)",
        container, RESULTS_BLOB_NAME, len(body), len(results),
    )
    blob.upload_blob(body, overwrite=True, content_type="application/json")
    logger.info("Uploaded results to %s", blob.url)
    return blob.url


def upload_needs_review(needs_review: list[dict]) -> str:
    """Upload the needs-review list as JSON to RESULTS_CONTAINER/needs_review.json.

    Same container and pattern as upload_results, separate blob. Returns the
    blob URL on success; raises on failure (the orchestrator decides whether
    to treat the upload as fatal or just log it).
    """
    container = os.getenv("RESULTS_CONTAINER")
    if not container:
        raise RuntimeError("RESULTS_CONTAINER not set in .env")

    body = json.dumps(needs_review, indent=2, ensure_ascii=False).encode("utf-8")

    service = _get_service_client()
    blob = service.get_blob_client(container=container, blob=NEEDS_REVIEW_BLOB_NAME)

    logger.info(
        "Uploading needs_review to blob: container=%s blob=%s (%d bytes, %d records)",
        container, NEEDS_REVIEW_BLOB_NAME, len(body), len(needs_review),
    )
    blob.upload_blob(body, overwrite=True, content_type="application/json")
    logger.info("Uploaded needs_review to %s", blob.url)
    return blob.url


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()

    sample = [{"student_id": "TEST", "note": "blob_writer smoke test"}]
    url = upload_results(sample)
    print(f"Uploaded: {url}")