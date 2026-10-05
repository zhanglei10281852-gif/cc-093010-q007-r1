from __future__ import annotations

import json
import sqlite3

from app.catalog.repository import CatalogRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


class CatalogService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CatalogRepository(self.connection)

    def create_product(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.product_by_code(code):
            raise ConflictError("产品编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "organization": data["organization"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_product(payload, to_storage(self.clock.now()))

    def update_product(self, code: str, changes: dict) -> dict:
        product = self.repository.product_by_code(code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的产品字段")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).update_product(product["id"], values, to_storage(self.clock.now()))

    def list_products(self, category: str | None, status: str | None, active_only: bool, limit: int) -> list[dict]:
        return self.repository.list_products(category=category, status=status, active_only=active_only, limit=limit)

    def create_site(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.site_by_code(code):
            raise ConflictError("场地编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "region": data["region"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_site(payload, to_storage(self.clock.now()))

    def update_site(self, code: str, changes: dict) -> dict:
        site = self.repository.site_by_code(code)
        if site is None:
            raise NotFoundError("试点场地不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的场地字段")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).update_site(site["id"], values, to_storage(self.clock.now()))

    def list_sites(self, status: str | None, site_type: str | None, capability: str | None) -> list[dict]:
        return self.repository.list_sites(status=status, site_type=site_type, capability=capability)

    def submit_evidence(self, data: dict) -> dict:
        product = self.repository.product_by_code(data["product_code"])
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        duplicate = self.repository.evidence_duplicate(product["id"], data["evidence_type"], data["version"], data["content_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_evidence(product["id"], data, to_storage(self.clock.now()))

    def review_evidence(self, evidence_id: int, reviewer: str, decision: str, note: str) -> dict:
        evidence = self.repository.evidence_by_id(evidence_id)
        if evidence is None:
            raise NotFoundError("证据材料不存在")
        if evidence["status"] != "submitted":
            raise ConflictError("只有待审阅材料可以作出决定")
        if decision == "rejected" and len(note.strip()) < 4:
            raise ValidationError("驳回时需要说明可执行的原因")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).review_evidence(evidence_id, reviewer.strip(), decision, to_storage(self.clock.now()))

    def list_evidence(self, product_code: str | None, status: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.list_evidence(product_id=product_id, status=status)

    def submit_feedback(self, data: dict) -> dict:
        from app.consent.service import ConsentService

        product = self.repository.product_by_code(data["product_code"])
        if product is None or not product["active"]:
            raise NotFoundError("可体验的健康创新产品不存在")
        site = self.repository.site_by_code(data["site_code"])
        if site is None or site["status"] != "active":
            raise NotFoundError("可用的试点场地不存在")
        duplicate = self.repository.feedback_duplicate(site["id"], data["session_reference"], data["audience_type"], data["contact_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            consent = ConsentService(connection, self.clock)
            now = to_storage(self.clock.now())
            grant_id = None
            subject_digest = (data.get("subject_digest") or "").strip()
            if subject_digest:
                improvement = consent.effective_decision(
                    subject_digest=subject_digest, purpose="internal_improvement",
                    product_id=product["id"], connection=connection,
                )
                if not improvement["permitted"]:
                    raise ConflictError(
                        "参与者未授权将体验反馈用于内部产品改进",
                        context={"reason": improvement["reason"]},
                    )
                grant_id = improvement["grant_id"]
                if data["consent_to_follow_up"]:
                    follow_up = consent.effective_decision(
                        subject_digest=subject_digest, purpose="follow_up_contact",
                        product_id=product["id"], connection=connection,
                    )
                    if not follow_up["permitted"]:
                        raise ConflictError(
                            "参与者未授权后续联系", context={"reason": follow_up["reason"]}
                        )
            feedback = catalog.create_feedback(product["id"], site["id"], data, now, consent_grant_id=grant_id)
            if grant_id:
                consent.register_activity(
                    grant_id=grant_id, purpose="internal_improvement", resource_type="feedback",
                    resource_id=str(feedback["id"]), product_id=product["id"], site_id=site["id"],
                    session_reference=data["session_reference"], status="completed", connection=connection, now=now,
                )
                if data["consent_to_follow_up"]:
                    consent.register_activity(
                        grant_id=grant_id, purpose="follow_up_contact", resource_type="feedback",
                        resource_id=str(feedback["id"]), product_id=product["id"], site_id=site["id"],
                        session_reference=data["session_reference"], status="in_progress", connection=connection, now=now,
                    )
            return feedback

    def feedback_summary(self, product_code: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.feedback_summary(product_id)

    def export_for_partner(self, product_code: str, partner_code: str) -> dict:
        from app.consent.service import ConsentService

        product = self.repository.product_by_code(product_code)
        if product is None or not product["active"]:
            raise NotFoundError("健康创新产品不存在")
        partner_code = partner_code.strip()
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            consent = ConsentService(connection, self.clock)
            now = to_storage(self.clock.now())
            exported: list[dict] = []
            notice_basis: set[str] = set()
            rows = connection.execute(
                "SELECT f.id,f.audience_type,f.rating,f.tags_json,f.consent_grant_id,g.subject_digest "
                "FROM public_feedback f JOIN consent_grants g ON g.id=f.consent_grant_id "
                "WHERE f.product_id=? AND f.disposition_status='active' ORDER BY f.id",
                (product["id"],),
            ).fetchall()
            for row in rows:
                decision = consent.effective_decision(
                    subject_digest=row["subject_digest"], purpose="partner_sharing",
                    product_id=product["id"], partner_code=partner_code, connection=connection,
                )
                if not decision["permitted"]:
                    continue
                notice_basis.add(decision["notice_version_code"])
                consent.register_activity(
                    grant_id=decision["grant_id"], purpose="partner_sharing", resource_type="feedback",
                    resource_id=str(row["id"]), product_id=product["id"], site_id=None,
                    session_reference="", status="completed", connection=connection, now=now,
                )
                exported.append({
                    "feedback_id": row["id"], "audience_type": row["audience_type"],
                    "rating": row["rating"], "tags": json.loads(row["tags_json"]),
                })
            return {"product_code": product_code, "partner_code": partner_code,
                    "notice_basis": sorted(notice_basis),
                    "items": exported, "exported_at": now}

