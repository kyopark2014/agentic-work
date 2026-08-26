"""Chat image / Load-files upload orchestration."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from application import utils

logger = logging.getLogger("file_upload_service")

IMAGE_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Documents attached via "Load files" (agent reads under /mnt/workspace/.../upload/).
LOAD_FILE_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".html",
    ".htm",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".yml",
    ".yaml",
    ".xml",
    ".rst",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
}

WORKSPACE_MOUNT_PATH = "/mnt/workspace"
UPLOAD_SUBDIR = "upload"


class FileUploadServiceError(Exception):
    """Business failure while validating or uploading a chat file."""

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


def sanitize_load_filename(filename: str) -> str:
    """Validate Load-files extension and return a safe basename (overwrite-safe)."""
    name = os.path.basename(filename or "").strip() or "upload.bin"
    if name in {".", ".."} or "/" in name or "\\" in name:
        name = "upload.bin"
    ext = os.path.splitext(name)[1].lower()
    if ext not in LOAD_FILE_ALLOWED_EXTENSIONS:
        raise FileUploadServiceError(
            400,
            f"Unsupported file type: {ext or '(none)'}",
        )
    return name


def workspace_upload_path(user_id: str | None, file_name: str) -> str:
    """Runtime path mapped from agentcore-sessions/{user}/upload/{file}."""
    segment = utils.sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name)
    return f"{WORKSPACE_MOUNT_PATH}/{segment}/{UPLOAD_SUBDIR}/{safe_name}"


def upload_chat_image(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Upload ``file_bytes`` to S3 under images/{user_id}/ for chat attachment.

    Raises:
        FileUploadServiceError: when the file is empty, S3 upload fails, or
            the sharing URL is not configured.
    """
    if not file_bytes:
        raise FileUploadServiceError(400, "Empty file")

    try:
        upload_result = utils.upload_to_s3(file_bytes, file_name, user_id=user_id)
    except Exception:
        logger.exception("S3 upload failed for file=%s user=%s", file_name, user_id)
        raise FileUploadServiceError(500, "Failed to upload file to S3") from None
    if not upload_result:
        raise FileUploadServiceError(500, "Failed to upload file to S3")
    if not upload_result.get("url"):
        raise FileUploadServiceError(
            500,
            "File uploaded but sharing URL is not configured",
        )

    logger.info(
        "File upload complete: user=%s file=%s s3_key=%s url=%s",
        user_id,
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


# Single PUT max object size on S3 is 5 GiB; keep a hard cap for safety.
MAX_LOAD_FILE_BYTES = 5 * 1024 * 1024 * 1024


def _assert_load_file_size(size: int | None) -> None:
    if size is None:
        return
    if size < 0:
        raise FileUploadServiceError(400, "Invalid file size")
    if size == 0:
        raise FileUploadServiceError(400, "Empty file")
    if size > MAX_LOAD_FILE_BYTES:
        raise FileUploadServiceError(400, "File exceeds the 5 GiB upload limit")


def _expected_session_upload_key(user_id: str | None, file_name: str) -> str:
    return utils.session_upload_s3_key(file_name, user_id=user_id)


def create_load_file_presign(
    file_name: str,
    user_id: str | None = None,
    *,
    size: int | None = None,
) -> dict[str, Any]:
    """Issue a short-lived S3 PUT URL for a Load-files attachment.

    The browser uploads directly to S3 (bypassing ECS/ALB body limits).
    """
    safe_name = sanitize_load_filename(file_name)
    _assert_load_file_size(size)

    try:
        presign = utils.generate_session_upload_presigned_put(
            safe_name, user_id=user_id
        )
    except Exception:
        logger.exception(
            "Presign failed for file=%s user=%s", safe_name, user_id
        )
        raise FileUploadServiceError(500, "Failed to create upload URL") from None
    if not presign or not presign.get("upload_url"):
        raise FileUploadServiceError(500, "Failed to create upload URL")

    workspace_path = workspace_upload_path(user_id, safe_name)
    logger.info(
        "Load-file presign: user=%s file=%s s3_key=%s size=%s",
        user_id,
        safe_name,
        presign.get("s3_key"),
        size,
    )
    return {
        "ok": True,
        "file_name": safe_name,
        "s3_key": presign["s3_key"],
        "workspace_path": workspace_path,
        "content_type": presign.get("content_type"),
        "upload_url": presign["upload_url"],
        "headers": presign.get("headers") or {},
        "expires_in": presign.get("expires_in"),
    }


def complete_load_file_upload(
    file_name: str,
    s3_key: str,
    user_id: str | None = None,
    *,
    size: int | None = None,
) -> dict[str, Any]:
    """Verify a browser PUT landed in the user's upload prefix and wait for mount."""
    safe_name = sanitize_load_filename(file_name)
    _assert_load_file_size(size)

    expected_key = _expected_session_upload_key(user_id, safe_name)
    key = (s3_key or "").strip()
    if key != expected_key:
        raise FileUploadServiceError(400, "Invalid upload target")

    head = utils.head_session_upload_object(key)
    if not head:
        raise FileUploadServiceError(404, "Uploaded object not found")
    content_length = int(head.get("content_length") or 0)
    if content_length <= 0:
        raise FileUploadServiceError(400, "Empty file")
    if size is not None and content_length != size:
        raise FileUploadServiceError(
            400,
            f"Uploaded size mismatch (expected {size}, got {content_length})",
        )

    workspace_path = workspace_upload_path(user_id, safe_name)
    mount_ready = utils.wait_for_workspace_file(
        workspace_path,
        expected_size=content_length,
        timeout_sec=90.0,
        interval_sec=0.5,
    )

    logger.info(
        "Load-file upload complete: user=%s file=%s s3_key=%s workspace=%s mount_ready=%s",
        user_id,
        safe_name,
        key,
        workspace_path,
        mount_ready,
    )

    return {
        "ok": True,
        "file_name": safe_name,
        "s3_key": key,
        "workspace_path": workspace_path,
        "content_type": head.get("content_type"),
        "mount_ready": mount_ready,
    }


def upload_load_file(
    file_bytes: bytes,
    file_name: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Upload a Load-files attachment under agentcore-sessions/{user}/upload/.

    AgentCore Runtime mounts that prefix at /mnt/workspace, so the agent receives
    ``/mnt/workspace/{user}/upload/{file_name}``.

    When ``/mnt/workspace`` is mounted in this process, waits until the object is
    visible there before returning (S3 Files eventual consistency).

    Prefer :func:`create_load_file_presign` + browser PUT for large files so the
    request body does not traverse ECS/ALB.
    """
    if not file_bytes:
        raise FileUploadServiceError(400, "Empty file")
    _assert_load_file_size(len(file_bytes))

    try:
        upload_result = utils.upload_to_session_upload(
            file_bytes, file_name, user_id=user_id
        )
    except Exception:
        logger.exception(
            "Session upload failed for file=%s user=%s", file_name, user_id
        )
        raise FileUploadServiceError(500, "Failed to upload file to S3") from None
    if not upload_result:
        raise FileUploadServiceError(500, "Failed to upload file to S3")

    workspace_path = workspace_upload_path(user_id, upload_result["file_name"])
    mount_ready = utils.wait_for_workspace_file(
        workspace_path,
        expected_size=len(file_bytes),
        timeout_sec=90.0,
        interval_sec=0.5,
    )

    logger.info(
        "Load-file upload complete: user=%s file=%s s3_key=%s workspace=%s mount_ready=%s",
        user_id,
        file_name,
        upload_result.get("s3_key"),
        workspace_path,
        mount_ready,
    )

    return {
        "ok": True,
        "file_name": upload_result["file_name"],
        "s3_key": upload_result["s3_key"],
        "workspace_path": workspace_path,
        "content_type": upload_result.get("content_type"),
        "mount_ready": mount_ready,
    }
