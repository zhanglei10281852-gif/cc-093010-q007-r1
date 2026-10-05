from __future__ import annotations

from datetime import UTC, datetime

from app.consent.purposes import (
    PURPOSE_COLLECTION,
    PURPOSE_FOLLOW_UP,
    PURPOSE_INSTANT_REPORT,
    PURPOSE_PARTNER_SHARING,
    PURPOSE_PRODUCT_IMPROVEMENT,
)
from app.consent.service import ConsentService
from app.core.clock import FrozenClock
from app.database import get_connection

NOTICE = {
    "notice_code": "expo-tongue-eeg",
    "title": "数贸会 AI 舌诊与脑电体验告知",
    "content_hash": "notice-v1-content-hash-0001",
    "purposes": [
        PURPOSE_COLLECTION,
        PURPOSE_INSTANT_REPORT,
        PURPOSE_PRODUCT_IMPROVEMENT,
        PURPOSE_PARTNER_SHARING,
        PURPOSE_FOLLOW_UP,
    ],
    "data_categories": ["舌象图像", "脑电信号"],
    "partners": ["partner-univ"],
    "created_by": "privacy-officer",
}

ALL_PURPOSE_DECISIONS = [
    {"purpose_code": PURPOSE_COLLECTION, "decision": "allow"},
    {"purpose_code": PURPOSE_INSTANT_REPORT, "decision": "allow"},
    {"purpose_code": PURPOSE_PRODUCT_IMPROVEMENT, "decision": "allow"},
    {"purpose_code": PURPOSE_PARTNER_SHARING, "partner_code": "partner-univ", "decision": "allow"},
    {"purpose_code": PURPOSE_FOLLOW_UP, "decision": "deny"},
]


def setup_catalog(client) -> None:
    site = client.post("/api/catalog/sites", json={
        "code": "expo-hall-b",
        "name": "数贸会二号体验点",
        "site_type": "展会体验点",
        "region": "杭州",
        "capabilities": ["tongue-ai", "eeg"],
    })
    assert site.status_code == 201, site.text


def setup_partner_and_notice(client, admin: dict, notice: dict | None = None) -> dict:
    partner = client.post("/api/consent/partners", headers=admin["headers"], json={
        "partner_code": "partner-univ",
        "name": "联合大学生物医学工程学院",
        "partner_type": "研究机构",
        "region": "浙江",
        "retention_days": 1825,
    })
    assert partner.status_code == 201, partner.text
    response = client.post("/api/consent/notices", headers=admin["headers"], json=notice or NOTICE)
    assert response.status_code == 201, response.text
    return response.json()


def consent_payload(key: str, *, decisions=None, participant: str = "participant-001",
                    site: str = "expo-hall-b", session: str = "session-001", validity_days: int = 90) -> dict:
    return {
        "participant_digest": participant,
        "site_code": site,
        "session_reference": session,
        "subject_type": "self",
        "notice_code": NOTICE["notice_code"],
        "decisions": ALL_PURPOSE_DECISIONS if decisions is None else decisions,
        "validity_days": validity_days,
        "idempotency_key": key,
    }


def test_notice_versioning_per_purpose_decisions_validity_and_guardian(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)

    payload = consent_payload("consent-key-000001")
    first = client.post("/api/consent/records", json=payload)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["notice_version"] == 1
    assert body["replayed"] is False
    assert len(body["decisions"]) == 5
    assert body["validity_expires_at"] > body["validity_starts_at"]

    # 重复请求：同一个最终决定
    second = client.post("/api/consent/records", json=payload)
    assert second.status_code == 201
    assert second.json()["record_id"] == body["record_id"]
    assert second.json()["replayed"] is True

    # 跨场地重放：同一幂等键换场地必须被拒绝
    replayed = dict(payload)
    replayed["site_code"] = "other-site"
    conflict = client.post("/api/consent/records", json=replayed)
    assert conflict.status_code == 409

    # 缺少逐项决定
    missing = consent_payload("consent-key-000002", decisions=[{"purpose_code": PURPOSE_COLLECTION, "decision": "allow"}])
    response = client.post("/api/consent/records", json=missing)
    assert response.status_code == 422

    # 监护人代签必须说明关系与依据
    guardian = consent_payload("consent-key-000003", participant="minor-001", decisions=ALL_PURPOSE_DECISIONS)
    guardian["subject_type"] = "guardian"
    rejected = client.post("/api/consent/records", json=guardian)
    assert rejected.status_code == 422
    guardian["guardian"] = {"relation": "父亲", "basis": "民法典第二十七条法定监护", "guardian_digest": "guardian-001"}
    signed = client.post("/api/consent/records", json=guardian)
    assert signed.status_code == 201, signed.text
    assert signed.json()["subject_type"] == "guardian"


def test_prerequisite_and_partner_scope_are_enforced(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)

    # 拒绝采集却允许即时报告：前置用途不满足
    payload = consent_payload(
        "consent-key-000010",
        decisions=[
            {"purpose_code": PURPOSE_COLLECTION, "decision": "deny"},
            {"purpose_code": PURPOSE_INSTANT_REPORT, "decision": "allow"},
        ],
    )
    assert client.post("/api/consent/records", json=payload).status_code == 422

    # 合作方共享必须逐家指定
    payload = consent_payload(
        "consent-key-000011",
        decisions=[
            {"purpose_code": PURPOSE_COLLECTION, "decision": "allow"},
            {"purpose_code": PURPOSE_INSTANT_REPORT, "decision": "deny"},
            {"purpose_code": PURPOSE_PRODUCT_IMPROVEMENT, "decision": "deny"},
            {"purpose_code": PURPOSE_PARTNER_SHARING, "decision": "allow"},
            {"purpose_code": PURPOSE_FOLLOW_UP, "decision": "deny"},
        ],
    )
    assert client.post("/api/consent/records", json=payload).status_code == 422


def test_new_purpose_in_notice_v2_does_not_inherit_old_decision(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    v1 = client.post("/api/consent/records", json=consent_payload("consent-key-v2-0001"))
    assert v1.status_code == 201

    # 发布告知 v2：新增用途并移除合作方共享
    v2_notice = dict(NOTICE)
    v2_notice["content_hash"] = "notice-v2-content-hash-0002"
    v2_notice["purposes"] = [PURPOSE_COLLECTION, PURPOSE_INSTANT_REPORT, PURPOSE_PRODUCT_IMPROVEMENT, PURPOSE_FOLLOW_UP, "new_analysis"]
    v2_notice["partners"] = []
    created = client.post("/api/consent/notices", headers=admin["headers"], json=v2_notice)
    assert created.status_code == 422  # 未注册用途不能写进告知

    v2_notice["purposes"] = [PURPOSE_COLLECTION, PURPOSE_INSTANT_REPORT, PURPOSE_PRODUCT_IMPROVEMENT, PURPOSE_FOLLOW_UP]
    published = client.post("/api/consent/notices", headers=admin["headers"], json=v2_notice)
    assert published.status_code == 201
    assert published.json()["version"] == 2

    # v1 授权仍在有效期内：内部改进仍可依据 v1
    gate = client.get(f"/api/consent/check?participant_digest=participant-001&purpose_code={PURPOSE_PRODUCT_IMPROVEMENT}")
    assert gate.json()["allowed"] is True
    assert gate.json()["notice_version"] == 1

    # 新一次签署必须依据 v2，且逐项决定；旧授权记录不被覆盖
    decisions = [
        {"purpose_code": PURPOSE_COLLECTION, "decision": "allow"},
        {"purpose_code": PURPOSE_INSTANT_REPORT, "decision": "deny"},
        {"purpose_code": PURPOSE_PRODUCT_IMPROVEMENT, "decision": "deny"},
        {"purpose_code": PURPOSE_FOLLOW_UP, "decision": "allow"},
    ]
    resigned = client.post("/api/consent/records", json=consent_payload("consent-key-v2-0002", decisions=decisions))
    assert resigned.status_code == 201
    assert resigned.json()["notice_version"] == 2

    instant_gate = client.get(f"/api/consent/check?participant_digest=participant-001&purpose_code={PURPOSE_INSTANT_REPORT}")
    assert instant_gate.json()["allowed"] is False
    follow_gate = client.get(f"/api/consent/check?participant_digest=participant-001&purpose_code={PURPOSE_FOLLOW_UP}")
    assert follow_gate.json()["allowed"] is True
    assert follow_gate.json()["notice_version"] == 2

    # v1 记录仍可追溯到当时的允许
    detail = client.get(f"/api/consent/records/{v1.json()['record_id']}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["notice_version"] == 1
    assert any(d["decision"] == "allow" for d in detail.json()["decisions"])


def test_withdrawal_stops_unstarted_processing_and_disposes_derived(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    consent = client.post("/api/consent/records", json=consent_payload("consent-key-wd-0001"))
    assert consent.status_code == 201
    participant = "participant-001"

    # 即时结论处理已登记但尚未开始
    intent_key = "intent-instant-000001"
    register = client.post("/api/consent/processing/intents", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_INSTANT_REPORT,
        "site_code": "expo-hall-b",
        "session_reference": "session-001",
        "record_type": "instant_report",
        "idempotency_key": intent_key,
    })
    assert register.status_code == 202
    assert register.json()["status"] == "scheduled"

    # 已完成的内部改进派生记录（撤回后默认隔离）
    complete_intent = "intent-improve-000001"
    client.post("/api/consent/processing/intents", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_PRODUCT_IMPROVEMENT,
        "site_code": "expo-hall-b",
        "session_reference": "session-001",
        "record_type": "improvement_dataset",
        "idempotency_key": complete_intent,
    })
    assert client.post(f"/api/consent/processing/{complete_intent}/start").status_code == 200
    done = client.post(f"/api/consent/processing/{complete_intent}/complete", json={
        "derived_record_type": "improvement_dataset",
        "derived_record_ref": "dataset-batch-0007",
    })
    assert done.status_code == 200

    withdrawal = client.post("/api/consent/withdrawals", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_INSTANT_REPORT,
        "reason": "参与者撤回即时结论用途",
        "idempotency_key": "withdrawal-000001",
    })
    assert withdrawal.status_code == 201, withdrawal.text
    body = withdrawal.json()
    assert body["affected_decisions"] == 1
    assert body["stopped_intents"] == 1
    assert body["decision_snapshots"][0]["notice_version"] == 1

    # 撤回到达后，尚未开始的处理不能再启动
    late_start = client.post(f"/api/consent/processing/{intent_key}/start")
    assert late_start.status_code == 409
    assert late_start.json()["error"]["context"]["withdrawal_id"] == body["withdrawal_id"]

    # 同一撤回重放得到同一结果
    replay = client.post("/api/consent/withdrawals", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_INSTANT_REPORT,
        "reason": "参与者撤回即时结论用途",
        "idempotency_key": "withdrawal-000001",
    })
    assert replay.status_code == 201
    assert replay.json()["withdrawal_id"] == body["withdrawal_id"]
    assert replay.json()["replayed"] is True

    # 撤回内部改进：派生记录进入隔离队列
    withdrawal_all = client.post("/api/consent/withdrawals", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_PRODUCT_IMPROVEMENT,
        "idempotency_key": "withdrawal-000002",
    })
    assert withdrawal_all.status_code == 201
    assert withdrawal_all.json()["queued_derived"] == 1

    detail = client.get(f"/api/consent/withdrawals/{withdrawal_all.json()['withdrawal_id']}", headers=admin["headers"])
    assert detail.status_code == 200
    disposition = detail.json()["derived_dispositions"][0]
    assert disposition["record_ref"] == "dataset-batch-0007"
    assert disposition["status"] == "quarantined"
    assert detail.json()["stopped_intents"] == []


def test_retention_obligation_routes_derived_to_retain_and_due_delete_runs(client, admin):
    setup_catalog(client)
    clock = FrozenClock(datetime(2026, 10, 5, 1, 0, tzinfo=UTC))
    service = ConsentService(get_connection(), clock)

    partner = service.register_partner({
        "partner_code": "partner-lab",
        "name": "联合实验室",
        "partner_type": "研究机构",
        "region": "浙江",
        "retention_days": 365,
    })
    assert partner["partner_code"] == "partner-lab"
    notice = service.publish_notice({
        "notice_code": "lab-study",
        "title": "合作研究告知",
        "content_hash": "lab-notice-v1-hash-0001",
        "purposes": [PURPOSE_COLLECTION, PURPOSE_PARTNER_SHARING],
        "partners": ["partner-lab"],
        "created_by": "privacy-officer",
    })
    assert notice["version"] == 1
    service.submit_consent({
        "participant_digest": "participant-lab",
        "site_code": "expo-hall-b",
        "session_reference": "lab-session-1",
        "subject_type": "self",
        "notice_code": "lab-study",
        "decisions": [
            {"purpose_code": PURPOSE_COLLECTION, "decision": "allow"},
            {"purpose_code": PURPOSE_PARTNER_SHARING, "partner_code": "partner-lab", "decision": "allow"},
        ],
        "validity_days": 30,
        "idempotency_key": "consent-lab-000001",
    })

    # 登记一条带保存义务（尚未到期）的合作导出记录
    service.register_derived_record({
        "participant_digest": "participant-lab",
        "source_purpose_code": PURPOSE_PARTNER_SHARING,
        "source_partner_code": "partner-lab",
        "record_type": "partner_export",
        "record_ref": "export-0001",
        "notice_version": 1,
        "retention_basis": "合作协议约定的五年科研留存",
        "retention_until": "2031-10-05T00:00:00+00:00",
    })

    result = service.withdraw({
        "participant_digest": "participant-lab",
        "purpose_code": PURPOSE_PARTNER_SHARING,
        "partner_code": "partner-lab",
        "reason": "",
        "idempotency_key": "withdrawal-lab-0001",
    })
    assert result["queued_derived"] == 1
    detail = service.withdrawal_detail(result["withdrawal_id"])
    assert detail["derived_dispositions"][0]["status"] == "retained"
    assert "保存义务" in detail["derived_dispositions"][0]["disposition_reason"]


def test_expired_authorization_closes_the_gate(client, admin):
    setup_catalog(client)
    clock = FrozenClock(datetime(2026, 10, 5, 1, 0, tzinfo=UTC))
    service = ConsentService(get_connection(), clock)
    service.publish_notice({
        "notice_code": NOTICE["notice_code"],
        "title": NOTICE["title"],
        "content_hash": NOTICE["content_hash"],
        "purposes": [PURPOSE_COLLECTION, PURPOSE_FOLLOW_UP],
        "created_by": "privacy-officer",
    })
    service.submit_consent({
        "participant_digest": "participant-exp",
        "site_code": "expo-hall-b",
        "session_reference": "exp-1",
        "subject_type": "self",
        "notice_code": NOTICE["notice_code"],
        "decisions": [
            {"purpose_code": PURPOSE_COLLECTION, "decision": "allow"},
            {"purpose_code": PURPOSE_FOLLOW_UP, "decision": "allow"},
        ],
        "validity_days": 10,
        "idempotency_key": "consent-exp-000001",
    })
    gate = service.check_allowed("participant-exp", PURPOSE_FOLLOW_UP)
    assert gate["allowed"] is True

    clock.advance(days=11)
    expired = service.check_allowed("participant-exp", PURPOSE_FOLLOW_UP)
    assert expired["allowed"] is False
    assert "expired" in expired["reason"]


def test_audit_trace_follows_any_decision_to_notice_subject_and_disposition(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    client.post("/api/consent/records", json=consent_payload("consent-key-trace-001"))

    trace = client.get(
        f"/api/consent/decisions/trace?participant_digest=participant-001&purpose_code={PURPOSE_FOLLOW_UP}",
        headers=admin["headers"],
    )
    assert trace.status_code == 200
    current = trace.json()["current"]
    assert current["decision"] == "deny"
    assert current["notice_version"] == 1
    assert current["notice_content_hash"] == NOTICE["content_hash"]
    assert current["subject_type"] == "self"
    assert current["site_code"] == "expo-hall-b"
    assert len(trace.json()["history"]) == 1

    # 对外摘要只返回必要字段
    summary = client.get("/api/consent/participant-summary?participant_digest=participant-001")
    assert summary.status_code == 200
    purposes = {item["purpose_code"]: item for item in summary.json()["purposes"]}
    assert purposes[PURPOSE_FOLLOW_UP]["decision"] == "deny"
    assert "guardian_basis" not in summary.text

    # 审计事件记录了签署链路
    audit_events = client.get("/api/audit?resource_type=consent_record", headers=admin["headers"])
    assert audit_events.status_code == 200
    assert audit_events.json()["total"] >= 1


def test_guardian_consent_trace_contains_relation_and_basis(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    payload = consent_payload("consent-key-guard-001", participant="minor-007")
    payload["subject_type"] = "guardian"
    payload["guardian"] = {"relation": "母亲", "basis": "未成年人法定监护人", "guardian_digest": "guardian-007"}
    response = client.post("/api/consent/records", json=payload)
    assert response.status_code == 201

    detail = client.get(f"/api/consent/records/{response.json()['record_id']}", headers=admin["headers"])
    assert detail.json()["guardian_relation"] == "母亲"
    assert "法定监护" in detail.json()["guardian_basis"]


def test_delete_queue_is_processed_and_replayed_start_is_idempotent(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    participant = "participant-del"
    client.post("/api/consent/records", json=consent_payload("consent-key-del-0001", participant=participant))

    intent_key = "intent-del-000001"
    register_payload = {
        "participant_digest": participant,
        "purpose_code": PURPOSE_INSTANT_REPORT,
        "site_code": "expo-hall-b",
        "session_reference": "session-001",
        "record_type": "instant_report",
        "idempotency_key": intent_key,
    }
    first_register = client.post("/api/consent/processing/intents", json=register_payload)
    assert first_register.status_code == 202
    assert first_register.json()["replayed"] is False
    # 进程恢复后重复登记：只返回同一意图，不产生第二条
    second_register = client.post("/api/consent/processing/intents", json=register_payload)
    assert second_register.status_code == 202
    assert second_register.json()["replayed"] is True
    assert second_register.json()["intent_id"] == first_register.json()["intent_id"]

    # 跨场地重放同一处理键：拒绝
    cross_site = dict(register_payload)
    cross_site["site_code"] = "other-site"
    assert client.post("/api/consent/processing/intents", json=cross_site).status_code == 409

    start = client.post(f"/api/consent/processing/{intent_key}/start")
    assert start.status_code == 200
    # 重复启动只形成一个结果
    start_again = client.post(f"/api/consent/processing/{intent_key}/start")
    assert start_again.status_code == 200
    assert start_again.json()["intent_id"] == start.json()["intent_id"]

    complete = client.post(f"/api/consent/processing/{intent_key}/complete", json={
        "derived_record_type": "instant_report",
        "derived_record_ref": "report-del-0001",
    })
    assert complete.status_code == 200

    withdrawal = client.post("/api/consent/withdrawals", json={
        "participant_digest": participant,
        "purpose_code": PURPOSE_INSTANT_REPORT,
        "idempotency_key": "withdrawal-del-0001",
    })
    assert withdrawal.status_code == 201
    assert withdrawal.json()["queued_derived"] == 1

    processed = client.post("/api/consent/derived/process-due", headers=admin["headers"])
    assert processed.status_code == 200
    assert processed.json()["deleted"] == 1

    detail = client.get(
        f"/api/consent/decisions/trace?participant_digest={participant}&purpose_code={PURPOSE_INSTANT_REPORT}",
        headers=admin["headers"],
    )
    # 当前决定已撤回，但历史与来源记录仍可追溯
    assert detail.json()["current"]["status"] == "withdrawn"
    assert len(detail.json()["history"]) == 1


def test_suspended_partner_closes_sharing_gate(client, admin):
    setup_catalog(client)
    setup_partner_and_notice(client, admin)
    client.post("/api/consent/records", json=consent_payload("consent-key-partner-001", participant="participant-p"))

    gate = client.get(
        "/api/consent/check?participant_digest=participant-p&purpose_code=partner_sharing&partner_code=partner-univ"
    )
    assert gate.json()["allowed"] is True

    get_connection().execute("UPDATE consent_partners SET status='suspended' WHERE partner_code='partner-univ'")
    closed = client.get(
        "/api/consent/check?participant_digest=participant-p&purpose_code=partner_sharing&partner_code=partner-univ"
    )
    assert closed.json()["allowed"] is False
    assert "合作方" in closed.json()["reason"]
