"""RAG upload orchestration: S3 ingest + Knowledge Base sync."""

from __future__ import annotations

import logging
from typing import Any

from application import utils

logger = logging.getLogger("rag_service")


class RagServiceError(Exception):
    """Business failure while uploading or syncing a RAG document."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def ingest_rag_upload(file_bytes: bytes, file_name: str) -> dict[str, Any]:
    """Upload ``file_bytes`` to S3 and start a Knowledge Base sync.

    Raises:
        RagServiceError: when sync status cannot be checked, an ingest is
            already running, S3 upload fails, or KB sync fails.
    """
    try:
        active_job = utils.get_active_ingestion_job()
    except Exception:
        raise RagServiceError(
            503,
            "Unable to check Knowledge Base sync status. Please try again later.",
        ) from None
    if active_job:
        raise RagServiceError(
            409,
            "현재 이전에 업로드된 파일을 처리하고 있습니다. 조금후 다시 시도해주세요.",
        )

    try:
        upload_result = utils.upload_to_s3(file_bytes, file_name)
    except Exception:
        logger.exception("S3 upload failed for file=%s", file_name)
        raise RagServiceError(
            500,
            "Failed to upload file to S3",
        ) from None
    if not upload_result:
        raise RagServiceError(500, "Failed to upload file to S3")

    try:
        sync_result = utils.sync_data_source()
    except Exception:
        logger.exception("Knowledge Base sync failed for file=%s", file_name)
        raise RagServiceError(
            500,
            "File uploaded but Knowledge Base sync failed",
        ) from None
    if not sync_result:
        raise RagServiceError(
            500,
            "File uploaded but Knowledge Base sync failed",
        )

    logger.info(
        "RAG upload complete: file=%s s3_key=%s job=%s",
        file_name,
        upload_result.get("s3_key"),
        sync_result.get("ingestion_job_id"),
    )

    return {
        "ok": True,
        "file_name": upload_result["file_name"],
        "s3_key": upload_result["s3_key"],
        "url": upload_result.get("url"),
        "sync": sync_result,
        "message": (
            f'"{file_name}"가 S3에 업로드 되었고 Knowledge Base와 동기화를 시작합니다.'
        ),
    }
