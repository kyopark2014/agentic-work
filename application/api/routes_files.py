from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from application.api.routes_auth import require_user_id
from application.services.file_upload_service import (
    FileUploadServiceError,
    sanitize_image_filename,
    sanitize_load_filename,
    upload_chat_image,
    upload_load_file,
)

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Upload an image to S3 (images/{user_id}/) for chat attachment. No Knowledge Base sync."""
    user_id = require_user_id(request)

    try:
        file_name = sanitize_image_filename(file.filename or "pasted.png")
        file_bytes = await file.read()
        return upload_chat_image(file_bytes, file_name, user_id=user_id)
    except FileUploadServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/load")
async def load_file(request: Request, file: UploadFile = File(...)):
    """Upload a Load-files attachment under agentcore-sessions/{user}/upload/.

    Returns ``workspace_path`` (``/mnt/workspace/{user}/upload/{name}``) for the
    agent payload — not a CloudFront URL.
    """
    user_id = require_user_id(request)

    try:
        file_name = sanitize_load_filename(file.filename or "upload.bin")
        file_bytes = await file.read()
        return upload_load_file(file_bytes, file_name, user_id=user_id)
    except FileUploadServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
