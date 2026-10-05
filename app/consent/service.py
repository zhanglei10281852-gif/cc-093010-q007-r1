from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.consent.purposes import (
    DEFAULT_DERIVED_DISPOSITION,
    PURPOSE_PARTNER_SHARING,
    PURPOSE_REQUIRES,
)
from app.consent.repository import ConsentRepository
from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.repositories.audit import AuditRepository


PROCESSING_SCOPE = "consent-processing"


def evaluate_gate(connection: sqlite3.Connection, participant_digest: str, purpose_code: str, partner_code: str = "", now: str | None = None) -> dict[str, Any]:
    """在调用方事务内评估授权闸门，不自行开启事务。"""
    repository = ConsentRepository(connection)
    moment = now or to_storage(SystemClock().now())
    repository.expire_current_decisions(moment)
    current = repository.current_decision(participant_digest, purpose_code, partner_code)
    allowed, reason = ConsentService._gate(repository, current, purpose_code, partner_code)
    return {
        "allowed": allowed,
        "reason": reason,
        "purpose_code": purpose_code,
        "partner_code": partner_code or None,
        "notice_version": current["notice_version"] if current else None,
        "valid_until": current["valid_until"] if current else None,
        "record_id": current["record_id"] if current else None,
    }


class ConsentService:
    """可版本化的用途授权：告知版本、分用途决定、监护人代签、有效期、撤回与派生记录处置。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ConsentRepository(self.connection)

    # ---- 告知版本与合作方 ----

    def register_partner(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = payload["partner_code"].strip().lower()
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            if repository.partner_by_code(code):
                raise ConflictError("合作方编码已存在")
            partner = repository.create_partner({**payload, "partner_code": code}, now)
            self._audit(connection, "consent.partner_register", "consent_partner", partner["id"], after=partner)
            return partner

    def list_partners(self, active_only: bool = True) -> list[dict[str, Any]]:
        return self.repository.list_partners(active_only)

    def publish_notice(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = payload["notice_code"].strip().lower()
        purposes = list(dict.fromkeys(payload["purposes"]))
        partners = list(dict.fromkeys(payload.get("partners", [])))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            if PURPOSE_PARTNER_SHARING in purposes and not partners:
                raise ValidationError("告知包含向合作方共享用途时，必须至少列出一家合作方")
            for partner_code in partners:
                partner = repository.partner_by_code(partner_code)
                if partner is None or partner["status"] != "active":
                    raise ValidationError(f"告知引用了不可用的合作方：{partner_code}")
            previous = repository.latest_notice(code)
            version = (int(previous["version"]) + 1) if previous else 1
            if previous is not None:
                new_purposes = set(purposes) - set(json.loads(previous["purposes_json"]))
                if not new_purposes and payload["content_hash"] == previous["content_hash"]:
                    raise ConflictError("告知内容与用途未变化，不需要发布新版本")
            repository.deprecate_active_notices(code)
            data = {**payload, "notice_code": code, "version": version, "purposes": purposes, "partners": partners}
            notice = repository.create_notice(data, now)
            self._audit(
                connection, "consent.notice_publish", "consent_notice", notice["id"],
                before={"status": previous["status"] if previous else None, "version": previous["version"] if previous else None},
                after={"notice_code": code, "version": version, "purposes": purposes, "partners": partners},
            )
            return notice

    def list_notices(self, notice_code: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_notices(notice_code)

    # ---- 授权签署 ----

    def submit_consent(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = request_fingerprint(payload)
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)

            existing = repository.consent_by_idempotency(payload["idempotency_key"])
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise ConflictError("同一授权幂等键不能用于不同请求（疑似跨场地或修改后重放）")
                return self._consent_result(repository, existing, replayed=True)

            notice = repository.active_notice(payload["notice_code"])
            if notice is None:
                raise NotFoundError("当前没有生效的告知版本")
            site = connection.execute("SELECT status FROM pilot_sites WHERE code=?", (payload["site_code"],)).fetchone()
            if site is None:
                raise NotFoundError("授权关联的场地不存在")
            if site["status"] != "active":
                raise ConflictError("授权关联的场地当前不可用")
            notice_purposes = json.loads(notice["purposes_json"])
            notice_partners = json.loads(notice["partners_json"])

            keys = self._validate_decisions(payload["decisions"], notice_purposes, notice_partners)

            starts = now
            expires = to_storage(now_value + timedelta(days=int(payload["validity_days"])))
            record_data = {
                "participant_digest": payload["participant_digest"],
                "subject_type": payload["subject_type"],
                "guardian_relation": (payload.get("guardian") or {}).get("relation"),
                "guardian_basis": (payload.get("guardian") or {}).get("basis"),
                "guardian_digest": (payload.get("guardian") or {}).get("guardian_digest"),
                "site_code": payload["site_code"],
                "session_reference": payload["session_reference"],
                "notice_id": notice["id"],
                "notice_code": notice["notice_code"],
                "notice_version": notice["version"],
                "idempotency_key": payload["idempotency_key"],
                "validity_starts_at": starts,
                "validity_expires_at": expires,
            }
            record_id = repository.insert_consent(record_data, fingerprint, now)

            for (purpose_code, partner_code), decision in keys.items():
                repository.insert_decision(
                    record_id=record_id, participant_digest=payload["participant_digest"],
                    purpose_code=purpose_code, partner_code=partner_code, decision=decision,
                    notice_version=notice["version"], now=now,
                )
                seq = repository.next_decision_seq(payload["participant_digest"], purpose_code, partner_code)
                repository.upsert_current_decision(
                    participant_digest=payload["participant_digest"], purpose_code=purpose_code,
                    partner_code=partner_code, decision=decision, notice_id=notice["id"],
                    notice_version=notice["version"], record_id=record_id, seq=seq,
                    valid_from=starts, valid_until=expires, now=now,
                )

            record = repository.consent_by_id(record_id)
            self._audit(
                connection, "consent.submit", "consent_record", record_id,
                after={"notice": f"{notice['notice_code']}@v{notice['version']}", "site_code": payload["site_code"],
                       "session_reference": payload["session_reference"], "subject_type": payload["subject_type"],
                       "decisions": [{"purpose": p, "partner": pc or None, "decision": d} for (p, pc), d in keys.items()]},
                metadata={"participant_digest": payload["participant_digest"],
                          "guardian_relation": record_data["guardian_relation"]},
            )
            return self._consent_result(repository, record, replayed=False)

    def _validate_decisions(self, supplied: list[dict[str, Any]], notice_purposes: list[str], notice_partners: list[str]) -> dict[tuple[str, str], str]:
        keys: dict[tuple[str, str], str] = {}
        for item in supplied:
            purpose = item["purpose_code"]
            partner = item.get("partner_code") or ""
            if purpose not in notice_purposes:
                raise ValidationError(f"用途 {purpose} 不在当前告知版本中，不能依据旧告知作出新用途决定")
            if purpose == PURPOSE_PARTNER_SHARING:
                if not partner or partner not in notice_partners:
                    raise ValidationError("向合作方共享必须逐家指定告知中列出的合作方")
            elif partner:
                raise ValidationError(f"用途 {purpose} 不能指定合作方")
            pair = (purpose, partner)
            if pair in keys:
                raise ValidationError(f"用途 {purpose} 对同一合作方存在重复决定")
            keys[pair] = item["decision"]

        expected: set[tuple[str, str]] = set()
        for purpose in notice_purposes:
            if purpose == PURPOSE_PARTNER_SHARING:
                expected.update((purpose, code) for code in notice_partners)
            else:
                expected.add((purpose, ""))
        missing = sorted(expected - keys.keys())
        if missing:
            raise ValidationError("以下用途缺少逐项决定", context={"missing": [{"purpose": p, "partner": pc or None} for p, pc in missing]})
        extra = set(keys.keys()) - expected
        if extra:
            raise ValidationError("存在告知范围之外的决定")

        allowed_pairs = {(purpose, partner) for (purpose, partner), decision in keys.items() if decision == "allow"}
        for purpose, partner in allowed_pairs:
            for prerequisite in PURPOSE_REQUIRES.get(purpose, ()):
                if (prerequisite, "") not in allowed_pairs:
                    target = f"向合作方 {partner} 共享" if partner else f"用途 {purpose}"
                    raise ValidationError(f"允许{target}前必须先允许前置用途：{prerequisite}")
        return keys

    # ---- 撤回 ----

    def withdraw(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = request_fingerprint(payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)

            existing = repository.withdrawal_by_idempotency(payload["idempotency_key"])
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise ConflictError("同一撤回幂等键不能用于不同请求")
                return self._withdrawal_result(repository, existing, replayed=True)

            withdrawal_id = repository.insert_withdrawal(
                participant_digest=payload["participant_digest"], purpose_code=payload.get("purpose_code"),
                partner_code=payload.get("partner_code") or "", reason=payload.get("reason") or "",
                idempotency_key=payload["idempotency_key"], fingerprint=fingerprint, now=now,
            )

            decisions = repository.active_decisions_for_withdrawal(
                payload["participant_digest"], payload.get("purpose_code"), payload.get("partner_code") or ""
            )
            stopped = 0
            queued = 0
            for current in decisions:
                repository.mark_decision_withdrawn(current["id"], now)
                repository.insert_withdrawal_decision(withdrawal_id=withdrawal_id, snapshot={
                    "record_id": current["record_id"], "participant_digest": current["participant_digest"],
                    "purpose_code": current["purpose_code"], "partner_code": current["partner_code"],
                    "decision": current["decision"], "notice_version": current["notice_version"], "seq": current["seq"],
                }, now=now)
                stopped += repository.stop_pending_intents(
                    participant_digest=current["participant_digest"], purpose_code=current["purpose_code"],
                    partner_code=current["partner_code"], withdrawal_id=withdrawal_id, now=now,
                )
                queued += self._queue_derived_for_withdrawal(repository, current, withdrawal_id, now)

            repository.finalize_withdrawal(withdrawal_id, affected=len(decisions), stopped=stopped, queued=queued, now=now)
            self._audit(
                connection, "consent.withdraw", "consent_withdrawal", withdrawal_id,
                after={"purpose_code": payload.get("purpose_code"), "partner_code": payload.get("partner_code") or None,
                       "affected_decisions": len(decisions), "stopped_intents": stopped, "queued_derived": queued},
                metadata={"participant_digest": payload["participant_digest"], "reason": payload.get("reason") or ""},
            )
            withdrawal = repository.withdrawal_by_id(withdrawal_id)
            return self._withdrawal_result(repository, withdrawal, replayed=False)

    def _queue_derived_for_withdrawal(self, repository: ConsentRepository, current: dict[str, Any], withdrawal_id: int, now: str) -> int:
        derived_rows = repository.derived_for_withdrawal(
            current["participant_digest"], current["purpose_code"], current["partner_code"]
        )
        now_dt = from_storage(now)
        count = 0
        for derived in derived_rows:
            action, due_at, reason = self._disposition(derived, now_dt)
            if action == "retain" and due_at is None and derived["status"] == "retained":
                continue
            repository.queue_derived(derived["id"], action=action, due_at=due_at, reason=reason, withdrawal_id=withdrawal_id, now=now)
            count += 1
        return count

    @staticmethod
    def _disposition(derived: dict[str, Any], now_dt) -> tuple[str, str | None, str]:
        basis = (derived.get("retention_basis") or "").strip()
        until_text = derived.get("retention_until")
        until = from_storage(until_text) if until_text else None
        if basis and until is not None and until > now_dt:
            return "retain", to_storage(until), f"保存义务未到期：{basis}"
        purpose = derived["source_purpose_code"]
        default = DEFAULT_DERIVED_DISPOSITION.get(purpose, "delete")
        if default == "delete":
            return "delete", to_storage(now_dt), "撤回且无未履行的保存义务"
        if default == "quarantine":
            due = to_storage(until) if until is not None and until > now_dt else None
            return "quarantine", due, "撤回后隔离，暂停内部使用"
        return "retain", None, "撤回后保留禁联/义务所需最小摘要"

    def process_due_derived(self) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)
            moved_to_delete = 0
            for derived in repository.derived_due_for_transition(now):
                repository.transition_to_delete_pending(derived["id"], now)
                moved_to_delete += 1
            deleted = 0
            for derived in repository.due_derived(now):
                repository.mark_derived_deleted(derived["id"], now)
                deleted += 1
                self._audit(
                    connection, "consent.derived_delete", "consent_derived_record", derived["id"],
                    before={"status": "delete_pending"}, after={"status": "deleted", "record_type": derived["record_type"]},
                    metadata={"participant_digest": derived["participant_digest"], "source_purpose_code": derived["source_purpose_code"]},
                )
        return {"moved_to_delete_queue": moved_to_delete, "deleted": deleted, "processed_at": now}

    # ---- 授权闸门与处理意图 ----

    def check_allowed(self, participant_digest: str, purpose_code: str, partner_code: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)
            current = repository.current_decision(participant_digest, purpose_code, partner_code)
            allowed, reason = self._gate(repository, current, purpose_code, partner_code)
            return {
                "allowed": allowed,
                "reason": reason,
                "purpose_code": purpose_code,
                "partner_code": partner_code or None,
                "notice_version": current["notice_version"] if current else None,
                "valid_until": current["valid_until"] if current else None,
                "record_id": current["record_id"] if current else None,
            }

    @staticmethod
    def _gate(repository: ConsentRepository, current: dict[str, Any] | None, purpose_code: str, partner_code: str) -> tuple[bool, str]:
        if current is None:
            return False, "从未就此用途作出决定"
        if current["status"] != "active":
            return False, f"授权状态为 {current['status']}"
        if current["decision"] != "allow":
            return False, "参与者已明确拒绝"
        if purpose_code == PURPOSE_PARTNER_SHARING and partner_code:
            partner = repository.partner_by_code(partner_code)
            if partner is None or partner["status"] != "active":
                return False, "合作方已不可用"
        return True, "ok"

    def register_processing(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        fingerprint = request_fingerprint(payload)
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)
            existing = repository.intent_by_idempotency(payload["idempotency_key"])
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise ConflictError("同一处理幂等键不能用于不同请求（疑似跨场地重放）")
                return self._intent_view(repository, existing, replayed=True)
            intent = repository.register_intent(payload, fingerprint, now)
            return self._intent_view(repository, intent, replayed=False)

    def start_processing(self, idempotency_key: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)
            intent = repository.intent_by_idempotency(idempotency_key)
            if intent is None:
                raise NotFoundError("处理意图不存在，需要先登记")
            if intent["status"] == "stopped":
                raise ConflictError("处理已被撤回停止，不能开始", context={"withdrawal_id": intent["stopped_by_withdrawal_id"]})
            if intent["status"] != "scheduled":
                return self._intent_view(repository, intent, replayed=True)
            current = repository.current_decision(intent["participant_digest"], intent["purpose_code"], intent["partner_code"])
            allowed, reason = self._gate(repository, current, intent["purpose_code"], intent["partner_code"])
            if not allowed:
                raise ConflictError("授权闸门未通过，处理不能开始", context={"reason": reason})
            repository.mark_intent_running(intent["id"], now)
            view = self._intent_view(repository, repository.intent_by_idempotency(idempotency_key), replayed=False)
            self._audit(connection, "consent.processing_start", "consent_processing_intent", intent["id"],
                        after={"purpose_code": intent["purpose_code"], "partner_code": intent["partner_code"] or None,
                               "site_code": intent["site_code"], "notice_version": current["notice_version"]},
                        metadata={"participant_digest": intent["participant_digest"]})
            return view

    def complete_processing(self, idempotency_key: str, derived: dict[str, Any] | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            intent = repository.intent_by_idempotency(idempotency_key)
            if intent is None:
                raise NotFoundError("处理意图不存在")
            if intent["status"] != "running":
                raise ConflictError(f"处理意图状态为 {intent['status']}，不能完成")
            repository.mark_intent_done(intent["id"], now)
            derived_view = None
            if derived and derived.get("derived_record_ref"):
                current = repository.current_decision(intent["participant_digest"], intent["purpose_code"], intent["partner_code"])
                payload = {
                    "participant_digest": intent["participant_digest"],
                    "source_purpose_code": intent["purpose_code"],
                    "source_partner_code": intent["partner_code"],
                    "record_type": derived.get("derived_record_type") or "derived_record",
                    "record_ref": derived["derived_record_ref"],
                    "notice_version": current["notice_version"] if current else 1,
                    "retention_basis": derived.get("retention_basis") or "",
                    "retention_until": derived.get("retention_until"),
                }
                derived_view = repository.register_derived(payload, now)
            self._audit(connection, "consent.processing_complete", "consent_processing_intent", intent["id"],
                        after={"status": "done", "derived_ref": derived_view["record_ref"] if derived_view else None},
                        metadata={"participant_digest": intent["participant_digest"]})
            view = self._intent_view(repository, repository.intent_by_idempotency(idempotency_key), replayed=False)
            view["derived_record"] = derived_view
            return view

    # ---- 派生记录登记（处理流水线或既有系统回填）----

    def register_derived_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            existing = repository.derived_by_ref(payload["record_type"], payload["record_ref"])
            view = repository.register_derived(payload, now)
            self._audit(
                connection, "consent.derived_register", "consent_derived_record", view["id"],
                after={"record_type": payload["record_type"], "record_ref": payload["record_ref"],
                       "source_purpose_code": payload["source_purpose_code"],
                       "source_partner_code": payload.get("source_partner_code") or None,
                       "replayed": existing is not None},
                metadata={"participant_digest": payload["participant_digest"]},
            )
            return view

    # ---- 查询：对外摘要与审计追溯 ----

    def participant_summary(self, participant_digest: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            repository.expire_current_decisions(now)
            rows = connection.execute(
                "SELECT purpose_code,partner_code,decision,status,notice_version,valid_from,valid_until,record_id "
                "FROM consent_current_decisions WHERE participant_digest=? ORDER BY purpose_code,partner_code",
                (participant_digest,),
            ).fetchall()
            return {
                "participant_ref": participant_digest[:12],
                "checked_at": now,
                "purposes": [
                    {"purpose_code": row["purpose_code"], "partner_code": row["partner_code"] or None,
                     "decision": row["decision"], "status": row["status"], "notice_version": row["notice_version"],
                     "valid_until": row["valid_until"], "record_id": row["record_id"]}
                    for row in rows
                ],
            }

    def consent_record_detail(self, record_id: int) -> dict[str, Any]:
        record = self.repository.consent_by_id(record_id)
        if record is None:
            raise NotFoundError("授权记录不存在")
        immutable = self.repository.decisions_of_record(record_id)
        current = self.repository.current_decisions_for_records([record_id])
        result = dict(record)
        result["decisions"] = [
            {"purpose_code": row["purpose_code"], "partner_code": row["partner_code"] or None,
             "decision": row["decision"], "notice_version": row["notice_version"], "created_at": row["created_at"]}
            for row in immutable
        ]
        result["current_state"] = [
            {"purpose_code": row["purpose_code"], "partner_code": row["partner_code"] or None,
             "decision": row["decision"], "status": row["status"], "seq": row["seq"],
             "notice_version": row["notice_version"], "record_id": row["record_id"]}
            for row in current
        ]
        return result

    def withdrawal_detail(self, withdrawal_id: int) -> dict[str, Any]:
        withdrawal = self.repository.withdrawal_by_id(withdrawal_id)
        if withdrawal is None:
            raise NotFoundError("撤回记录不存在")
        snapshots = self.repository.withdrawal_decisions(withdrawal_id)
        participant = withdrawal["participant_digest"]
        derived = [
            row for row in self.repository.derived_for_participant(participant)
            if row["withdrawal_id"] == withdrawal_id
        ]
        stopped = self.connection.execute(
            "SELECT id,purpose_code,partner_code,site_code,session_reference,record_type,status FROM consent_processing_intents "
            "WHERE stopped_by_withdrawal_id=? ORDER BY id", (withdrawal_id,),
        ).fetchall()
        result = dict(withdrawal)
        result["decision_snapshots"] = [dict(row) for row in snapshots]
        result["derived_dispositions"] = derived
        result["stopped_intents"] = [dict(row) for row in stopped]
        return result

    def decision_trace(self, participant_digest: str, purpose_code: str, partner_code: str = "") -> dict[str, Any]:
        trace = self.repository.decision_trace_by_key(participant_digest, purpose_code, partner_code)
        if trace is None:
            raise NotFoundError("该用途没有任何决定记录")
        history = self.repository.decision_history(participant_digest, purpose_code, partner_code)
        return {
            "participant_ref": participant_digest[:12],
            "purpose_code": purpose_code,
            "partner_code": partner_code or None,
            "current": trace,
            "history": history,
        }

    # ---- 内部辅助 ----

    def _consent_result(self, repository: ConsentRepository, record: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        decisions = repository.decisions_of_record(record["id"])
        return {
            "record_id": record["id"],
            "replayed": replayed,
            "notice_code": record["notice_code"],
            "notice_version": record["notice_version"],
            "site_code": record["site_code"],
            "session_reference": record["session_reference"],
            "subject_type": record["subject_type"],
            "validity_starts_at": record["validity_starts_at"],
            "validity_expires_at": record["validity_expires_at"],
            "decisions": [
                {"purpose_code": row["purpose_code"], "partner_code": row["partner_code"] or None, "decision": row["decision"]}
                for row in decisions
            ],
        }

    def _withdrawal_result(self, repository: ConsentRepository, withdrawal: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        snapshots = repository.withdrawal_decisions(withdrawal["id"])
        return {
            "withdrawal_id": withdrawal["id"],
            "replayed": replayed,
            "received_at": withdrawal["received_at"],
            "processed_at": withdrawal["processed_at"],
            "affected_decisions": withdrawal["affected_decisions"],
            "stopped_intents": withdrawal["stopped_intents"],
            "queued_derived": withdrawal["queued_derived"],
            "decision_snapshots": [
                {"purpose_code": row["purpose_code"], "partner_code": row["partner_code"] or None,
                 "decision": row["decision"], "notice_version": row["notice_version"], "source_record_id": row["record_id"]}
                for row in snapshots
            ],
        }

    @staticmethod
    def _intent_view(repository: ConsentRepository, intent: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        current = repository.current_decision(intent["participant_digest"], intent["purpose_code"], intent["partner_code"])
        return {
            "intent_id": intent["id"],
            "replayed": replayed,
            "status": intent["status"],
            "purpose_code": intent["purpose_code"],
            "partner_code": intent["partner_code"] or None,
            "site_code": intent["site_code"],
            "session_reference": intent["session_reference"],
            "record_type": intent["record_type"],
            "notice_version": current["notice_version"] if current else None,
            "stopped_by_withdrawal_id": intent["stopped_by_withdrawal_id"],
        }

    def _audit(self, connection: sqlite3.Connection, action: str, resource_type: str, resource_id: int,
               *, after: dict[str, Any] | None = None, before: dict[str, Any] | None = None,
               metadata: dict[str, Any] | None = None, now: str | None = None) -> None:
        AuditRepository(connection).append(
            actor_user_id=None, actor_name="consent-service", action=action, resource_type=resource_type,
            resource_id=resource_id, outcome="success", before=before, after=after, metadata=metadata,
            correlation_id=None, created_at=now or to_storage(self.clock.now()),
        )
