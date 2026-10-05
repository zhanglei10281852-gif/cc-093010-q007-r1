from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.consent.purposes import PURPOSES


class PartnerCreate(BaseModel):
    partner_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    partner_type: Literal["医院", "康复机构", "研究机构", "产业伙伴"]
    region: str = Field(default="", max_length=120)
    retention_days: int | None = Field(default=None, ge=1, le=36500)


class NoticeCreate(BaseModel):
    notice_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=200)
    content_hash: str = Field(min_length=16, max_length=128)
    content_excerpt: str = Field(default="", max_length=1000)
    purposes: list[str] = Field(min_length=1, max_length=20)
    data_categories: list[str] = Field(default_factory=list, max_length=50)
    partners: list[str] = Field(default_factory=list, max_length=100)
    created_by: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_purposes(self) -> "NoticeCreate":
        unknown = sorted(set(self.purposes) - set(PURPOSES))
        if unknown:
            raise ValueError(f"告知包含未注册的用途：{', '.join(unknown)}")
        if len(set(self.purposes)) != len(self.purposes):
            raise ValueError("告知用途不能重复")
        return self


class GuardianSignature(BaseModel):
    relation: str = Field(min_length=2, max_length=60)
    basis: str = Field(min_length=2, max_length=300)
    guardian_digest: str = Field(min_length=4, max_length=128)


class PurposeDecisionInput(BaseModel):
    purpose_code: str
    decision: Literal["allow", "deny"]
    partner_code: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_purpose(self) -> "PurposeDecisionInput":
        if self.purpose_code not in PURPOSES:
            raise ValueError(f"未注册的用途：{self.purpose_code}")
        return self


class ConsentSubmit(BaseModel):
    participant_digest: str = Field(min_length=4, max_length=128)
    site_code: str = Field(min_length=2, max_length=64)
    session_reference: str = Field(min_length=2, max_length=120)
    subject_type: Literal["self", "guardian"] = "self"
    guardian: GuardianSignature | None = None
    notice_code: str = Field(min_length=2, max_length=64)
    decisions: list[PurposeDecisionInput] = Field(min_length=1, max_length=50)
    validity_days: int = Field(default=365, ge=1, le=3650)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @model_validator(mode="after")
    def validate_guardian(self) -> "ConsentSubmit":
        if self.subject_type == "guardian" and self.guardian is None:
            raise ValueError("监护人代签时必须提供关系与依据")
        if self.subject_type == "self" and self.guardian is not None:
            raise ValueError("本人签署时不能附带监护人信息")
        return self


class WithdrawalSubmit(BaseModel):
    participant_digest: str = Field(min_length=4, max_length=128)
    purpose_code: str | None = None
    partner_code: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @model_validator(mode="after")
    def validate_target(self) -> "WithdrawalSubmit":
        if self.purpose_code is not None and self.purpose_code not in PURPOSES:
            raise ValueError(f"未注册的用途：{self.purpose_code}")
        if self.partner_code and self.purpose_code != "partner_sharing":
            raise ValueError("只有向合作方共享用途可以指定合作方")
        return self


class ProcessingIntentRegister(BaseModel):
    participant_digest: str = Field(min_length=4, max_length=128)
    purpose_code: str
    partner_code: str = Field(default="", max_length=64)
    site_code: str = Field(min_length=2, max_length=64)
    session_reference: str = Field(min_length=2, max_length=120)
    record_type: str = Field(default="", max_length=80)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @model_validator(mode="after")
    def validate_purpose(self) -> "ProcessingIntentRegister":
        if self.purpose_code not in PURPOSES:
            raise ValueError(f"未注册的用途：{self.purpose_code}")
        return self


class DerivedRecordRegister(BaseModel):
    participant_digest: str = Field(min_length=4, max_length=128)
    source_purpose_code: str
    source_partner_code: str = Field(default="", max_length=64)
    record_type: str = Field(min_length=2, max_length=80)
    record_ref: str = Field(min_length=2, max_length=160)
    notice_version: int = Field(ge=1)
    retention_basis: str = Field(default="", max_length=200)
    retention_until: str | None = None

    @model_validator(mode="after")
    def validate_purpose(self) -> "DerivedRecordRegister":
        if self.source_purpose_code not in PURPOSES:
            raise ValueError(f"未注册的用途：{self.source_purpose_code}")
        return self


class ProcessingComplete(BaseModel):
    derived_record_type: str = Field(default="", max_length=80)
    derived_record_ref: str = Field(default="", max_length=160)
    retention_basis: str = Field(default="", max_length=200)
    retention_until: str | None = None

    @model_validator(mode="after")
    def validate_derived_pair(self) -> "ProcessingComplete":
        if bool(self.derived_record_type) != bool(self.derived_record_ref):
            raise ValueError("派生记录类型与引用必须同时提供")
        return self
