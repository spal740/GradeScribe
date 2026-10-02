"""Reusable factory for the Azure OpenAI client.

Loads config from .env so every module gets a consistent client
without copy-pasting setup code.
"""

import os
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()


def get_openai_client() -> AzureOpenAI:
    """Return an AzureOpenAI client configured from .env."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION")

    missing = [
        name for name, val in [
            ("AZURE_OPENAI_ENDPOINT", endpoint),
            ("AZURE_OPENAI_API_KEY", api_key),
            ("AZURE_OPENAI_API_VERSION", api_version),
        ] if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
    )


def get_deployment_name() -> str:
    """Return the GPT deployment name from .env."""
    name = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not name:
        raise RuntimeError("AZURE_OPENAI_DEPLOYMENT not set in .env")
    return name