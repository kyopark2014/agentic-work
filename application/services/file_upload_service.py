"""Chat image upload orchestration: validate filename and upload to S3."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from application import utils

logger = logging.getLogger("file_upload_service")

IMAGE_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class FileUploadServiceError(Exception):
    """Business failure while validating or uploading a chat image."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def sanitize_image_filename(filename: str) -> str:
    """Validate image extension and return a collision-safe filename."""
    name = os.path.basename(filename or "").strip()
    if not name:
        raise FileUploadServiceError(400, "File name is required")
    ext = os.path.splitext(name)[1].lower()
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        raise FileUploadServiceError(
            400,
            f"Unsupported image type: {ext or '(none)'}",
        )
    # Avoid collisions when multiple pastes share a generic name
    stem = os.path.splitext(name)[0] or "pasted"
    unique = uuid.uuid4().hex[:10]
    return f"{stem}_{unique}{ext}"


def upload_chat_image(file_bytes: bytes, file_name: str) -> dict[str, Any]:
    """Upload ``file_bytes`` to S3 under images/ for chat attachment.

    Raises:
        FileUploadServiceError: when the file is empty, S3 upload fails, or
            the sharing URL is not configured.
    """
    if not file_bytes:
        raise FileUploadServiceError(400, "Empty file")

    try:
        upload_result = utils.upload_to_s3(file_bytes, file_name)
    except Exception:
        logger.exception("S3 upload failed for file=%s", file_name)
        raise FileUploadServiceError(500, "Failed to upload file to S3") from None
    if not upload_result:
        raise FileUploadServiceError(500, "Failed to upload file to S3")
    if not upload_result.get("url"):
        raise FileUploadServiceError(
            500,
            "File uploaded but sharing URL is not configured",
        )

    logger.info(
        "File upload complete: file=%s s3_key=%s url=%s",
        file_name,
        upload_result.get("s3_key"),
        upload_result.get("url"),
    )

    return {
        "ok": True,
        "file_name": upload_result["file_name"],
        "s3_key": upload_result["s3_key"],
        "url": upload_result["url"],
        "content_type": upload_result.get("content_type"),
    }
