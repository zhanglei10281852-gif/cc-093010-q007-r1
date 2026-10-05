from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.consent.schemas import DispositionClaim, GrantRequest, NoticePublish, WithdrawRequest
from app.consent.service import ConsentService
from app.core.security import Principal

router = APIRouter(prefix="/api/consent", tags=["用途授权"])


def service() -> ConsentService:
    return ConsentService()


@router.post("/notices", status_code=201)
def publish_notice(payload: NoticePublish):
    return service().publish_notice(payload.model_dump())


@router.get("/notices")
def list_notices():
    return {"items": service().list_notices()}


@router.post("/grants", status_code=201)
def record_grant(payload: GrantRequest):
    return service().record_grant(payload.model_dump())


@router.post("/withdrawals")
def withdraw(payload: WithdrawRequest):
    return service().withdraw(payload.model_dump())


@router.get("/subjects/{subject_digest}/summary")
def subject_summary(subject_digest: str):
    """对外查询：仅返回各用途最新状态、依据告知版本与有效期，不返回身份明细。"""
    return service().subject_summary(subject_digest)


@router.get("/grants/{grant_key}")
def grant_detail(grant_key: str, principal: Principal = Depends(current_principal)):
    """审计入口：从一次允许或拒绝追到告知版本、授权主体、用途决定与后续处置。"""
    principal.require("consent.read")
    return service().grant_for_key(grant_key)


@router.get("/dispositions")
def list_pending_dispositions(principal: Principal = Depends(current_principal)):
    principal.require("consent.read")
    return {"queued": service().pending_dispositions()}


@router.post("/dispositions/run")
def run_dispositions(payload: DispositionClaim, principal: Principal = Depends(current_principal)):
    principal.require("consent.dispose")
    return service().run_dispositions(payload.limit, payload.actor_name)


@router.post("/expiry/run")
def run_expiry(principal: Principal = Depends(current_principal)):
    principal.require("consent.dispose")
    return service().expire_due_grants()
