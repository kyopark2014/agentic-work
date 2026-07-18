import logging
import os

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from application.api.routes_auth import require_user_id
from application import utils

logger = logging.getLogger("routes_rag")

router = APIRouter(prefix="/api/rag", tags=["rag"])

RAG_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
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
}


def _validate_filename(filename: str) -> str:
    name = os.path.basename(filename or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="File name is required")
    ext = os.path.splitext(name)[1].lower()
    if ext not in RAG_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'}",
        )
    return name


@router.post("/upload")
async def upload_to_rag(request: Request, file: UploadFile = File(...)):
    require_user_id(request)

    file_name = _validate_filename(file.filename or "")
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        active_job = utils.get_active_ingestion_job()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Unable to check Knowledge Base sync status. Please try again later.",
        )
    if active_job:
        raise HTTPException(
            status_code=409,
            detail="현재 이전에 업로드된 파일을 처리하고 있습니다. 조금후 다시 시도해주세요.",
        )

    upload_result = utils.upload_to_s3(file_bytes, file_name)
    if not upload_result:
        raise HTTPException(status_code=500, detail="Failed to upload file to S3")

    sync_result = utils.sync_data_source()
    if not sync_result:
        raise HTTPException(
            status_code=500,
            detail="File uploaded but Knowledge Base sync failed",
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
