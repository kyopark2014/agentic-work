from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from application.api.routes_auth import require_user_id
from application.services.file_upload_service import (
    FileUploadServiceError,
    sanitize_image_filename,
    upload_chat_image,
)

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Upload an image to S3 (images/) for chat attachment. No Knowledge Base sync."""
    require_user_id(request)

    try:
        file_name = sanitize_image_filename(file.filename or "pasted.png")
        file_bytes = await file.read()
        return upload_chat_image(file_bytes, file_name)
    except FileUploadServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
