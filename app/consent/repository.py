from __future__ import annotations

import json
import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ConsentRepository:
    """封装用途授权领域的 SQLite 读写，所有方法假设调用方已持有事务。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 告知版本 ----

    def create_notice(self, *, version_code: str, title: str, body_digest: str, purposes: list[str], created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO consent_notice_versions(version_code,title,body_digest,purposes_json,status,published_at,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,'published',?,?,?,?)",
            (version_code, title, body_digest, json.dumps(purposes, ensure_ascii=False), now, created_by, now, now),
        )
        return self.notice_by_id(int(cursor.lastrowid)) or {}

    def notice_by_code(self, version_code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_notice_versions WHERE version_code=?", (version_code,)
        ).fetchone())

    def notice_by_id(self, notice_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_notice_versions WHERE id=?", (notice_id,)
        ).fetchone())

    def list_notices(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_notice_versions ORDER BY published_at DESC,id DESC"
        ).fetchall()]

    # ---- 授权聚合 ----

    def create_grant(self, *, grant_key: str, subject_ref: str, subject_digest: str, signer_type: str,
                     guardian_relation: str, guardian_basis: str, signer_display: str,
                     product_id: int | None, site_id: int | None, session_reference: str,
                     notice_id: int, notice_version_code: str, valid_from: str, valid_until: str | None,
                     now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO consent_grants(grant_key,subject_ref,subject_digest,signer_type,guardian_relation,guardian_basis,"
            "signer_display,product_id,site_id,session_reference,notice_id,notice_version_code,valid_from,valid_until,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
            (grant_key, subject_ref, subject_digest, signer_type, guardian_relation, guardian_basis,
             signer_display, product_id, site_id, session_reference, notice_id, notice_version_code,
             valid_from, valid_until, now, now),
        )
        return self.grant_by_id(int(cursor.lastrowid)) or {}

    def grant_by_key(self, grant_key: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_grants WHERE grant_key=?", (grant_key,)
        ).fetchone())

    def grant_by_id(self, grant_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_grants WHERE id=?", (grant_id,)
        ).fetchone())

    def latest_grant(self, *, subject_digest: str, product_id: int | None, site_id: int | None,
                     session_reference: str) -> dict[str, Any] | None:
        # 场地不参与匹配键：参与者在数贸会主会场签署的授权，在分会场重放时仍应解析到同一聚合
        clauses = ["subject_digest=?"]
        params: list[Any] = [subject_digest]
        if product_id is None:
            clauses.append("product_id IS NULL")
        else:
            clauses.append("product_id=?")
            params.append(product_id)
        if session_reference:
            clauses.append("session_reference=?")
            params.append(session_reference)
        else:
            clauses.append("session_reference=''")
        return _dict(self.connection.execute(
            "SELECT * FROM consent_grants WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC,id DESC LIMIT 1",
            tuple(params),
        ).fetchone())

    def list_grants_for_subject(self, subject_digest: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_grants WHERE subject_digest=? ORDER BY id DESC", (subject_digest,)
        ).fetchall()]

    def update_grant_status(self, grant_id: int, status: str, latest_event_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_grants SET status=?,latest_event_id=?,updated_at=? WHERE id=?",
            (status, latest_event_id, now, grant_id),
        )

    def add_decision(self, *, grant_id: int, purpose_code: str, decision: str, partner_code: str,
                     partner_scope_digest: str, valid_until: str | None, now: str) -> None:
        self.connection.execute(
            "INSERT INTO consent_purpose_decisions(grant_id,purpose_code,decision,partner_code,partner_scope_digest,valid_until,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (grant_id, purpose_code, decision, partner_code, partner_scope_digest, valid_until, now),
        )

    def decisions_for_grant(self, grant_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_purpose_decisions WHERE grant_id=? ORDER BY id", (grant_id,)
        ).fetchall()]

    def add_event(self, *, grant_id: int, event_type: str, purpose_codes: list[str], decisions: list[dict[str, Any]],
                  notice_version_code: str, actor_name: str, reason: str, idempotency_key: str,
                  correlation_id: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO consent_events(grant_id,event_type,purpose_codes_json,decisions_json,notice_version_code,"
            "actor_name,reason,idempotency_key,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (grant_id, event_type, json.dumps(purpose_codes, ensure_ascii=False),
             json.dumps(decisions, ensure_ascii=False, sort_keys=True), notice_version_code,
             actor_name, reason, idempotency_key, correlation_id, now),
        )
        return int(cursor.lastrowid)

    def event_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        return _dict(self.connection.execute(
            "SELECT * FROM consent_events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone())

    def events_for_grant(self, grant_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_events WHERE grant_id=? ORDER BY id", (grant_id,)
        ).fetchall()]

    # ---- 处理活动登记 ----

    def stop_activity(self, activity_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_processing_activities SET status='stopped',finished_at=? WHERE id=? AND status IN ('pending','in_progress')",
            (now, activity_id),
        )

    # ---- 撤回后的处置队列 ----

    def retention_rule(self, purpose_code: str, resource_type: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_retention_rules WHERE purpose_code=? AND resource_type=?",
            (purpose_code, resource_type),
        ).fetchone())

    def enqueue_disposition(self, *, grant_id: int | None, trigger_event_id: int, purpose_code: str,
                            resource_type: str, resource_id: str, action: str, obligation_code: str,
                            retain_until: str | None, note: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO consent_disposition_tasks(grant_id,trigger_event_id,purpose_code,source_resource_type,"
            "source_resource_id,obligation_code,action,status,retain_until,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'queued',?,?,?,?) "
            "ON CONFLICT(grant_id,purpose_code,source_resource_type,source_resource_id,action) DO NOTHING",
            (grant_id, trigger_event_id, purpose_code, resource_type, str(resource_id),
             obligation_code, action, retain_until, note, now, now),
        )

    def queued_dispositions(self, limit: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_disposition_tasks WHERE status='queued' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()]

    def mark_disposition(self, task_id: int, status: str, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_disposition_tasks SET status=?,updated_at=? WHERE id=?", (status, now, task_id)
        )

    def disposition_by_id(self, task_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_disposition_tasks WHERE id=?", (task_id,)
        ).fetchone())

    # ---- 有效期巡检 ----

    def active_grants_expiring_before(self, moment: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_grants WHERE status='active' AND valid_until IS NOT NULL AND valid_until<? ORDER BY id",
            (moment,),
        ).fetchall()]
