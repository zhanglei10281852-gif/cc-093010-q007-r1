from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.consent import PURPOSE_LABELS
from app.consent.repository import ConsentRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.repositories.audit import AuditRepository


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class ConsentService:
    """可版本化用途授权：告知版本、用途级决定、撤回与派生记录处置。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ConsentRepository(self.connection)

    # ---- 告知版本 ----

    def publish_notice(self, data: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            if repository.notice_by_code(data["version_code"]):
                raise ConflictError("告知版本编码已存在")
            notice = repository.create_notice(
                version_code=data["version_code"], title=data["title"].strip(),
                body_digest=data["body_digest"], purposes=sorted(set(data["purposes"])),
                created_by=data["created_by"].strip(), now=now,
            )
            AuditRepository(connection).append(
                actor_user_id=None, actor_name=data["created_by"].strip(),
                action="consent.notice_published", resource_type="consent_notice",
                resource_id=notice["version_code"], outcome="success", before=None,
                after={"version_code": notice["version_code"], "purposes": json.loads(notice["purposes_json"])},
                metadata={}, correlation_id=None, created_at=now,
            )
            return self._notice_view(notice)

    def list_notices(self) -> list[dict[str, Any]]:
        return [self._notice_view(row) for row in self.repository.list_notices()]

    # ---- 授权决定 ----

    def record_grant(self, data: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        notice = self.repository.notice_by_code(data["notice_version_code"])
        if notice is None:
            raise NotFoundError("告知版本不存在")
        if notice["status"] != "published":
            raise ConflictError("告知版本未处于发布状态")
        notice_purposes = set(json.loads(notice["purposes_json"]))
        choices = self._normalize_choices(data["purposes"], notice_purposes)
        product_id, site_id = self._resolve_scope(data.get("product_code"), data.get("site_code"))
        valid_until = to_storage(data["valid_until"])
        if data["valid_until"] <= now_value:
            raise ValidationError("授权有效期必须晚于当前时间")

        guardian = data.get("guardian")
        signer_type = data["signer_type"]
        guardian_relation = (guardian or {}).get("relation", "") if signer_type == "guardian" else ""
        guardian_basis = (guardian or {}).get("basis", "") if signer_type == "guardian" else ""
        signer_display = (guardian or {}).get("signer_display", "") if signer_type == "guardian" else data["subject_ref"]

        decision_fp = digest({
            "subject": data["subject_digest"], "notice": notice["id"],
            "product": product_id or 0, "session": data.get("session_reference", ""),
            "choices": choices, "signer": signer_type,
        })
        grant_key = digest({
            "scope": "consent_grant", "subject": data["subject_digest"], "notice": notice["id"],
            "product": product_id or 0, "session": data.get("session_reference", ""), "decision": decision_fp,
        })

        # 先在事务外处理重复请求的常见路径
        idem = self.repository.event_by_idempotency_key(data["idempotency_key"])
        if idem is not None:
            return self.grant_detail_by_id(int(idem["grant_id"]))
        existing = self.repository.grant_by_key(grant_key)
        if existing is not None:
            # 同一参与者、同一告知版本、同一用途决定在任何场地重放，都返回同一个最终决定
            return self.grant_detail_by_id(int(existing["id"]))

        for attempt in range(2):
            try:
                with transaction(immediate=True) as connection:
                    repository = ConsentRepository(connection)
                    # 事务内重检唯一键：并发重复请求、跨场地重放和进程恢复都收敛到同一个最终决定
                    idem = repository.event_by_idempotency_key(data["idempotency_key"])
                    if idem is not None:
                        return self.grant_detail_by_id(int(idem["grant_id"]), connection)
                    existing = repository.grant_by_key(grant_key)
                    if existing is not None:
                        return self.grant_detail_by_id(int(existing["id"]), connection)
                    grant = repository.create_grant(
                        grant_key=grant_key, subject_ref=data["subject_ref"], subject_digest=data["subject_digest"],
                        signer_type=signer_type, guardian_relation=guardian_relation, guardian_basis=guardian_basis,
                        signer_display=signer_display, product_id=product_id, site_id=site_id,
                        session_reference=data.get("session_reference", ""), notice_id=int(notice["id"]),
                        notice_version_code=notice["version_code"], valid_from=now, valid_until=valid_until, now=now,
                    )
                    for choice in choices:
                        repository.add_decision(
                            grant_id=int(grant["id"]), purpose_code=choice["purpose"],
                            decision="granted" if choice["granted"] else "denied",
                            partner_code=choice.get("partner_code") or "",
                            partner_scope_digest=digest(choice["partner_scope"]) if choice.get("partner_scope") else "",
                            valid_until=valid_until, now=now,
                        )
                    any_granted = any(choice["granted"] for choice in choices)
                    event_id = repository.add_event(
                        grant_id=int(grant["id"]), event_type="granted" if any_granted else "denied",
                        purpose_codes=[c["purpose"] for c in choices], decisions=choices,
                        notice_version_code=notice["version_code"], actor_name=signer_display or data["subject_ref"],
                        reason="", idempotency_key=data["idempotency_key"], correlation_id="", now=now,
                    )
                    repository.update_grant_status(int(grant["id"]), "active", event_id, now)
                    AuditRepository(connection).append(
                        actor_user_id=None, actor_name=signer_display or data["subject_ref"],
                        action="consent.decided", resource_type="consent_grant", resource_id=grant_key[:16],
                        outcome="success", before=None, after={"grant_id": grant["id"], "notice": notice["version_code"],
                                                   "decisions": choices, "signer_type": signer_type},
                        metadata={"site_id": site_id, "product_id": product_id}, correlation_id=None, created_at=now,
                    )
                    return self.grant_detail_by_id(int(grant["id"]), connection)
            except sqlite3.IntegrityError:
                if attempt:
                    raise
        return self.grant_detail_by_id(int(self.repository.grant_by_key(grant_key)["id"]))

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        idem = self.repository.event_by_idempotency_key(data["idempotency_key"])
        if idem is not None:
            return self.grant_detail_by_id(int(idem["grant_id"]))
        grant = self.repository.grant_by_key(data["grant_key"])
        if grant is None:
            raise NotFoundError("授权记录不存在")
        if grant["status"] != "active":
            raise ConflictError(f"授权当前状态为 {grant['status']}，不能撤回")
        decisions = self.repository.decisions_for_grant(int(grant["id"]))
        granted_purposes = [row["purpose_code"] for row in decisions if row["decision"] == "granted"]
        withdrawn_already = self._withdrawn_purposes(int(grant["id"]))
        target = data["purposes"] or sorted(set(granted_purposes))
        unknown = [code for code in target if code not in granted_purposes]
        if unknown:
            raise ValidationError("这些用途未曾授权，无法撤回：" + "、".join(unknown))
        target = [code for code in target if code not in withdrawn_already]
        if not target:
            return self.grant_detail_by_id(int(grant["id"]))

        for attempt in range(2):
            try:
                with transaction(immediate=True) as connection:
                    repository = ConsentRepository(connection)
                    idem = repository.event_by_idempotency_key(data["idempotency_key"])
                    if idem is not None:
                        return self.grant_detail_by_id(int(idem["grant_id"]), connection)
                    event_id = repository.add_event(
                        grant_id=int(grant["id"]), event_type="withdrawn", purpose_codes=target,
                        decisions=[], notice_version_code=grant["notice_version_code"],
                        actor_name=data["actor_name"], reason=data["reason"],
                        idempotency_key=data["idempotency_key"], correlation_id="", now=now,
                    )
                    remaining = [code for code in granted_purposes if code not in withdrawn_already and code not in target]
                    new_status = "withdrawn" if not remaining else "active"
                    repository.update_grant_status(int(grant["id"]), new_status, event_id, now)
                    effects = self._apply_withdrawal_effects(connection, grant, target, event_id, now)
                    AuditRepository(connection).append(
                        actor_user_id=None, actor_name=data["actor_name"], action="consent.withdrawn",
                        resource_type="consent_grant", resource_id=grant["grant_key"][:16], outcome="success",
                        before=None, after={"purposes": target, "status": new_status, "reason": data["reason"]},
                        metadata=effects, correlation_id=None, created_at=now,
                    )
                    return self.grant_detail_by_id(int(grant["id"]), connection)
            except sqlite3.IntegrityError:
                if attempt:
                    raise
        return self.grant_detail_by_id(int(self.repository.event_by_idempotency_key(data["idempotency_key"])["grant_id"]))

    def expire_due_grants(self) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expired: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            for grant in repository.active_grants_expiring_before(now):
                decisions = repository.decisions_for_grant(int(grant["id"]))
                purposes = sorted({row["purpose_code"] for row in decisions if row["decision"] == "granted"})
                purposes = [code for code in purposes if code not in self._withdrawn_purposes(int(grant["id"]), connection)]
                event_id = repository.add_event(
                    grant_id=int(grant["id"]), event_type="expired", purpose_codes=purposes,
                    decisions=[], notice_version_code=grant["notice_version_code"],
                    actor_name="system-expiry", reason="授权有效期届满",
                    idempotency_key="", correlation_id="", now=now,
                )
                repository.update_grant_status(int(grant["id"]), "expired", event_id, now)
                self._apply_withdrawal_effects(connection, grant, purposes, event_id, now)
                expired.append(int(grant["id"]))
        return {"expired_grant_ids": expired, "checked_at": now}

    # ---- 授权判定（处理链路门控） ----

    def effective_decision(self, *, subject_digest: str, purpose: str, product_id: int | None = None,
                           session_reference: str = "", partner_code: str | None = None,
                           connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        conn = connection or self.connection
        # 匹配优先级：产品+场次 > 产品+全场 > 全场+场次 > 全场通用
        product_candidates = [product_id] + ([None] if product_id is not None else [])
        session_candidates = [session_reference] + ([""] if session_reference else [])
        last = {"permitted": False, "reason": "no_grant"}
        for candidate_product in product_candidates:
            for candidate_session in session_candidates:
                result = self._effective_decision_once(
                    connection=conn, subject_digest=subject_digest, purpose=purpose,
                    product_id=candidate_product, session_reference=candidate_session, partner_code=partner_code,
                )
                if result.get("grant_id") is not None:
                    return result
                last = result
        return last

    def _effective_decision_once(self, *, connection: sqlite3.Connection, subject_digest: str, purpose: str,
                                 product_id: int | None, session_reference: str,
                                 partner_code: str | None) -> dict[str, Any]:
        repository = ConsentRepository(connection)
        grant = repository.latest_grant(
            subject_digest=subject_digest, product_id=product_id, site_id=None,
            session_reference=session_reference,
        )
        denied: dict[str, Any] | None
        if grant is None:
            return {"permitted": False, "reason": "no_grant"}
        if grant["status"] == "withdrawn":
            denied = {"permitted": False, "reason": "withdrawn"}
        elif grant["status"] == "expired":
            denied = {"permitted": False, "reason": "expired"}
        elif grant["valid_until"] and grant["valid_until"] < to_storage(self.clock.now()):
            denied = {"permitted": False, "reason": "expired"}
        else:
            denied = None
        if denied is not None:
            denied.update({"grant_id": grant["id"], "grant_key": grant["grant_key"],
                           "notice_version_code": grant["notice_version_code"]})
            return denied
        decision_row = None
        for row in repository.decisions_for_grant(int(grant["id"])):
            if row["purpose_code"] != purpose:
                continue
            if purpose == "partner_sharing" and row["partner_code"] != (partner_code or ""):
                continue
            decision_row = row
            break
        if decision_row is None:
            return {"permitted": False, "reason": "purpose_not_in_notice_or_undecided",
                    "grant_id": grant["id"], "grant_key": grant["grant_key"],
                    "notice_version_code": grant["notice_version_code"]}
        if purpose in self._withdrawn_purposes(int(grant["id"]), connection):
            return {"permitted": False, "reason": "withdrawn", "grant_id": grant["id"],
                    "grant_key": grant["grant_key"], "notice_version_code": grant["notice_version_code"]}
        if decision_row["valid_until"] and decision_row["valid_until"] < to_storage(self.clock.now()):
            return {"permitted": False, "reason": "purpose_expired", "grant_id": grant["id"],
                    "grant_key": grant["grant_key"], "notice_version_code": grant["notice_version_code"]}
        if decision_row["decision"] != "granted":
            return {"permitted": False, "reason": "denied", "grant_id": grant["id"],
                    "grant_key": grant["grant_key"], "notice_version_code": grant["notice_version_code"]}
        return {
            "permitted": True, "reason": "granted", "grant_id": grant["id"],
            "grant_key": grant["grant_key"], "notice_version_code": grant["notice_version_code"],
            "valid_until": decision_row["valid_until"],
        }

    def grant_for_key(self, grant_key: str) -> dict[str, Any] | None:
        grant = self.repository.grant_by_key(grant_key)
        return self.grant_detail_by_id(int(grant["id"])) if grant else None

    def grant_detail_by_id(self, grant_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        repository = ConsentRepository(connection or self.connection)
        grant = repository.grant_by_id(grant_id)
        if grant is None:
            raise NotFoundError("授权记录不存在")
        decisions = repository.decisions_for_grant(grant_id)
        events = repository.events_for_grant(grant_id)
        return {
            "grant_key": grant["grant_key"],
            "subject_digest": grant["subject_digest"],
            "signer_type": grant["signer_type"],
            "guardian_relation": grant["guardian_relation"],
            "guardian_basis": grant["guardian_basis"],
            "signer_display": grant["signer_display"],
            "product_id": grant["product_id"],
            "site_id": grant["site_id"],
            "session_reference": grant["session_reference"],
            "notice_version_code": grant["notice_version_code"],
            "valid_from": grant["valid_from"],
            "valid_until": grant["valid_until"],
            "status": grant["status"],
            "created_at": grant["created_at"],
            "updated_at": grant["updated_at"],
            "decisions": [
                {
                    "purpose": row["purpose_code"],
                    "purpose_label": PURPOSE_LABELS.get(row["purpose_code"], row["purpose_code"]),
                    "decision": row["decision"],
                    "partner_code": row["partner_code"] or None,
                    "valid_until": row["valid_until"],
                }
                for row in decisions
            ],
            "events": [
                {
                    "id": row["id"], "event_type": row["event_type"],
                    "purposes": json.loads(row["purpose_codes_json"]),
                    "decisions": json.loads(row["decisions_json"]),
                    "notice_version_code": row["notice_version_code"],
                    "actor_name": row["actor_name"], "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in events
            ],
        }

    def subject_summary(self, subject_digest: str) -> dict[str, Any]:
        """对外查询只返回必要摘要：每用途最新状态与依据版本，不含身份明细。"""
        grants = self.repository.list_grants_for_subject(subject_digest)
        purposes: dict[str, dict[str, Any]] = {}
        for grant in grants:
            withdrawn = self._withdrawn_purposes(int(grant["id"]))
            for row in self.repository.decisions_for_grant(int(grant["id"])):
                code = row["purpose_code"]
                key = code if code != "partner_sharing" else f"{code}:{row['partner_code']}"
                if key in purposes:
                    continue
                if code in withdrawn or grant["status"] in {"withdrawn", "expired"}:
                    state = "withdrawn" if code in withdrawn else str(grant["status"])
                elif row["valid_until"] and row["valid_until"] < to_storage(self.clock.now()):
                    state = "expired"
                else:
                    state = row["decision"]
                purposes[key] = {
                    "purpose": code,
                    "partner_code": row["partner_code"] or None,
                    "state": state,
                    "notice_version_code": grant["notice_version_code"],
                    "valid_until": row["valid_until"],
                    "updated_at": grant["updated_at"],
                }
        return {"subject_digest": subject_digest, "purposes": list(purposes.values())}

    # ---- 处理活动登记（被产品/场次链路调用） ----

    def register_activity(self, *, grant_id: int, purpose: str, resource_type: str, resource_id: str,
                          product_id: int | None, site_id: int | None, session_reference: str,
                          status: str, connection: sqlite3.Connection, now: str | None = None) -> int:
        moment = now or to_storage(self.clock.now())
        finished = moment if status == "completed" else None
        cursor = connection.execute(
            "INSERT INTO consent_processing_activities(grant_id,purpose_code,resource_type,resource_id,"
            "product_id,site_id,session_reference,status,started_at,finished_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (grant_id, purpose, resource_type, str(resource_id), product_id, site_id,
             session_reference, status, moment, finished, moment),
        )
        return int(cursor.lastrowid)

    # ---- 撤回/到期后的处置队列执行 ----

    def run_dispositions(self, limit: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        processed: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            repository = ConsentRepository(connection)
            tasks = repository.queued_dispositions(limit)
            for task in tasks:
                repository.mark_disposition(int(task["id"]), "in_progress", now)
                result = self._execute_disposition(connection, task, now)
                repository.mark_disposition(int(task["id"]), "completed", now)
                processed.append({"task_id": task["id"], "action": task["action"],
                                  "resource": f"{task['source_resource_type']}:{task['source_resource_id']}",
                                  "result": result})
            AuditRepository(connection).append(
                actor_user_id=None, actor_name=actor, action="consent.disposition_executed",
                resource_type="consent_disposition_batch", resource_id="",
                outcome="success", before=None, after={"tasks": processed}, metadata={}, correlation_id=None, created_at=now,
            )
        return {"processed": processed, "finished_at": now}

    def pending_dispositions(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM consent_disposition_tasks WHERE status='queued'"
        ).fetchone()[0])

    # ---- 内部辅助 ----

    def _apply_withdrawal_effects(self, connection: sqlite3.Connection, grant: sqlite3.Row,
                                  purposes: list[str], event_id: int, now: str) -> dict[str, Any]:
        repository = ConsentRepository(connection)
        stopped: list[int] = []
        cancelled_sessions: list[int] = []
        enqueued: list[int] = []

        # 尚未开始的处理立即停止：取消仍在排队的场次；运行中场次发出取消请求
        if "collect" in purposes or "instant_report" in purposes:
            rows = connection.execute(
                "SELECT id,status FROM pilot_sessions WHERE consent_grant_id=? AND status IN ('queued','running') ORDER BY id",
                (grant["id"],),
            ).fetchall()
            for row in rows:
                session_id = int(row["id"])
                target_status = "cancelled" if row["status"] == "queued" else "cancel_requested"
                connection.execute(
                    "UPDATE pilot_sessions SET status=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (target_status, now if target_status == "cancelled" else None, now, session_id),
                )
                before = {"status": row["status"]}
                after = {"status": target_status}
                connection.execute(
                    "INSERT INTO pilot_interventions(session_id,actor,action,reason,before_json,after_json,batch_key,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, "consent-withdrawal", "cancel",
                     f"授权撤回：{'、'.join(purposes)}",
                     json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                     f"consent-event-{event_id}", now),
                )
                if target_status == "cancelled":
                    cancelled_sessions.append(session_id)

        placeholders = ",".join("?" for _ in purposes)
        activities = connection.execute(
            f"SELECT * FROM consent_processing_activities WHERE grant_id=? AND purpose_code IN ({placeholders}) ORDER BY id",
            (grant["id"], *purposes),
        ).fetchall()
        for activity in activities:
            status = activity["status"]
            if status in {"pending", "in_progress"}:
                repository.stop_activity(int(activity["id"]), now)
                stopped.append(int(activity["id"]))
            rule = repository.retention_rule(activity["purpose_code"], activity["resource_type"])
            if rule is None:
                rule = {"action": "retain", "obligation_code": "unspecified", "retain_days": 90}
            retain_until = None
            if rule["action"] == "retain":
                retain_days = int(rule["retain_days"]) if int(rule["retain_days"]) > 0 else 90
                retain_until = to_storage(self.clock.now() + timedelta(days=retain_days))
            repository.enqueue_disposition(
                grant_id=int(grant["id"]), trigger_event_id=event_id,
                purpose_code=activity["purpose_code"], resource_type=activity["resource_type"],
                resource_id=activity["resource_id"], action=rule["action"],
                obligation_code=rule["obligation_code"], retain_until=retain_until,
                note="撤回触发的派生记录处置" if status == "completed" else "处理中断后的残留记录", now=now,
            )
            enqueued.append(1)
        return {"stopped_activity_ids": stopped, "cancelled_session_ids": cancelled_sessions,
                "dispositions_enqueued": len(enqueued)}

    def _execute_disposition(self, connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> str:
        resource_type = task["source_resource_type"]
        resource_id = task["source_resource_id"]
        action = task["action"]
        if resource_type == "feedback":
            row = connection.execute("SELECT id FROM public_feedback WHERE id=?", (resource_id,)).fetchone()
            if row is None:
                return "source_missing"
            if action == "delete":
                connection.execute("DELETE FROM public_feedback WHERE id=?", (resource_id,))
                return "deleted"
            if action == "quarantine" and task["purpose_code"] == "follow_up_contact":
                # 仅撤回后续联系：清除联系方式并取消联系标记，记录仍可服务于其他已授权用途
                connection.execute(
                    "UPDATE public_feedback SET contact_digest='',consent_to_follow_up=0,disposition_at=? WHERE id=?",
                    (now, resource_id),
                )
                return "contact_suppressed"
            connection.execute(
                "UPDATE public_feedback SET disposition_status=?,disposition_at=? WHERE id=?",
                ("quarantined" if action == "quarantine" else "retained", now, resource_id),
            )
            return str(action) + "d"
        if resource_type == "observation_report":
            row = connection.execute(
                "SELECT id FROM pilot_observations WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (resource_id,),
            ).fetchone()
            if row is None:
                return "source_missing"
            if action == "delete":
                connection.execute(
                    "UPDATE pilot_observations SET observation_json='{}',metrics_json='{}' WHERE session_id=?",
                    (resource_id,),
                )
                return "report_content_purged"
            connection.execute(
                "UPDATE pilot_observations SET observation_json=json_patch(observation_json,'{\"_quarantined\":true}') "
                "WHERE session_id=?",
                (resource_id,),
            )
            return "quarantined"
        if resource_type == "session":
            return "retained_by_schedule" if action == "retain" else "retained_for_audit"
        return "noop"

    def _withdrawn_purposes(self, grant_id: int, connection: sqlite3.Connection | None = None) -> set[str]:
        conn = connection or self.connection
        rows = conn.execute(
            "SELECT purpose_codes_json FROM consent_events WHERE grant_id=? AND event_type='withdrawn'",
            (grant_id,),
        ).fetchall()
        result: set[str] = set()
        for row in rows:
            result.update(json.loads(row["purpose_codes_json"]))
        return result

    def _resolve_scope(self, product_code: str | None, site_code: str | None) -> tuple[int | None, int | None]:
        product_id = site_id = None
        if product_code:
            row = self.connection.execute("SELECT id FROM health_products WHERE code=?", (product_code,)).fetchone()
            if row is None:
                raise NotFoundError("授权关联的产品不存在")
            product_id = int(row["id"])
        if site_code:
            row = self.connection.execute("SELECT id FROM pilot_sites WHERE code=?", (site_code,)).fetchone()
            if row is None:
                raise NotFoundError("授权关联的场地不存在")
            site_id = int(row["id"])
        return product_id, site_id

    @staticmethod
    def _normalize_choices(raw: list[dict[str, Any]], notice_purposes: set[str]) -> list[dict[str, Any]]:
        choices = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            code = item["purpose"]
            if code not in notice_purposes:
                raise ValidationError(f"用途 {PURPOSE_LABELS.get(code, code)} 不在该告知版本中，新增用途需重新签署授权")
            partner = item.get("partner_code") or ""
            key = (code, partner)
            if key in seen:
                raise ValidationError("同一用途（含合作方）不能重复决定")
            seen.add(key)
            choices.append({
                "purpose": code, "granted": bool(item["granted"]),
                "partner_code": partner or None,
                "partner_scope": item.get("partner_scope"),
            })
        decided = {code for code, _ in seen}
        missing = notice_purposes - decided
        if missing:
            raise ValidationError("必须对告知版本中的每个用途分别作出允许或拒绝："
                                  + "、".join(PURPOSE_LABELS.get(code, code) for code in sorted(missing)))
        return choices

    @staticmethod
    def _notice_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "version_code": row["version_code"], "title": row["title"],
            "body_digest": row["body_digest"], "purposes": json.loads(row["purposes_json"]),
            "status": row["status"], "published_at": row["published_at"], "created_by": row["created_by"],
        }
