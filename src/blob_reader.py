"""Read forms and data files from Azure Blob Storage.

Uses Entra ID (DefaultAzureCredential) — requires `az login` on the right tenant.
Required role on the storage account: Storage Blob Data Reader (or Contributor).
"""

import os
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

load_dotenv()


def _get_blob_service() -> BlobServiceClient:
    """Create a BlobServiceClient using Entra ID (CLI login)."""
    account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    if not account_name:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME not set in .env")
    account_url = f"https://{account_name}.blob.core.windows.net"
    credential = DefaultAzureCredential()
    return BlobServiceClient(account_url=account_url, credential=credential)


def list_blobs(container_name: str) -> list[str]:
    service = _get_blob_service()
    container = service.get_container_client(container_name)
    return [blob.name for blob in container.list_blobs()]


def download_blob(container_name: str, blob_name: str) -> bytes:
    service = _get_blob_service()
    blob = service.get_blob_client(container=container_name, blob=blob_name)
    return blob.download_blob().readall()


def list_forms() -> list[str]:
    return list_blobs(os.getenv("AZURE_CONTAINER_FORMS", "raw-forms"))


def list_data_files() -> list[str]:
    return list_blobs(os.getenv("AZURE_CONTAINER_DATA", "data"))


if __name__ == "__main__":
    print("Forms:    ", list_forms())
    print("Data:     ", list_data_files())