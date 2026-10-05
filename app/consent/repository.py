from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ConsentRepository:
    """用途授权领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 告知版本与合作方 ----

    def partner_by_code(self, partner_code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_partners WHERE partner_code=?", (partner_code,)).fetchone())

    def create_partner(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO consent_partners(partner_code,name,partner_type,region,retention_days,status,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
            (data["partner_code"], data["name"], data["partner_type"], data.get("region", ""), data.get("retention_days"), now, now),
        )
        return dict(self.connection.execute("SELECT * FROM consent_partners WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_partners(self, active_only: bool) -> list[dict[str, Any]]:
        where = " WHERE status='active'" if active_only else ""
        return [dict(row) for row in self.connection.execute("SELECT * FROM consent_partners" + where + " ORDER BY partner_code").fetchall()]

    def latest_notice(self, notice_code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_notice_versions WHERE notice_code=? ORDER BY version DESC LIMIT 1", (notice_code,)
        ).fetchone())

    def notice_by_id(self, notice_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_notice_versions WHERE id=?", (notice_id,)).fetchone())

    def active_notice(self, notice_code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_notice_versions WHERE notice_code=? AND status='active'", (notice_code,)
        ).fetchone())

    def create_notice(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO consent_notice_versions(notice_code,version,title,content_hash,content_excerpt,purposes_json,data_categories_json,partners_json,status,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
            (
                data["notice_code"], data["version"], data["title"], data["content_hash"], data.get("content_excerpt", ""),
                json.dumps(data["purposes"], ensure_ascii=False, sort_keys=True),
                json.dumps(data.get("data_categories", []), ensure_ascii=False, sort_keys=True),
                json.dumps(data.get("partners", []), ensure_ascii=False, sort_keys=True),
                data["created_by"], now,
            ),
        )
        return dict(self.connection.execute("SELECT * FROM consent_notice_versions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def deprecate_notice(self, notice_id: int, now: str) -> None:
        self.connection.execute("UPDATE consent_notice_versions SET status='deprecated' WHERE id=?", (notice_id,))

    def deprecate_active_notices(self, notice_code: str) -> int:
        cursor = self.connection.execute(
            "UPDATE consent_notice_versions SET status='deprecated' WHERE notice_code=? AND status='active'",
            (notice_code,),
        )
        return cursor.rowcount

    def list_notices(self, notice_code: str | None) -> list[dict[str, Any]]:
        if notice_code:
            rows = self.connection.execute(
                "SELECT * FROM consent_notice_versions WHERE notice_code=? ORDER BY version DESC", (notice_code,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM consent_notice_versions ORDER BY notice_code,version DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 授权记录 ----

    def consent_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_records WHERE idempotency_key=?", (key,)).fetchone())

    def consent_by_id(self, record_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_records WHERE id=?", (record_id,)).fetchone())

    def insert_consent(self, data: dict[str, Any], fingerprint: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO consent_records(participant_digest,subject_type,guardian_relation,guardian_basis,guardian_digest,"
            "site_code,session_reference,notice_id,notice_code,notice_version,idempotency_key,request_fingerprint,"
            "validity_starts_at,validity_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["participant_digest"], data["subject_type"], data.get("guardian_relation"), data.get("guardian_basis"),
                data.get("guardian_digest"), data["site_code"], data["session_reference"], data["notice_id"],
                data["notice_code"], data["notice_version"], data["idempotency_key"], fingerprint,
                data["validity_starts_at"], data["validity_expires_at"], now,
            ),
        )
        return int(cursor.lastrowid)

    def insert_decision(self, *, record_id: int, participant_digest: str, purpose_code: str, partner_code: str, decision: str, notice_version: int, now: str) -> None:
        self.connection.execute(
            "INSERT INTO consent_purpose_decisions(record_id,participant_digest,purpose_code,partner_code,decision,notice_version,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, participant_digest, purpose_code, partner_code, decision, notice_version, now),
        )

    def decisions_of_record(self, record_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_purpose_decisions WHERE record_id=? ORDER BY id", (record_id,)
        ).fetchall()]

    def next_decision_seq(self, participant_digest: str, purpose_code: str, partner_code: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM consent_current_decisions WHERE participant_digest=? AND purpose_code=? AND partner_code=?",
            (participant_digest, purpose_code, partner_code),
        ).fetchone()
        return int(row["next_seq"])

    def current_decision(self, participant_digest: str, purpose_code: str, partner_code: str = "") -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_current_decisions WHERE participant_digest=? AND purpose_code=? AND partner_code=?",
            (participant_digest, purpose_code, partner_code),
        ).fetchone())

    def upsert_current_decision(self, *, participant_digest: str, purpose_code: str, partner_code: str, decision: str,
                                notice_id: int, notice_version: int, record_id: int, seq: int,
                                valid_from: str, valid_until: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO consent_current_decisions(participant_digest,purpose_code,partner_code,decision,notice_id,notice_version,"
            "record_id,seq,status,decided_at,valid_from,valid_until,updated_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?) "
            "ON CONFLICT(participant_digest,purpose_code,partner_code) DO UPDATE SET decision=excluded.decision,"
            "notice_id=excluded.notice_id,notice_version=excluded.notice_version,record_id=excluded.record_id,seq=excluded.seq,"
            "status='active',decided_at=excluded.decided_at,valid_from=excluded.valid_from,valid_until=excluded.valid_until,updated_at=excluded.updated_at",
            (participant_digest, purpose_code, partner_code, decision, notice_id, notice_version, record_id, seq, now, valid_from, valid_until, now),
        )

    def expire_current_decisions(self, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE consent_current_decisions SET status='expired',updated_at=? WHERE status='active' AND valid_until<=?",
            (now, now),
        )
        return cursor.rowcount

    # ---- 撤回 ----

    def withdrawal_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_withdrawals WHERE idempotency_key=?", (key,)).fetchone())

    def withdrawal_by_id(self, withdrawal_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_withdrawals WHERE id=?", (withdrawal_id,)).fetchone())

    def insert_withdrawal(self, *, participant_digest: str, purpose_code: str | None, partner_code: str, reason: str,
                          idempotency_key: str, fingerprint: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO consent_withdrawals(participant_digest,purpose_code,partner_code,reason,idempotency_key,request_fingerprint,received_at) VALUES(?,?,?,?,?,?,?)",
            (participant_digest, purpose_code, partner_code, reason, idempotency_key, fingerprint, now),
        )
        return int(cursor.lastrowid)

    def active_decisions_for_withdrawal(self, participant_digest: str, purpose_code: str | None, partner_code: str) -> list[dict[str, Any]]:
        clauses = ["participant_digest=?", "status='active'"]
        params: list[Any] = [participant_digest]
        if purpose_code is not None:
            clauses.append("purpose_code=?")
            params.append(purpose_code)
            if partner_code:
                clauses.append("partner_code=?")
                params.append(partner_code)
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_current_decisions WHERE " + " AND ".join(clauses) + " ORDER BY id", tuple(params)
        ).fetchall()]

    def mark_decision_withdrawn(self, decision_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_current_decisions SET status='withdrawn',updated_at=? WHERE id=?", (now, decision_id)
        )

    def insert_withdrawal_decision(self, *, withdrawal_id: int, snapshot: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO consent_withdrawal_decisions(withdrawal_id,record_id,participant_digest,purpose_code,partner_code,"
            "decision,notice_version,decided_seq,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (
                withdrawal_id, snapshot["record_id"], snapshot["participant_digest"], snapshot["purpose_code"],
                snapshot["partner_code"], snapshot["decision"], snapshot["notice_version"], snapshot["seq"], now,
            ),
        )

    def stop_pending_intents(self, *, participant_digest: str, purpose_code: str, partner_code: str, withdrawal_id: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE consent_processing_intents SET status='stopped',stopped_by_withdrawal_id=?,updated_at=? "
            "WHERE participant_digest=? AND purpose_code=? AND partner_code=? AND status='scheduled'",
            (withdrawal_id, now, participant_digest, purpose_code, partner_code),
        )
        return cursor.rowcount

    def finalize_withdrawal(self, withdrawal_id: int, *, affected: int, stopped: int, queued: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_withdrawals SET processed_at=?,affected_decisions=?,stopped_intents=?,queued_derived=? WHERE id=?",
            (now, affected, stopped, queued, withdrawal_id),
        )

    # ---- 处理意图（用于撤回时停止尚未开始的处理）----

    def intent_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM consent_processing_intents WHERE idempotency_key=?", (key,)).fetchone())

    def register_intent(self, data: dict[str, Any], fingerprint: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO consent_processing_intents(participant_digest,purpose_code,partner_code,site_code,session_reference,"
            "record_type,status,idempotency_key,request_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,'scheduled',?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (data["participant_digest"], data["purpose_code"], data.get("partner_code", ""), data["site_code"],
             data["session_reference"], data.get("record_type", ""), data["idempotency_key"], fingerprint, now, now),
        )
        row = self.intent_by_idempotency(data["idempotency_key"])
        return dict(row) if row else {}

    def mark_intent_running(self, intent_id: int, now: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE consent_processing_intents SET status='running',updated_at=? WHERE id=? AND status='scheduled'",
            (now, intent_id),
        )
        return cursor.rowcount == 1

    def mark_intent_done(self, intent_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_processing_intents SET status='done',updated_at=? WHERE id=? AND status='running'",
            (now, intent_id),
        )

    # ---- 派生记录处置队列 ----

    def derived_by_ref(self, record_type: str, record_ref: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT * FROM consent_derived_records WHERE record_type=? AND record_ref=?", (record_type, record_ref)
        ).fetchone())

    def register_derived(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO consent_derived_records(participant_digest,source_purpose_code,source_partner_code,record_type,record_ref,"
            "notice_version,retention_basis,retention_until,status,queued_action,action_due_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(record_type,record_ref) DO UPDATE SET updated_at=excluded.updated_at",
            (
                data["participant_digest"], data["source_purpose_code"], data.get("source_partner_code", ""),
                data["record_type"], data["record_ref"], data["notice_version"], data.get("retention_basis", ""),
                data.get("retention_until"), "active", "none", None, now, now,
            ),
        )
        row = self.derived_by_ref(data["record_type"], data["record_ref"])
        return dict(row) if row else {}

    def derived_for_withdrawal(self, participant_digest: str, purpose_code: str, partner_code: str) -> list[dict[str, Any]]:
        clauses = ["participant_digest=?", "source_purpose_code=?", "status IN ('active','retained','quarantined','delete_pending')"]
        params: list[Any] = [participant_digest, purpose_code]
        if partner_code:
            clauses.append("source_partner_code=?")
            params.append(partner_code)
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_derived_records WHERE " + " AND ".join(clauses) + " ORDER BY id", tuple(params)
        ).fetchall()]

    def queue_derived(self, derived_id: int, *, action: str, due_at: str | None, reason: str, withdrawal_id: int, now: str) -> None:
        status_map = {"retain": "retained", "quarantine": "quarantined", "delete": "delete_pending"}
        self.connection.execute(
            "UPDATE consent_derived_records SET queued_action=?,status=?,action_due_at=?,disposition_reason=?,withdrawal_id=?,updated_at=? WHERE id=?",
            (action, status_map[action], due_at, reason, withdrawal_id, now, derived_id),
        )

    def due_derived(self, now: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_derived_records WHERE status='delete_pending' AND action_due_at<=? ORDER BY id", (now,)
        ).fetchall()]

    def derived_due_for_transition(self, now: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_derived_records WHERE status='quarantined' AND action_due_at IS NOT NULL AND action_due_at<=? ORDER BY id",
            (now,),
        ).fetchall()]

    def transition_to_delete_pending(self, derived_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_derived_records SET status='delete_pending',queued_action='delete',updated_at=? WHERE id=?",
            (now, derived_id),
        )

    def mark_derived_deleted(self, derived_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_derived_records SET status='deleted',queued_action='none',processed_at=?,updated_at=? WHERE id=?",
            (now, now, derived_id),
        )

    def mark_derived_retained(self, derived_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE consent_derived_records SET status='retained',queued_action='retain',processed_at=?,updated_at=? WHERE id=?",
            (now, now, derived_id),
        )

    # ---- 查询与审计线索 ----

    def list_consent_records(self, participant_digest: str, limit: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_records WHERE participant_digest=? ORDER BY id DESC LIMIT ?", (participant_digest, limit)
        ).fetchall()]

    def decision_trace(self, decision_pk: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT d.*,r.site_code,r.session_reference,r.subject_type,r.guardian_relation,r.guardian_basis,"
            "n.title AS notice_title,n.content_hash AS notice_content_hash,n.purposes_json AS notice_purposes "
            "FROM consent_current_decisions d "
            "JOIN consent_records r ON r.id=d.record_id "
            "JOIN consent_notice_versions n ON n.id=d.notice_id WHERE d.id=?",
            (decision_pk,),
        ).fetchone())

    def decision_trace_by_key(self, participant_digest: str, purpose_code: str, partner_code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute(
            "SELECT d.id AS decision_pk,d.decision,d.status,d.notice_version,d.seq,d.record_id,d.decided_at,"
            "d.valid_from,d.valid_until,r.participant_digest,r.site_code,r.session_reference,r.subject_type,"
            "r.guardian_relation,r.guardian_basis,r.guardian_digest,r.notice_code,"
            "n.title AS notice_title,n.content_hash AS notice_content_hash "
            "FROM consent_current_decisions d "
            "JOIN consent_records r ON r.id=d.record_id "
            "JOIN consent_notice_versions n ON n.id=d.notice_id "
            "WHERE d.participant_digest=? AND d.purpose_code=? AND d.partner_code=?",
            (participant_digest, purpose_code, partner_code),
        ).fetchone())

    def decision_history(self, participant_digest: str, purpose_code: str, partner_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT pd.id,pd.record_id,pd.decision,pd.notice_version,pd.created_at,"
            "r.subject_type,r.guardian_relation "
            "FROM consent_purpose_decisions pd JOIN consent_records r ON r.id=pd.record_id "
            "WHERE pd.participant_digest=? AND pd.purpose_code=? AND pd.partner_code=? ORDER BY pd.id",
            (participant_digest, purpose_code, partner_code),
        ).fetchall()]

    def current_decisions_for_records(self, record_ids: Iterable[int]) -> list[dict[str, Any]]:
        record_ids = list(record_ids)
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        return [dict(row) for row in self.connection.execute(
            f"SELECT * FROM consent_current_decisions WHERE record_id IN ({placeholders}) ORDER BY id", record_ids
        ).fetchall()]

    def withdrawal_decisions(self, withdrawal_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM consent_withdrawal_decisions WHERE withdrawal_id=? ORDER BY id", (withdrawal_id,)
        ).fetchall()]

    def derived_for_participant(self, participant_digest: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,source_purpose_code,source_partner_code,record_type,record_ref,notice_version,retention_basis,"
            "retention_until,status,queued_action,action_due_at,disposition_reason,withdrawal_id,created_at,updated_at,processed_at "
            "FROM consent_derived_records WHERE participant_digest=? ORDER BY id", (participant_digest,)
        ).fetchall()]

    def intents_for_session(self, participant_digest: str, site_code: str, session_reference: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,purpose_code,partner_code,record_type,status,stopped_by_withdrawal_id,created_at,updated_at "
            "FROM consent_processing_intents WHERE participant_digest=? AND site_code=? AND session_reference=? ORDER BY id",
            (participant_digest, site_code, session_reference),
        ).fetchall()]
