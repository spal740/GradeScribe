"""Load CSR counts, POGS score, and ground-truth grades from the Excel in blob."""

import io
import os
import logging
import pandas as pd
from dotenv import load_dotenv
from src.blob_reader import _get_blob_service

load_dotenv()
logger = logging.getLogger(__name__)


def _read_excel_bytes_from_blob() -> bytes:
    container = os.getenv("AZURE_CONTAINER_DATA", "data")
    try:
        service = _get_blob_service()
        blobs = list(service.get_container_client(container).list_blobs())
    except Exception as e:
        logger.error("Failed to access blob container '%s': %s", container, e)
        raise

    xlsx_blobs = [b for b in blobs if b.name.lower().endswith(".xlsx")]
    if not xlsx_blobs:
        raise RuntimeError(f"No .xlsx file in container '{container}'")

    blob_name = xlsx_blobs[0].name
    logger.info("Reading Excel: %s", blob_name)
    try:
        return service.get_blob_client(
            container=container, blob=blob_name
        ).download_blob().readall()
    except Exception as e:
        logger.error("Failed to download '%s': %s", blob_name, e)
        raise


def read_students(limit: int | None = None) -> list[dict]:
    """Return student dicts: CSR counts + POGS + ground-truth grades.

    Each dict:
      student_id,
      csr {key_excellents_count, some_reservations_count, major_deficiencies_count},
      pogs_score,
      _truth {CSR_Grade, Final_Overall_Grade}
    """
    excel_bytes = _read_excel_bytes_from_blob()
    try:
        df = pd.read_excel(io.BytesIO(excel_bytes))
    except Exception as e:
        logger.error("Failed to parse Excel: %s", e)
        raise

    required = [
        "student id", "NoofExcellentsinkeyfields", "NoofSRsandMDs",
        "NoofMajordefinkeyfields", "POGS_No",
        "Final Grade for Ward", "Final Overall Grade for Run",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Excel missing expected columns: {missing}")

    if limit:
        df = df.head(limit)

    def safe_int(val, default=0):
        try:
            return int(val) if pd.notna(val) else default
        except (ValueError, TypeError):
            return default

    def safe_str(val):
        return str(val).strip() if pd.notna(val) else ""

    def _parse_pogs(val):
        """Parse POGS score, preserving missing as None (not 0).

        A blank POGS must stay None so the payload builder skips the form
        for human review, rather than scoring a missing value as 0 -> Fail.
        A genuine 0 in the Excel still parses as 0 and grades normally.
        """
        if pd.isna(val):
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    students = []
    skipped = 0
    for _, row in df.iterrows():
        sid = safe_str(row.get("student id"))
        if not sid:
            skipped += 1
            continue

        srmd = safe_int(row.get("NoofSRsandMDs"))
        major_def = safe_int(row.get("NoofMajordefinkeyfields"))
        some_res = max(0, srmd - major_def)

        students.append({
            "student_id": sid,
            "csr": {
                "key_excellents_count": safe_int(row.get("NoofExcellentsinkeyfields")),
                "some_reservations_count": some_res,
                "major_deficiencies_count": major_def,
            },
            "pogs_score": _parse_pogs(row.get("POGS_No")),
            "_truth": {
                "CSR_Grade": safe_str(row.get("Final Grade for Ward")),
                "Final_Overall_Grade": safe_str(row.get("Final Overall Grade for Run")),
            },
        })

    logger.info("Loaded %d students (%d rows skipped, no ID)", len(students), skipped)
    return students


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    import json
    students = read_students(limit=5)
    print(json.dumps(students, indent=2))