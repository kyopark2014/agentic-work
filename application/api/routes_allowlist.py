"""Allowlist management API (registered Google accounts) — admin only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from application.api import routes_admin as admin
from application import allow_list

router = APIRouter(prefix="/api/allowlist", tags=["allowlist"])


class AllowlistResponse(BaseModel):
    ids: list[str]
    admin_ids: list[str] = Field(default_factory=list)


class AllowlistAddRequest(BaseModel):
    id: str = Field(..., min_length=3, max_length=254, description="Google email")


class AllowlistDeleteRequest(BaseModel):
    id: str = Field(..., min_length=3, max_length=254)


def _response(ids: list[str]) -> AllowlistResponse:
    return AllowlistResponse(
        ids=ids,
        admin_ids=sorted(admin.get_admin_emails()),
    )


@router.get("", response_model=AllowlistResponse)
def get_allowlist(request: Request) -> AllowlistResponse:
    admin.require_admin(request)
    return _response(allow_list.list_allowed_ids())


@router.post("", response_model=AllowlistResponse)
def add_allowlist_entry(
    body: AllowlistAddRequest, request: Request
) -> AllowlistResponse:
    admin.require_admin(request)
    return _response(allow_list.add_allowed_id(body.id))


@router.delete("", response_model=AllowlistResponse)
def remove_allowlist_entry(
    body: AllowlistDeleteRequest, request: Request
) -> AllowlistResponse:
    actor = admin.require_admin(request)
    target = allow_list.normalize_email(body.id)
    if admin.is_admin_user(target):
        raise HTTPException(
            status_code=400,
            detail="마스터 계정은 등록 목록에서 삭제할 수 없습니다.",
        )
    return _response(allow_list.remove_allowed_id(body.id, actor=actor))
