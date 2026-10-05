from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PurposeCode = Literal["collect", "instant_report", "internal_improvement", "partner_sharing", "follow_up_contact"]


class NoticePublish(BaseModel):
    version_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    title: str = Field(min_length=2, max_length=200)
    body_digest: str = Field(min_length=16, max_length=128)
    purposes: list[PurposeCode] = Field(min_length=1, max_length=10)
    created_by: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_purposes(self) -> "NoticePublish":
        if len(set(self.purposes)) != len(self.purposes):
            raise ValueError("告知用途不能重复")
        return self


class PurposeChoice(BaseModel):
    purpose: PurposeCode
    granted: bool
    partner_code: str | None = Field(default=None, max_length=64)
    partner_scope: dict[str, Any] | None = None

    @model_validator(mode="after")
    def partner_scope_required(self) -> "PurposeChoice":
        if self.purpose == "partner_sharing":
            if self.granted and not self.partner_code:
                raise ValueError("授权向合作方共享时必须指定 partner_code")
            if not self.granted:
                self.partner_code = None
                self.partner_scope = None
        else:
            self.partner_code = None
            self.partner_scope = None
        return self


class Guardian(BaseModel):
    relation: str = Field(min_length=1, max_length=80)
    basis: str = Field(min_length=2, max_length=500)
    signer_display: str = Field(min_length=1, max_length=120)


class GrantRequest(BaseModel):
    subject_ref: str = Field(min_length=2, max_length=120)
    subject_digest: str = Field(min_length=8, max_length=128)
    signer_type: Literal["self", "guardian"] = "self"
    guardian: Guardian | None = None
    notice_version_code: str = Field(min_length=2, max_length=64)
    product_code: str | None = Field(default=None, max_length=64)
    site_code: str | None = Field(default=None, max_length=64)
    session_reference: str = Field(default="", max_length=120)
    valid_until: datetime | None = None
    purposes: list[PurposeChoice] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @model_validator(mode="after")
    def validate_guardian_and_purposes(self) -> "GrantRequest":
        if self.signer_type == "guardian":
            if self.guardian is None:
                raise ValueError("监护人代签时必须提供关系与法定依据")
        else:
            self.guardian = None
        codes = [choice.purpose for choice in self.purposes]
        if len(set((c.purpose, c.partner_code or "") for c in self.purposes)) != len(self.purposes):
            raise ValueError("同一用途（含合作方）不能重复决定")
        if "collect" in codes:
            collect = next(choice for choice in self.purposes if choice.purpose == "collect")
            if not collect.granted:
                granted_downstream = [c.purpose for c in self.purposes if c.granted and c.purpose != "collect"]
                if granted_downstream:
                    raise ValueError("未授权采集时不能授权下游用途：" + "、".join(granted_downstream))
        return self


class WithdrawRequest(BaseModel):
    grant_key: str = Field(min_length=8, max_length=128)
    purposes: list[PurposeCode] | None = Field(default=None, max_length=10)
    reason: str = Field(min_length=2, max_length=1000)
    actor_name: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @model_validator(mode="after")
    def unique_purposes(self) -> "WithdrawRequest":
        if self.purposes is not None and len(set(self.purposes)) != len(self.purposes):
            raise ValueError("撤回用途不能重复")
        return self


class DispositionClaim(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)
    actor_name: str = Field(min_length=1, max_length=120)
