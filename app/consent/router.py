from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.consent.schemas import (
    ConsentSubmit,
    DerivedRecordRegister,
    NoticeCreate,
    PartnerCreate,
    ProcessingComplete,
    ProcessingIntentRegister,
    WithdrawalSubmit,
)
from app.consent.service import ConsentService
from app.core.security import Principal

router = APIRouter(prefix="/api/consent", tags=["参与者用途授权"])


def service() -> ConsentService:
    return ConsentService()


# ---- 合作方与告知版本（运营/合规维护）----

@router.post("/partners", status_code=201)
def register_partner(payload: PartnerCreate, principal: Principal = Depends(current_principal)):
    principal.require("consent.admin")
    return service().register_partner(payload.model_dump())


@router.get("/partners")
def list_partners(active_only: bool = True, principal: Principal = Depends(current_principal)):
    principal.require("consent.read")
    return {"items": service().list_partners(active_only)}


@router.post("/notices", status_code=201)
def publish_notice(payload: NoticeCreate, principal: Principal = Depends(current_principal)):
    principal.require("consent.admin")
    return service().publish_notice(payload.model_dump())


@router.get("/notices")
def list_notices(notice_code: str | None = None, principal: Principal = Depends(current_principal)):
    principal.require("consent.read")
    return {"items": service().list_notices(notice_code)}


# ---- 参与者签署与撤回 ----

@router.post("/records", status_code=201)
def submit_consent(payload: ConsentSubmit):
    return service().submit_consent(payload.model_dump())


@router.post("/withdrawals", status_code=201)
def withdraw_consent(payload: WithdrawalSubmit):
    return service().withdraw(payload.model_dump())


# ---- 处理闸门（场地设备/处理流水线调用）----

@router.get("/check")
def check_consent(
    participant_digest: str = Query(..., min_length=4, max_length=128),
    purpose_code: str = Query(...),
    partner_code: str = Query(default="", max_length=64),
):
    return service().check_allowed(participant_digest, purpose_code, partner_code)


@router.post("/processing/intents", status_code=202)
def register_processing(payload: ProcessingIntentRegister):
    return service().register_processing(payload.model_dump())


@router.post("/processing/{idempotency_key}/start", status_code=200)
def start_processing(idempotency_key: str):
    return service().start_processing(idempotency_key)


@router.post("/processing/{idempotency_key}/complete", status_code=200)
def complete_processing(idempotency_key: str, payload: ProcessingComplete):
    return service().complete_processing(idempotency_key, payload.model_dump(exclude_none=True))


@router.post("/derived", status_code=201)
def register_derived(payload: DerivedRecordRegister):
    values = payload.model_dump()
    return service().register_derived_record(values)


@router.post("/derived/process-due", status_code=200)
def process_due_derived(principal: Principal = Depends(current_principal)):
    principal.require("consent.admin")
    return service().process_due_derived()


# ---- 查询：对外摘要与审计追溯 ----

@router.get("/participant-summary")
def participant_summary(participant_digest: str = Query(..., min_length=4, max_length=128)):
    return service().participant_summary(participant_digest)


@router.get("/records/{record_id}")
def consent_record_detail(record_id: int, principal: Principal = Depends(current_principal)):
    principal.require("consent.read")
    return service().consent_record_detail(record_id)


@router.get("/withdrawals/{withdrawal_id}")
def withdrawal_detail(withdrawal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("consent.read")
    return service().withdrawal_detail(withdrawal_id)


@router.get("/decisions/trace")
def decision_trace(
    participant_digest: str = Query(..., min_length=4, max_length=128),
    purpose_code: str = Query(...),
    partner_code: str = Query(default="", max_length=64),
    principal: Principal = Depends(current_principal),
):
    principal.require("consent.read")
    return service().decision_trace(participant_digest, purpose_code, partner_code)
