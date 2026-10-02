"""Shared logging setup for the grading pipeline.

Call setup_logging() once at the start of any entry-point file. It:
  - shows INFO logs from our own code (clear, useful progress messages)
  - silences the chatty Azure SDK libraries (only WARNING and above)

Every module gets its logger with:  logger = logging.getLogger(__name__)
"""

import logging


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the whole pipeline. Call once, at startup."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Azure SDK libraries log every HTTP request at INFO -- far too noisy.
    # Raise them to WARNING so they only speak up on real problems.
    for noisy in (
        "azure",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "azure.storage",
        "urllib3",
        "openai",
        "httpx",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)