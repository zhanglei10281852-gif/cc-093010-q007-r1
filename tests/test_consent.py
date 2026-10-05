from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.consent.service import ConsentService
from app.core.clock import FrozenClock
from app.database import get_connection
from app.pilots.service import PilotOperationsService

SUBJECT = "subject-digest-000000000001"

NOTICE_V1 = {
    "version_code": "notice-v1",
    "title": "数贸会健康体验告知书 v1",
    "body_digest": "sha256:" + "a" * 58,
    "purposes": ["collect", "instant_report", "internal_improvement", "follow_up_contact"],
    "created_by": "privacy-officer",
}

NOTICE_V2 = {
    "version_code": "notice-v2",
    "title": "数贸会健康体验告知书 v2（新增合作方共享）",
    "body_digest": "sha256:" + "b" * 58,
    "purposes": ["collect", "instant_report", "internal_improvement", "partner_sharing", "follow_up_contact"],
    "created_by": "privacy-officer",
}

ALL_PURPOSES_V1 = [
    {"purpose": "collect", "granted": True},
    {"purpose": "instant_report", "granted": True},
    {"purpose": "internal_improvement", "granted": True},
    {"purpose": "follow_up_contact", "granted": True},
]


def grant_payload(*, key: str, purposes, notice: str = "notice-v1", signer="self", guardian=None,
                  subject=SUBJECT, product=None, site=None, session_reference="", valid_days=30,
                  base_time=None):
    base = base_time or datetime.now(UTC)
    payload = {
        "subject_ref": "P-0001",
        "subject_digest": subject,
        "signer_type": signer,
        "notice_version_code": notice,
        "purposes": purposes,
        "idempotency_key": key,
        "valid_until": (base + timedelta(days=valid_days)).isoformat(),
    }
    if guardian:
        payload["guardian"] = guardian
    if product:
        payload["product_code"] = product
    if site:
        payload["site_code"] = site
    if session_reference:
        payload["session_reference"] = session_reference
    return payload


def publish(client, notice=NOTICE_V1):
    response = client.post("/api/consent/notices", json=notice)
    assert response.status_code == 201, response.text


def setup_product_site(client):
    product = client.post("/api/catalog/products", json={
        "code": "ai-tongue", "name": "AI舌诊体验设备", "organization": "数智中医",
        "origin_country": "中国", "category": "数字中医",
        "intended_use": "用于展会现场舌象采集与即时体质倾向体验，不替代临床诊断",
        "risk_level": "medium",
    })
    assert product.status_code == 201, product.text
    site = client.post("/api/catalog/sites", json={
        "code": "dte-main", "name": "数贸会主会场", "site_type": "展会体验点",
        "region": "杭州", "capabilities": ["tongue"], "max_concurrent": 2,
    })
    assert site.status_code == 201, site.text
    site_b = client.post("/api/catalog/sites", json={
        "code": "dte-branch", "name": "数贸会分会场", "site_type": "展会体验点",
        "region": "杭州", "capabilities": ["tongue"], "max_concurrent": 2,
    })
    assert site_b.status_code == 201, site_b.text
    return "ai-tongue"


# ---- 告知与授权 ----

def test_notice_publish_and_listing(client):
    publish(client)
    notices = client.get("/api/consent/notices").json()["items"]
    assert notices[0]["version_code"] == "notice-v1"
    assert "partner_sharing" not in notices[0]["purposes"]


def test_grant_requires_guardian_relation_and_basis(client):
    publish(client)
    missing = client.post("/api/consent/grants", json=grant_payload(
        key="grant-guardian-1", purposes=ALL_PURPOSES_V1, signer="guardian"))
    assert missing.status_code == 422
    ok = client.post("/api/consent/grants", json=grant_payload(
        key="grant-guardian-2", purposes=ALL_PURPOSES_V1, signer="guardian",
        guardian={"relation": "父亲", "basis": "民法典第二十七条：父母是未成年子女的监护人", "signer_display": "张某父亲"}))
    assert ok.status_code == 201, ok.text
    assert ok.json()["signer_type"] == "guardian"
    assert ok.json()["guardian_relation"] == "父亲"
    assert "民法典" in ok.json()["guardian_basis"]


def test_new_purpose_cannot_reuse_old_decision(client):
    publish(client)
    response = client.post("/api/consent/grants", json=grant_payload(
        key="grant-old-notice", purposes=ALL_PURPOSES_V1 + [
            {"purpose": "partner_sharing", "granted": True, "partner_code": "lab-x"}]))
    assert response.status_code == 422
    publish(client, NOTICE_V2)
    response = client.post("/api/consent/grants", json=grant_payload(
        key="grant-new-notice", notice="notice-v2",
        purposes=ALL_PURPOSES_V1 + [
            {"purpose": "partner_sharing", "granted": True, "partner_code": "lab-x"}]))
    assert response.status_code == 201, response.text


def test_partner_sharing_requires_partner_code(client):
    publish(client, NOTICE_V2)
    bad = client.post("/api/consent/grants", json=grant_payload(
        key="grant-partner-bad", notice="notice-v2",
        purposes=[{"purpose": "partner_sharing", "granted": True}]))
    assert bad.status_code == 422


# ---- 幂等、重放、恢复 ----

def test_duplicate_request_and_cross_site_replay_form_one_decision(client):
    setup_product_site(client)
    publish(client)
    payload = grant_payload(key="grant-replay", purposes=ALL_PURPOSES_V1, site="dte-main")
    first = client.post("/api/consent/grants", json=payload)
    assert first.status_code == 201
    second = client.post("/api/consent/grants", json=payload)
    assert second.status_code == 201 and second.json()["grant_key"] == first.json()["grant_key"]
    # 跨场地重放：不同场地、不同幂等键，但决定内容相同 → 同一最终决定
    replay = client.post("/api/consent/grants", json=grant_payload(
        key="grant-replay-other-site", purposes=ALL_PURPOSES_V1, site="dte-branch"))
    assert replay.json()["grant_key"] == first.json()["grant_key"]
    assert len(first.json()["events"]) == 1


def test_different_decisions_create_separate_grants(client):
    publish(client)
    granted = client.post("/api/consent/grants", json=grant_payload(key="grant-a", purposes=ALL_PURPOSES_V1))
    denied_report = [
        {"purpose": "collect", "granted": True},
        {"purpose": "instant_report", "granted": False},
        {"purpose": "internal_improvement", "granted": True},
        {"purpose": "follow_up_contact", "granted": False},
    ]
    other = client.post("/api/consent/grants", json=grant_payload(key="grant-b", purposes=denied_report))
    assert other.status_code == 201
    assert other.json()["grant_key"] != granted.json()["grant_key"]


def test_cannot_grant_downstream_without_collect(client):
    publish(client)
    response = client.post("/api/consent/grants", json=grant_payload(key="grant-no-collect", purposes=[
        {"purpose": "collect", "granted": False},
        {"purpose": "instant_report", "granted": True},
    ]))
    assert response.status_code == 422


# ---- 业务链路门控 ----

def test_session_submit_gated_by_collect_consent(client):
    setup_product_site(client)
    protocol = client.post("/api/pilots/protocols?actor=op", json={
        "code": "tongue", "name": "AI舌诊方案", "capability": "tongue",
        "parameter_schema": {"seconds": {"type": "integer", "required": True, "minimum": 1, "maximum": 60}},
        "default_parameters": {}, "max_runtime_seconds": 300, "max_attempts": 2,
    })
    assert protocol.status_code == 201
    payload = {
        "protocol_code": "tongue", "project_code": "dte-2026", "requested_by": "op-1",
        "parameters": {"seconds": 10}, "priority": 50, "idempotency_key": "session-consent-1",
        "subject_digest": SUBJECT,
    }
    blocked = client.post("/api/pilots/sessions", json=payload)
    assert blocked.status_code == 409
    publish(client)
    client.post("/api/consent/grants", json=grant_payload(key="grant-session-1", purposes=ALL_PURPOSES_V1))
    accepted = client.post("/api/pilots/sessions", json=payload)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["consent_grant_id"] is not None


def test_instant_report_denied_blocks_completion(client):
    setup_product_site(client)
    publish(client)
    client.post("/api/consent/grants", json=grant_payload(key="grant-report-denied", purposes=[
        {"purpose": "collect", "granted": True},
        {"purpose": "instant_report", "granted": False},
        {"purpose": "internal_improvement", "granted": True},
        {"purpose": "follow_up_contact", "granted": False},
    ]))
    _seed_session(client)
    claimed = client.post("/api/pilots/sessions/claim", json={
        "site_code": "dte-main", "capabilities": ["tongue"], "lease_seconds": 120})
    session_id = claimed.json()["session"]["id"]
    blocked = client.post(f"/api/pilots/sessions/{session_id}/complete", json={
        "site_code": "dte-main", "observation": {"constitution": "气虚"}, "metrics": {}})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["reason"] in {"denied"}


def _seed_session(client) -> int:
    client.post("/api/pilots/protocols?actor=op", json={
        "code": "tongue", "name": "AI舌诊方案", "capability": "tongue",
        "parameter_schema": {"seconds": {"type": "integer"}},
        "default_parameters": {"seconds": 5}, "max_runtime_seconds": 300, "max_attempts": 2,
    })
    response = client.post("/api/pilots/sessions", json={
        "protocol_code": "tongue", "project_code": "dte-2026", "requested_by": "op-2",
        "parameters": {}, "priority": 50, "idempotency_key": "session-report-1",
        "subject_digest": SUBJECT,
    })
    return response.json()["id"]


def test_feedback_requires_internal_improvement_consent(client):
    setup_product_site(client)
    publish(client)
    client.post("/api/consent/grants", json=grant_payload(key="grant-feedback-only", purposes=[
        {"purpose": "collect", "granted": True},
        {"purpose": "instant_report", "granted": True},
        {"purpose": "internal_improvement", "granted": False},
        {"purpose": "follow_up_contact", "granted": False},
    ]))
    blocked = client.post("/api/catalog/feedback", json={
        "product_code": "ai-tongue", "site_code": "dte-main", "session_reference": "S-1",
        "audience_type": "公众", "rating": 5, "tags": ["准确"], "comment": "",
        "contact_digest": "", "consent_to_follow_up": False, "subject_digest": SUBJECT,
    })
    assert blocked.status_code == 409


# ---- 撤回与处置 ----

def test_withdrawal_stops_pending_and_disposes_derived_records(client):
    setup_product_site(client)
    publish(client)
    grant = client.post("/api/consent/grants", json=grant_payload(key="grant-withdraw-1", purposes=ALL_PURPOSES_V1))
    grant_id = grant.json()["grant_key"]
    # 一条排队中场次（撤回采集应立即取消）
    client.post("/api/pilots/protocols?actor=op", json={
        "code": "tongue", "name": "AI舌诊方案", "capability": "tongue",
        "parameter_schema": {"seconds": {"type": "integer"}},
        "default_parameters": {"seconds": 5}, "max_runtime_seconds": 300, "max_attempts": 2,
    })
    queued = client.post("/api/pilots/sessions", json={
        "protocol_code": "tongue", "project_code": "dte-2026", "requested_by": "op-3",
        "parameters": {}, "priority": 50, "idempotency_key": "session-withdraw-1",
        "subject_digest": SUBJECT,
    }).json()
    # 一条已完成的反馈（内部改进派生记录）
    feedback = client.post("/api/catalog/feedback", json={
        "product_code": "ai-tongue", "site_code": "dte-main", "session_reference": "S-W",
        "audience_type": "公众", "rating": 4, "tags": [], "comment": "",
        "contact_digest": "contact-digest-0001", "consent_to_follow_up": True, "subject_digest": SUBJECT,
    })
    assert feedback.status_code == 201, feedback.text

    withdrawal = client.post("/api/consent/withdrawals", json={
        "grant_key": grant_id, "purposes": ["internal_improvement"],
        "reason": "参与者不再允许将数据用于产品改进", "actor_name": "privacy-officer",
        "idempotency_key": "withdraw-1",
    })
    assert withdrawal.status_code == 200, withdrawal.text
    detail = client.get("/api/consent/grants/" + grant_id, headers=_admin_headers(client)).json()
    assert detail["status"] == "active"  # 仅撤回部分用途
    # 只撤回内部改进不应取消场次
    assert client.get(f"/api/pilots/session-details/{queued['id']}").json()["status"] == "queued"

    full = client.post("/api/consent/withdrawals", json={
        "grant_key": grant_id,
        "reason": "参与者撤回全部授权", "actor_name": "privacy-officer",
        "idempotency_key": "withdraw-2",
    })
    assert full.status_code == 200
    assert client.get(f"/api/pilots/session-details/{queued['id']}").json()["status"] == "cancelled"
    detail = client.get("/api/consent/grants/" + grant_id, headers=_admin_headers(client)).json()
    assert detail["status"] == "withdrawn"

    queued_count = client.get("/api/consent/dispositions", headers=_admin_headers(client)).json()["queued"]
    assert queued_count >= 2
    run = client.post("/api/consent/dispositions/run", json={"limit": 50, "actor_name": "privacy-job"},
                      headers=_admin_headers(client))
    assert run.status_code == 200, run.text
    results = {(item["resource"], item["result"]) for item in run.json()["processed"]}
    assert any(result in {"quarantined", "contact_suppressed", "deleted"}
               for _, result in results)
    # 被隔离/删除的反馈不再进入分析
    summary = client.get("/api/catalog/feedback/summary?product_code=ai-tongue").json()["items"][0]
    assert summary["feedback_count"] == 0


def test_withdrawal_idempotent(client):
    publish(client)
    grant = client.post("/api/consent/grants", json=grant_payload(key="grant-wd-idem", purposes=ALL_PURPOSES_V1)).json()
    payload = {"grant_key": grant["grant_key"], "reason": "重复撤回请求", "actor_name": "officer",
               "idempotency_key": "withdraw-idem-1"}
    first = client.post("/api/consent/withdrawals", json=payload)
    second = client.post("/api/consent/withdrawals", json=payload)
    assert first.status_code == second.status_code == 200
    detail = client.get("/api/consent/grants/" + grant["grant_key"], headers=_admin_headers(client)).json()
    assert len(detail["events"]) == 2  # 一次签署 + 一次撤回


# ---- 有效期 ----

def test_expired_grant_blocks_processing(client):
    publish(client)
    base = datetime(2026, 9, 1, tzinfo=UTC)
    clock = FrozenClock(base)
    service = ConsentService(get_connection(), clock)
    service = ConsentService(get_connection(), clock)
    payload = grant_payload(key="grant-expiry", purposes=ALL_PURPOSES_V1, valid_days=10, base_time=base)
    payload["valid_until"] = base + timedelta(days=10)
    grant = service.record_grant(payload)
    clock.advance(days=11)
    decision = service.effective_decision(subject_digest=SUBJECT, purpose="collect")
    assert decision["permitted"] is False and decision["reason"] == "expired"
    grant_row_id = service.repository.grant_by_key(grant["grant_key"])["id"]
    result = service.expire_due_grants()
    assert result["expired_grant_ids"] == [grant_row_id]
    detail = service.grant_for_key(grant["grant_key"])
    assert detail["status"] == "expired"
    assert detail["events"][-1]["event_type"] == "expired"


# ---- 对外摘要与审计追溯 ----

def test_subject_summary_is_minimal_but_grant_detail_is_traceable(client, admin):
    setup_product_site(client)
    publish(client)
    grant = client.post("/api/consent/grants", json=grant_payload(key="grant-trace", purposes=ALL_PURPOSES_V1)).json()
    summary = client.get(f"/api/consent/subjects/{SUBJECT}/summary")
    assert summary.status_code == 200
    states = {item["purpose"]: item for item in summary.json()["purposes"]}
    assert set(states) == {"collect", "instant_report", "internal_improvement", "follow_up_contact"}
    assert all(item["notice_version_code"] == "notice-v1" for item in states.values())
    assert "guardian_basis" not in summary.text and "subject_ref" not in summary.text

    # 未认证不能看授权详情
    assert client.get("/api/consent/grants/" + grant["grant_key"]).status_code == 401
    detail = client.get("/api/consent/grants/" + grant["grant_key"], headers=admin["headers"])
    assert detail.status_code == 200
    body = detail.json()
    assert body["notice_version_code"] == "notice-v1"
    assert body["signer_type"] == "self"
    assert {d["purpose"] for d in body["decisions"]} == set(states)
    assert body["events"][0]["event_type"] in {"granted", "denied"}

    # 审计人员可从授权追到审计事件
    audit = client.get("/api/audit?resource_type=consent_grant&size=20", headers=admin["headers"])
    assert audit.status_code == 200
    assert any(item["action"] == "consent.decided" for item in audit.json()["data"])


def test_partner_export_filters_by_partner_specific_consent(client):
    setup_product_site(client)
    publish(client, NOTICE_V2)
    # 参与者 A：授权共享给 lab-x；参与者 B：拒绝任何共享
    client.post("/api/consent/grants", json=grant_payload(
        key="grant-partner-a", notice="notice-v2", subject=SUBJECT,
        purposes=ALL_PURPOSES_V1 + [
            {"purpose": "partner_sharing", "granted": True, "partner_code": "lab-x",
             "partner_scope": {"datasets": ["feedback_ratings"]}}]))
    client.post("/api/consent/grants", json=grant_payload(
        key="grant-partner-b", notice="notice-v2", subject="subject-digest-000000000002",
        purposes=ALL_PURPOSES_V1 + [{"purpose": "partner_sharing", "granted": False}]))
    for subject, contact, key in (
        (SUBJECT, "contact-a", "fb-a"),
        ("subject-digest-000000000002", "contact-b", "fb-b"),
    ):
        response = client.post("/api/catalog/feedback", json={
            "product_code": "ai-tongue", "site_code": "dte-main", "session_reference": key,
            "audience_type": "公众", "rating": 4, "tags": ["t"], "comment": "",
            "contact_digest": contact, "consent_to_follow_up": False, "subject_digest": subject,
        })
        assert response.status_code == 201, response.text

    export = client.post("/api/catalog/feedback/partner-export/ai-tongue", json={"partner_code": "lab-x"})
    assert export.status_code == 200, export.text
    body = export.json()
    assert body["partner_code"] == "lab-x"
    assert body["notice_basis"] == ["notice-v2"]
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert "comment" not in item and "contact_digest" not in item
    # 未获授权的合作方拿不到数据
    other = client.post("/api/catalog/feedback/partner-export/ai-tongue", json={"partner_code": "lab-y"})
    assert other.json()["items"] == []


def _admin_headers(client):
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "t"})
    login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "t"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}
