"""End-to-end API integration tests across all personas, compliance, COI, HIPAA, and Swagger."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.domain.types import UserRole


def _future_service_date(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def test_swagger_docs_and_openapi_schema_are_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200

    schema_resp = client.get("/openapi.json")
    assert schema_resp.status_code == 200
    schema = schema_resp.json()
    assert schema["info"]["title"] == "Oxbridge Health Prior Authorization API"
    assert "BearerJWT" in schema["components"]["securitySchemes"]
    assert "/api/v1/provider/requests" in schema["paths"]
    assert "/api/v1/member/requests" in schema["paths"]
    assert "/api/v1/reviewer/queue/claim-next" in schema["paths"]
    assert "/api/v1/admin/requests/{request_id}/reassign" in schema["paths"]


def test_health_and_dev_token_minter(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok"}
    assert "X-Request-ID" in health.headers

    users_resp = client.get("/api/v1/dev/users")
    assert users_resp.status_code == 200
    assert len(users_resp.json()) >= 7

    token_resp = client.post("/api/v1/dev/tokens", json={"user_id": "user-prov-maine"})
    assert token_resp.status_code == 200
    access_token = token_resp.json()["access_token"]

    me_requests = client.get(
        "/api/v1/provider/requests",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_requests.status_code == 200


def test_auth_rejects_missing_bad_token_and_wrong_role(
    client: TestClient, auth_headers
) -> None:
    assert client.get("/api/v1/provider/requests").status_code == 401
    assert (
        client.get(
            "/api/v1/provider/requests",
            headers={"Authorization": "Bearer not-a-real-jwt"},
        ).status_code
        == 401
    )

    member_hdr = auth_headers("user-mem-alice", UserRole.MEMBER)
    forbidden = client.get("/api/v1/admin/requests", headers=member_hdr)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "INSUFFICIENT_ROLE"


def test_full_prior_auth_lifecycle_coi_hipaa_audit_and_notifications(
    client: TestClient, auth_headers
) -> None:
    prov_maine = auth_headers("user-prov-maine", UserRole.PROVIDER_OFFICE, "prac-maine")
    prov_hawaii = auth_headers("user-prov-hawaii", UserRole.PROVIDER_OFFICE, "prac-hawaii")
    mem_alice = auth_headers("user-mem-alice", UserRole.MEMBER)
    mem_bob = auth_headers("user-mem-bob", UserRole.MEMBER)
    rev_nurse = auth_headers("user-rev-nurse", UserRole.REVIEWER)
    rev_dr_smith = auth_headers("user-rev-dr-smith", UserRole.REVIEWER)
    admin_hdr = auth_headers("user-admin-1", UserRole.ADMIN)

    secret_clinical_note = "CONFIDENTIAL CLINICAL NOTE: positive straight-leg raise."
    secret_denial_reason = "INTERNAL REASONING: missing 6-week conservative PT documentation."

    # 1. Provider submits a request for Alice with an Idempotency-Key.
    payload = {
        "member_id": "mem-alice",
        "diagnosis_code": "M54.50",
        "date_of_service": _future_service_date(20),
        "procedures": [
            {"code_type": "CPT", "code": "72148", "units": 1},
            {"code_type": "CPT", "code": "97110", "units": 12},
        ],
        "clinical_note": secret_clinical_note,
    }
    create_resp = client.post(
        "/api/v1/provider/requests",
        json=payload,
        headers={**prov_maine, "Idempotency-Key": "idem-alice-001"},
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    req_id = created["id"]
    assert created["status"] == "submitted"
    assert len(created["procedures"]) == 2

    # Idempotent retry returns the exact same request with 200 OK.
    replay_resp = client.post(
        "/api/v1/provider/requests",
        json=payload,
        headers={**prov_maine, "Idempotency-Key": "idem-alice-001"},
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json()["id"] == req_id
    assert replay_resp.headers.get("Idempotency-Replayed") == "true"

    # Hawaii office cannot see Maine's request.
    assert client.get(f"/api/v1/provider/requests/{req_id}", headers=prov_hawaii).status_code == 404

    # 2. Conflict of Interest: Dr. Smith treats Alice, so queue hides/skips it & direct claim is 403.
    smith_queue = client.get("/api/v1/reviewer/queue", headers=rev_dr_smith)
    assert smith_queue.status_code == 200
    assert smith_queue.json() == []
    assert smith_queue.headers.get("Cache-Control") == "no-store"

    nurse_queue = client.get("/api/v1/reviewer/queue", headers=rev_nurse)
    assert nurse_queue.status_code == 200
    assert [item["id"] for item in nurse_queue.json()] == [req_id]

    skip_resp = client.post("/api/v1/reviewer/queue/claim-next", headers=rev_dr_smith)
    assert skip_resp.status_code == 404
    assert skip_resp.json()["code"] == "QUEUE_EMPTY"

    direct_coi = client.post(f"/api/v1/reviewer/requests/{req_id}/claim", headers=rev_dr_smith)
    assert direct_coi.status_code == 403
    assert direct_coi.json()["code"] == "CONFLICT_OF_INTEREST"

    # 3. Nurse Jordan claims next from queue.
    claim_resp = client.post("/api/v1/reviewer/queue/claim-next", headers=rev_nurse)
    assert claim_resp.status_code == 200
    claimed = claim_resp.json()
    assert claimed["id"] == req_id
    assert claimed["status"] == "in_review"
    assert claimed["assigned_reviewer_id"] == "user-rev-nurse"

    # Wishlist: Admin checks reviewer workload.
    workload_resp = client.get("/api/v1/admin/reviewers/workload", headers=admin_hdr)
    assert workload_resp.status_code == 200
    workload_map = {w["reviewer_id"]: w["open_request_count"] for w in workload_resp.json()}
    assert workload_map["user-rev-nurse"] == 1
    assert workload_map["user-rev-dr-smith"] == 0

    # Admin cannot reassign Alice's request to Dr. Smith due to COI.
    bad_reassign = client.post(
        f"/api/v1/admin/requests/{req_id}/reassign",
        json={"reviewer_id": "user-rev-dr-smith", "reason": "Load balancing"},
        headers=admin_hdr,
    )
    assert bad_reassign.status_code == 409
    assert bad_reassign.json()["code"] == "CONFLICT_OF_INTEREST"

    # 4. Nurse submits atomic partial approval decision.
    proc1_id = created["procedures"][0]["id"]
    proc2_id = created["procedures"][1]["id"]
    decision_resp = client.post(
        f"/api/v1/reviewer/requests/{req_id}/decision",
        json={
            "decisions": [
                {"procedure_id": proc1_id, "decision": "approved"},
                {
                    "procedure_id": proc2_id,
                    "decision": "denied",
                    "denial_reason": secret_denial_reason,
                },
            ]
        },
        headers=rev_nurse,
    )
    assert decision_resp.status_code == 200
    decided = decision_resp.json()
    assert decided["status"] == "partially_approved"
    assert decided["decided_by"] == "user-rev-nurse"
    assert decided["decided_at"] is not None

    # Decisions are final: attempting to decide again fails with 409.
    retry_decision = client.post(
        f"/api/v1/reviewer/requests/{req_id}/decision",
        json={
            "decisions": [
                {"procedure_id": proc1_id, "decision": "approved"},
                {"procedure_id": proc2_id, "decision": "approved"},
            ]
        },
        headers=rev_nurse,
    )
    assert retry_decision.status_code == 409
    assert retry_decision.json()["code"] == "REQUEST_ALREADY_DECIDED"

    # 5. Member HIPAA visibility check: sees outcomes, NEVER clinical_note or denial_reason.
    member_resp = client.get(f"/api/v1/member/requests/{req_id}", headers=mem_alice)
    assert member_resp.status_code == 200
    member_body = member_resp.json()
    assert "clinical_note" not in member_body
    for proc in member_body["procedures"]:
        assert "denial_reason" not in proc
    assert secret_clinical_note not in member_resp.text
    assert secret_denial_reason not in member_resp.text
    assert {p["decision"] for p in member_body["procedures"]} == {"approved", "denied"}

    # Bob cannot view Alice's request.
    assert client.get(f"/api/v1/member/requests/{req_id}", headers=mem_bob).status_code == 404

    # 6. Provider sees decision notification (wishlist).
    notif_resp = client.get("/api/v1/provider/notifications", headers=prov_maine)
    assert notif_resp.status_code == 200
    notifs = notif_resp.json()
    assert len(notifs) == 1
    assert notifs[0]["request_id"] == req_id
    assert notifs[0]["payload"]["status"] == "partially_approved"

    # 7. Admin sees complete compliance audit trail.
    admin_detail = client.get(f"/api/v1/admin/requests/{req_id}", headers=admin_hdr)
    assert admin_detail.status_code == 200
    events = admin_detail.json()["audit_events"]
    assert [e["event_type"] for e in events] == [
        "request_submitted",
        "request_claimed",
        "request_decided",
    ]
    assert events[2]["actor_id"] == "user-rev-nurse"


def test_rejected_submission_returns_all_errors_and_is_traceable_by_support(
    client: TestClient, auth_headers
) -> None:
    prov_maine = auth_headers("user-prov-maine", UserRole.PROVIDER_OFFICE, "prac-maine")
    admin_hdr = auth_headers("user-admin-1", UserRole.ADMIN)

    bad_resp = client.post(
        "/api/v1/provider/requests",
        json={
            "member_id": "mem-carol",  # terminated coverage
            "diagnosis_code": "BAD",
            "date_of_service": _future_service_date(15),
            "procedures": [{"code_type": "CPT", "code": "97110", "units": 45}],
        },
        headers={**prov_maine, "X-Request-ID": "support-trace-123"},
    )
    assert bad_resp.status_code == 422
    body = bad_resp.json()
    assert body["request_id"] == "support-trace-123"
    returned_codes = {e["code"] for e in body["errors"]}
    assert returned_codes == {
        "COVERAGE_INACTIVE",
        "INVALID_DIAGNOSIS_CODE",
        "HIGH_UNITS_CONFIRMATION_REQUIRED",
    }

    # Support desk looks up the failed attempt by X-Request-ID without seeing PHI.
    support_resp = client.get(
        "/api/v1/admin/support/submissions",
        params={"request_id": "support-trace-123"},
        headers=admin_hdr,
    )
    assert support_resp.status_code == 200
    attempts = support_resp.json()
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "rejected"
    assert attempts[0]["practice_id"] == "prac-maine"
    assert set(attempts[0]["error_codes"]) == returned_codes


def test_admin_can_reassign_when_no_conflict_of_interest(
    client: TestClient, auth_headers
) -> None:
    prov_hawaii = auth_headers("user-prov-hawaii", UserRole.PROVIDER_OFFICE, "prac-hawaii")
    rev_nurse = auth_headers("user-rev-nurse", UserRole.REVIEWER)
    rev_dr_smith = auth_headers("user-rev-dr-smith", UserRole.REVIEWER)
    admin_hdr = auth_headers("user-admin-1", UserRole.ADMIN)

    # Bob is linked to Hawaii practice and NOT treated by Dr. Smith.
    create_resp = client.post(
        "/api/v1/provider/requests",
        json={
            "member_id": "mem-bob",
            "diagnosis_code": "E11.9",
            "date_of_service": _future_service_date(10),
            "procedures": [{"code_type": "HCPCS", "code": "J1745", "units": 40, "confirm_high_units": True}],
        },
        headers=prov_hawaii,
    )
    assert create_resp.status_code == 201
    req_id = create_resp.json()["id"]
    proc_id = create_resp.json()["procedures"][0]["id"]

    assert client.post(f"/api/v1/reviewer/requests/{req_id}/claim", headers=rev_nurse).status_code == 200

    # Admin reassigns to Dr. Smith.
    reassign_resp = client.post(
        f"/api/v1/admin/requests/{req_id}/reassign",
        json={"reviewer_id": "user-rev-dr-smith", "reason": "Specialist review needed"},
        headers=admin_hdr,
    )
    assert reassign_resp.status_code == 200
    assert reassign_resp.json()["assigned_reviewer_id"] == "user-rev-dr-smith"

    # Nurse can no longer decide it; Dr. Smith can.
    nurse_attempt = client.post(
        f"/api/v1/reviewer/requests/{req_id}/decision",
        json={"decisions": [{"procedure_id": proc_id, "decision": "approved"}]},
        headers=rev_nurse,
    )
    assert nurse_attempt.status_code == 403

    smith_decision = client.post(
        f"/api/v1/reviewer/requests/{req_id}/decision",
        json={"decisions": [{"procedure_id": proc_id, "decision": "approved"}]},
        headers=rev_dr_smith,
    )
    assert smith_decision.status_code == 200
    assert smith_decision.json()["status"] == "approved"
    assert smith_decision.json()["decided_by"] == "user-rev-dr-smith"


def test_schema_level_submission_rejection_is_also_traceable_by_support(
    client: TestClient, auth_headers
) -> None:
    prov_maine = auth_headers("user-prov-maine", UserRole.PROVIDER_OFFICE, "prac-maine")
    admin_hdr = auth_headers("user-admin-1", UserRole.ADMIN)

    # Malformed date and invalid code_type fail Pydantic schema validation before domain layer.
    resp = client.post(
        "/api/v1/provider/requests",
        json={
            "member_id": "mem-alice",
            "diagnosis_code": "M54.50",
            "date_of_service": "not-a-date",
            "procedures": [{"code_type": "UNKNOWN", "code": "72148", "units": 1}],
        },
        headers={**prov_maine, "X-Request-ID": "schema-trace-456"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_REQUEST_SCHEMA"
    assert resp.json()["request_id"] == "schema-trace-456"

    support_resp = client.get(
        "/api/v1/admin/support/submissions",
        params={"request_id": "schema-trace-456"},
        headers=admin_hdr,
    )
    assert support_resp.status_code == 200
    attempts = support_resp.json()
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "rejected"
    assert "INVALID_REQUEST_SCHEMA" in attempts[0]["error_codes"]

