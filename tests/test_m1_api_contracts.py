from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import pytest

from invoice_assistant import file_operations
from invoice_assistant.db import connect_db, json_dump, transaction, utc_now
from invoice_assistant.file_operations import recover_incomplete_file_operations
from invoice_assistant.idempotency import request_fingerprint

from .conftest import (
    add_attachment,
    confirm_item,
    create_confirmed_item,
    image_bytes,
    item_version_fields,
)


def agent_headers(tool_name: str, operation_id: str | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": operation_id or str(uuid.uuid4()),
        "X-Invoice-Agent-Tool": tool_name,
    }


def manual_agent_payload(**changes):
    result = {
        "merchant": "Agent 商户",
        "expense_date": "2026-08-20",
        "amount_cents": 12345,
        "currency": "CNY",
        "purpose": "Agent 合同测试",
        "project_id": 1,
    }
    result.update(changes)
    return result


def test_agent_database_write_idempotency_and_operation_snapshot(client):
    operation_id = str(uuid.uuid4())
    headers = agent_headers("create_manual_invoice_draft", operation_id)
    created = client.post("/api/items/manual", json=manual_agent_payload(), headers=headers)
    assert created.status_code == 201, created.get_json()
    payload = created.get_json()
    assert payload["meta"] == {"replayed": False}
    assert payload["item"]["amount_cents"] == 12345
    safe_snapshot = payload["operation_result"]

    replay = client.post("/api/items/manual", json=manual_agent_payload(), headers=headers)
    assert replay.status_code == 201
    assert replay.get_json() == {
        "operation_result": safe_snapshot,
        "meta": {"replayed": True},
    }
    assert client.get("/api/drafts").get_json()["count"] == 1

    mismatch = client.post(
        "/api/items/manual",
        json=manual_agent_payload(amount_cents=12346),
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "idempotency_mismatch"

    status = client.get(f"/api/agent-operations/{operation_id}")
    assert status.status_code == 200
    operation = status.get_json()["operation"]
    assert operation["status"] == "succeeded"
    assert operation["outcome"] == "applied"
    assert operation["operation_result"] == safe_snapshot
    assert "request_fingerprint" not in operation

    float_request = client.post(
        "/api/items/manual",
        json={**manual_agent_payload(), "amount": 0.1},
        headers=agent_headers("create_manual_invoice_draft"),
    )
    assert float_request.status_code == 400
    assert float_request.get_json()["error"] in {"amount_field_conflict", "json_float_not_allowed"}


def test_conditional_update_failure_is_durable_and_replayable(client):
    item_id = create_confirmed_item(client, merchant="并发版本商户")
    original = client.get(f"/api/items/{item_id}").get_json()["item"]
    first = client.patch(
        f"/api/items/{item_id}",
        json={"expected_version": original["version"], "purpose": "第一次更新"},
        headers=agent_headers("update_invoice_item"),
    )
    assert first.status_code == 200

    failed_id = str(uuid.uuid4())
    failed_headers = agent_headers("update_invoice_item", failed_id)
    stale_body = {"expected_version": original["version"], "purpose": "过期更新"}
    stale = client.patch(
        f"/api/items/{item_id}", json=stale_body, headers=failed_headers
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "stale_version"
    replay = client.patch(
        f"/api/items/{item_id}", json=stale_body, headers=failed_headers
    )
    assert replay.status_code == 409
    assert replay.get_json()["error"] == "stale_version"
    operation = client.get(f"/api/agent-operations/{failed_id}").get_json()["operation"]
    assert operation["status"] == "failed"
    assert operation["outcome"] == "not_applied"


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"currency": "!"}, "invalid_currency"),
        ({"project_id": 999_999}, "invalid_project"),
    ],
)
def test_manual_prevalidation_failure_is_terminal_and_parameter_bound(
    client, changes, error_code
):
    operation_id = str(uuid.uuid4())
    headers = agent_headers("create_manual_invoice_draft", operation_id)
    invalid_payload = manual_agent_payload(**changes)
    first = client.post("/api/items/manual", json=invalid_payload, headers=headers)
    assert first.status_code == 400
    assert first.get_json()["error"] == error_code
    assert first.get_json()["outcome"] == "not_applied"

    replay = client.post("/api/items/manual", json=invalid_payload, headers=headers)
    assert replay.status_code == 400
    assert replay.get_json()["error"] == error_code
    assert replay.get_json()["meta"] == {"replayed": True}

    changed = client.post(
        "/api/items/manual", json=manual_agent_payload(), headers=headers
    )
    assert changed.status_code == 409
    assert changed.get_json()["error"] == "idempotency_mismatch"
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert (operation["status"], operation["http_status"], operation["error_code"]) == (
        "failed",
        400,
        error_code,
    )


def test_structural_write_failure_is_durable_before_entity_validation(client):
    operation_id = str(uuid.uuid4())
    headers = agent_headers("create_reimbursement_batch", operation_id)
    invalid = {"name": "无条目", "items": []}
    first = client.post("/api/batches", json=invalid, headers=headers)
    assert first.status_code == 400
    assert first.get_json()["error"] == "items_required"
    replay = client.post("/api/batches", json=invalid, headers=headers)
    assert replay.status_code == 400
    assert replay.get_json()["meta"] == {"replayed": True}
    mismatch = client.post(
        "/api/batches",
        json={"name": "无条目", "items": [{"item_id": 1, "expected_version": 0}]},
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "idempotency_mismatch"


def test_confirm_and_merge_prevalidation_failures_are_durable(client):
    draft = client.post(
        "/api/items/manual",
        json={
            "merchant": "确认预校验",
            "expense_date": "2026-08-23",
            "amount": 10,
            "currency": "CNY",
            "purpose": "确认预校验",
            "project_id": 1,
        },
    ).get_json()["item"]
    detail = client.get(f"/api/items/{draft['id']}").get_json()["item"]
    invalid_confirm = {
        "expected_version": detail["version"],
        "review_token": detail["review"]["token"],
        "duplicate_resolution": "none",
        "acknowledged_uncertainty_ids": [],
        "acknowledged_duplicate_ids": [],
        "merchant": "不允许在确认时修改",
    }
    confirm_id = str(uuid.uuid4())
    confirm_headers = agent_headers("confirm_invoice_item", confirm_id)
    first_confirm = client.post(
        f"/api/items/{draft['id']}/confirm",
        json=invalid_confirm,
        headers=confirm_headers,
    )
    assert first_confirm.status_code == 400
    assert first_confirm.get_json()["error"] == "confirm_fields_not_allowed"
    replay_confirm = client.post(
        f"/api/items/{draft['id']}/confirm",
        json=invalid_confirm,
        headers=confirm_headers,
    )
    assert replay_confirm.status_code == 400
    assert replay_confirm.get_json()["meta"] == {"replayed": True}

    merge_id = str(uuid.uuid4())
    merge_headers = agent_headers("merge_invoice_draft", merge_id)
    invalid_merge = {
        "source_version": 0,
        "target_version": 0,
        "unexpected": True,
    }
    first_merge = client.post(
        "/api/items/999999/merge/999998",
        json=invalid_merge,
        headers=merge_headers,
    )
    assert first_merge.status_code == 400
    assert first_merge.get_json()["error"] == "invalid_request"
    replay_merge = client.post(
        "/api/items/999999/merge/999998",
        json=invalid_merge,
        headers=merge_headers,
    )
    assert replay_merge.status_code == 400
    assert replay_merge.get_json()["meta"] == {"replayed": True}


def test_replayed_unknown_failure_preserves_outcome_metadata(app, client):
    operation_id = str(uuid.uuid4())
    parameters = manual_agent_payload()
    normalized_parameters = {**parameters, "converted_amount_cents": None}
    fingerprint = request_fingerprint(
        "create_manual_invoice_draft", normalized_parameters
    )
    now = utc_now()
    db = connect_db(app.config["DATABASE"])
    try:
        with transaction(db):
            db.execute(
                """INSERT INTO agent_operations(
                       operation_id,operation_name,request_fingerprint,status,http_status,
                       error_code,error_outcome,created_at,updated_at,completed_at
                   ) VALUES(?,?,?,'failed',409,'outcome_uncertain','unknown',?,?,?)""",
                (operation_id, "create_manual_invoice_draft", fingerprint, now, now, now),
            )
    finally:
        db.close()
    replay = client.post(
        "/api/items/manual",
        json=parameters,
        headers=agent_headers("create_manual_invoice_draft", operation_id),
    )
    assert replay.status_code == 409
    assert replay.get_json() == {
        "error": "outcome_uncertain",
        "message": "该操作已以确定失败结束；请查询操作状态。",
        "outcome": "unknown",
        "meta": {"replayed": True},
    }


def test_duplicate_json_member_is_rejected_without_mutation(client):
    operation_id = str(uuid.uuid4())
    raw = (
        b'{"merchant":"duplicate","expense_date":"2026-08-20",'
        b'"amount_cents":100,"amount_cents":200,"currency":"CNY",'
        b'"purpose":"duplicate member","project_id":1}'
    )
    response = client.post(
        "/api/items/manual",
        data=raw,
        content_type="application/json",
        headers=agent_headers("create_manual_invoice_draft", operation_id),
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "duplicate_json_field"
    assert client.get("/api/drafts").get_json()["count"] == 0


def test_keyset_pagination_snapshot_and_filter_binding(client):
    first_ids = []
    for index in range(3):
        response = client.post(
            "/api/items/manual",
            json={
                "merchant": f"分页商户 {index}",
                "expense_date": "2026-08-21",
                "amount": index + 1,
                "currency": "CNY",
                "purpose": "分页",
                "project_id": 1,
            },
        )
        first_ids.append(response.get_json()["item"]["id"])

    page_one = client.get("/api/drafts?limit=2").get_json()
    assert "pagination" not in page_one
    pagination = page_one["meta"]["pagination"]
    assert pagination["has_more"] is True
    assert len(page_one["items"]) == 2

    inserted = client.post(
        "/api/items/manual",
        json={
            "merchant": "游标后新增",
            "expense_date": "2026-08-21",
            "amount": 9,
            "currency": "CNY",
            "purpose": "不进入当前快照",
            "project_id": 1,
        },
    ).get_json()["item"]["id"]
    page_two = client.get(f"/api/drafts?limit=2&cursor={pagination['next_cursor']}").get_json()
    traversed = [entry["id"] for entry in page_one["items"] + page_two["items"]]
    assert inserted not in traversed
    assert set(traversed) == set(first_ids)
    assert page_two["meta"]["pagination"] == {"next_cursor": None, "has_more": False}

    invalid = client.get(
        f"/api/items?status=pending_confirmation&limit=2&cursor={pagination['next_cursor']}"
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid_cursor"
    cents_filter = client.get(
        "/api/items?status=pending_confirmation&amount_min_cents=500&limit=100"
    )
    assert cents_filter.status_code == 200
    assert all(entry["amount_cents"] >= 500 for entry in cents_filter.get_json()["items"])


def test_overflow_review_session_unlocks_browser_confirmation(client):
    for index in range(101):
        response = client.post(
            "/api/items/manual",
            json={
                "merchant": "同一重复商户",
                "expense_date": "2026-08-22",
                "amount": 88,
                "currency": "CNY",
                "purpose": "重复核对",
                "project_id": 1,
            },
        )
        assert response.status_code == 201, index
    source = client.post(
        "/api/items/manual",
        json={
            "merchant": "同一重复商户",
            "expense_date": "2026-08-22",
            "amount": 88,
            "currency": "CNY",
            "purpose": "重复核对",
            "project_id": 1,
        },
    ).get_json()["item"]
    detail = client.get(f"/api/items/{source['id']}").get_json()["item"]
    review = detail["review"]
    assert review["duplicate_review_overflow"] is True
    assert review["blocking_total"] == 101
    assert len(review["duplicate_candidates"]) == 100
    assert set(review["blocking_duplicate_ids"]) == {
        entry["id"] for entry in review["duplicate_candidates"]
    }

    started = client.post(
        f"/api/items/{source['id']}/duplicate-review-sessions",
        json={"expected_version": detail["version"], "review_token": review["token"]},
    )
    assert started.status_code == 201, started.get_json()
    first_page = started.get_json()
    assert len(first_page["candidates"]) == 100
    assert first_page["has_more"] is True
    finished = client.post(
        f"/api/items/{source['id']}/duplicate-review-sessions/{first_page['session_id']}/next",
        json={"cursor": first_page["next_cursor"]},
    )
    assert finished.status_code == 200, finished.get_json()
    final_page = finished.get_json()
    assert len(final_page["candidates"]) == 1
    assert final_page["has_more"] is False
    assert final_page["overflow_review_token"].startswith("overflow-v1.")

    confirmed = client.post(
        f"/api/items/{source['id']}/confirm",
        json={
            "expected_version": detail["version"],
            "review_token": review["token"],
            "duplicate_resolution": "keep_separate",
            "acknowledged_uncertainty_ids": [],
            "acknowledged_duplicate_ids": [],
            "overflow_review_token": final_page["overflow_review_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.get_json()


def test_agent_import_ack_idempotency_and_filename_fingerprint(app, client):
    calls = {"count": 0}

    def recognizer(*args):
        calls["count"] += 1
        return app.config["TEST_RECOGNITION"]

    app.config["TEST_RECOGNITION"] = {
        "merchant": "文件 Agent",
        "expense_date": "2026-08-23",
        "amount": 66.6,
        "currency": "CNY",
        "converted_amount": None,
        "purpose": "文件导入",
        "document_type": "invoice",
        "uncertainties": [],
    }
    app.config["RECOGNIZER"] = recognizer
    denied = client.post(
        "/api/imports",
        data={"file": (image_bytes("agent-import"), "agent.png")},
        headers=agent_headers("import_invoice_file"),
        content_type="multipart/form-data",
    )
    assert denied.status_code == 400
    assert denied.get_json()["error"] == "external_processing_ack_required"
    assert calls["count"] == 0

    operation_id = str(uuid.uuid4())
    headers = agent_headers("import_invoice_file", operation_id)
    form = {
        "external_processing_notice_version": "deepseek-v1",
        "external_processing_ack": "true",
        "file": (image_bytes("agent-import"), "agent.png"),
    }
    created = client.post(
        "/api/imports", data=form, headers=headers, content_type="multipart/form-data"
    )
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["operation_result"]["warning_codes"] == []
    assert calls["count"] == 1

    replay = client.post(
        "/api/imports",
        data={
            "external_processing_notice_version": "deepseek-v1",
            "external_processing_ack": "true",
            "file": (image_bytes("agent-import"), "agent.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert replay.status_code == 201
    assert replay.get_json()["meta"]["replayed"] is True
    assert calls["count"] == 1

    mismatch = client.post(
        "/api/imports",
        data={
            "external_processing_notice_version": "deepseek-v1",
            "external_processing_ack": "true",
            "file": (image_bytes("agent-import"), "renamed.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "idempotency_mismatch"
    db = connect_db(app.config["DATABASE"])
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM file_operation_staging").fetchone()["n"] == 0
        managed = Path(
            db.execute("SELECT managed_path FROM attachments ORDER BY id DESC LIMIT 1").fetchone()["managed_path"]
        )
    finally:
        db.close()
    assert managed.is_file()
    assert "operations" in managed.parts


def test_recognition_started_recovery_never_redispatches(app, client):
    class SimulatedCrash(BaseException):
        pass

    calls = {"count": 0}

    def crash_recognizer(*_args):
        calls["count"] += 1
        raise SimulatedCrash()

    app.config["RECOGNIZER"] = crash_recognizer
    operation_id = str(uuid.uuid4())
    headers = agent_headers("import_invoice_file", operation_id)
    with pytest.raises(SimulatedCrash):
        client.post(
            "/api/imports",
            data={
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": "true",
                "file": (image_bytes("crash"), "crash.png"),
            },
            headers=headers,
            content_type="multipart/form-data",
        )
    db = connect_db(app.config["DATABASE"])
    try:
        stage = db.execute(
            "SELECT phase FROM file_operation_staging WHERE operation_id=?", (operation_id,)
        ).fetchone()
        assert stage["phase"] == "recognition_started"
    finally:
        db.close()

    with app.app_context():
        recovery = recover_incomplete_file_operations(app)
    assert recovery["recovered"] == 1
    assert calls["count"] == 1
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert operation["status"] == "succeeded"
    assert "recognition_result_unknown_manual_fallback" in operation["operation_result"]["warning_codes"]

    replay = client.post(
        "/api/imports",
        data={
            "external_processing_notice_version": "deepseek-v1",
            "external_processing_ack": "true",
            "file": (image_bytes("crash"), "crash.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert replay.status_code == 201
    assert replay.get_json()["meta"]["replayed"] is True
    assert calls["count"] == 1


def test_crash_after_local_stage_before_ledger_is_reconciled(app, client, monkeypatch):
    class SimulatedCrash(BaseException):
        pass

    operation_id = str(uuid.uuid4())
    real_fingerprint = file_operations.request_fingerprint

    def crash_before_ledger(*_args, **_kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(file_operations, "request_fingerprint", crash_before_ledger)
    with pytest.raises(SimulatedCrash):
        client.post(
            "/api/imports",
            data={
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": "true",
                "file": (image_bytes("pre-ledger-crash"), "pre-ledger-crash.png"),
            },
            headers=agent_headers("import_invoice_file", operation_id),
            content_type="multipart/form-data",
        )
    staging_dir = Path(app.config["DATA_DIR"]) / "operation-staging" / operation_id
    assert len(list(staging_dir.glob("incoming-*.png"))) == 1
    db = connect_db(app.config["DATABASE"])
    try:
        assert not db.execute(
            "SELECT 1 FROM agent_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
    finally:
        db.close()

    monkeypatch.setattr(file_operations, "request_fingerprint", real_fingerprint)
    with app.app_context():
        report = recover_incomplete_file_operations(app)
    assert report["orphaned_cleaned"] == 1
    assert not staging_dir.exists()


def test_agent_attachment_replay_and_stale_version_cleanup(app, client):
    item_id = create_confirmed_item(client, merchant="附件 Agent")
    versions = item_version_fields(client, item_id)
    operation_id = str(uuid.uuid4())
    headers = agent_headers("add_invoice_attachment", operation_id)
    data = {
        "expected_version": str(versions["expected_version"]),
        "category": "invoice",
        "file": (image_bytes("attachment-agent"), "attachment.png"),
    }
    created = client.post(
        f"/api/items/{item_id}/attachments",
        data=data,
        headers=headers,
        content_type="multipart/form-data",
    )
    assert created.status_code == 201, created.get_json()
    assert len(created.get_json()["item"]["attachments"]) == 1

    replay = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            "expected_version": str(versions["expected_version"]),
            "category": "invoice",
            "file": (image_bytes("attachment-agent"), "attachment.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert replay.status_code == 201
    assert replay.get_json()["meta"]["replayed"] is True

    stale_id = str(uuid.uuid4())
    stale = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            "expected_version": str(versions["expected_version"]),
            "category": "invoice",
            "file": (image_bytes("stale-attachment"), "stale.png"),
        },
        headers=agent_headers("add_invoice_attachment", stale_id),
        content_type="multipart/form-data",
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "stale_version"
    operation = client.get(f"/api/agent-operations/{stale_id}").get_json()["operation"]
    assert operation["status"] == "failed"
    db = connect_db(app.config["DATABASE"])
    try:
        assert db.execute("SELECT COUNT(*) AS n FROM file_operation_staging").fetchone()["n"] == 0
    finally:
        db.close()


def test_attachment_version_change_during_hash_prevents_external_dispatch(
    app, client, monkeypatch
):
    item_id = create_confirmed_item(client, merchant="hash race")
    expected_version = item_version_fields(client, item_id)["expected_version"]
    calls = {"recognizer": 0, "hash_bump": 0}

    def recognizer(*_args):
        calls["recognizer"] += 1
        return {"amount": 1, "currency": "CNY"}

    real_hash = file_operations._sha256_file

    def hash_then_bump(path):
        digest = real_hash(path)
        if calls["hash_bump"] == 0 and "operation-staging" in Path(path).parts:
            calls["hash_bump"] += 1
            other = connect_db(app.config["DATABASE"])
            try:
                with transaction(other):
                    other.execute(
                        "UPDATE expense_items SET row_version=row_version+1,updated_at=? WHERE id=?",
                        (utc_now(), item_id),
                    )
            finally:
                other.close()
        return digest

    app.config["RECOGNIZER"] = recognizer
    monkeypatch.setattr(file_operations, "_sha256_file", hash_then_bump)
    response = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            "expected_version": str(expected_version),
            "category": "payment_record",
            "external_processing_notice_version": "deepseek-v1",
            "external_processing_ack": "true",
            "file": (image_bytes("hash-race"), "hash-race.png"),
        },
        headers=agent_headers("add_invoice_attachment"),
        content_type="multipart/form-data",
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "stale_version"
    assert calls == {"recognizer": 0, "hash_bump": 1}
    detail = client.get(f"/api/items/{item_id}").get_json()["item"]
    assert detail["attachments"] == []


def test_file_failure_status_and_code_remain_stable_through_cleanup_recovery(
    app, client, monkeypatch
):
    item_id = create_confirmed_item(client, merchant="cleanup pending")
    stale_version = item_version_fields(client, item_id)["expected_version"]
    updated = client.patch(
        f"/api/items/{item_id}",
        json={"expected_version": stale_version, "purpose": "version bump"},
    )
    assert updated.status_code == 200
    operation_id = str(uuid.uuid4())
    headers = agent_headers("add_invoice_attachment", operation_id)
    original_cleanup = file_operations._cleanup_paths
    monkeypatch.setattr(file_operations, "_cleanup_paths", lambda *_args: False)
    first = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            "expected_version": str(stale_version),
            "category": "invoice",
            "file": (image_bytes("cleanup-pending"), "cleanup-pending.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert first.status_code == 409
    assert first.get_json()["error"] == "stale_version"
    assert first.get_json()["outcome"] == "unknown"
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert (
        operation["status"],
        operation["http_status"],
        operation["error_code"],
        operation["outcome"],
    ) == ("failed", 409, "stale_version", "unknown")

    monkeypatch.setattr(file_operations, "_cleanup_paths", original_cleanup)
    with app.app_context():
        recovered = recover_incomplete_file_operations(app)
    assert recovered["failed"] == 1
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert (
        operation["status"],
        operation["http_status"],
        operation["error_code"],
        operation["outcome"],
    ) == ("failed", 409, "stale_version", "not_applied")
    replay = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            "expected_version": str(stale_version),
            "category": "invoice",
            "file": (image_bytes("cleanup-pending"), "cleanup-pending.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert replay.status_code == 409
    assert replay.get_json()["error"] == "stale_version"
    assert replay.get_json()["meta"] == {"replayed": True}


def test_resource_applied_recovery_completes_ledger_without_duplicate(app, client):
    item_id = create_confirmed_item(client, merchant="已关联恢复")
    item = add_attachment(client, item_id, "invoice", "applied.png")
    attachment = item["attachments"][0]
    operation_id = str(uuid.uuid4())
    final_path = (
        Path(app.config["IMPORT_DIR"]) / "operations" / operation_id / "source.png"
    ).resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    (final_path.parent / ".invoice-agent-owner").write_text(
        f"invoice-agent-file-v1\n{operation_id}\n{attachment['sha256']}\n",
        encoding="utf-8",
        newline="\n",
    )
    lookup = connect_db(app.config["DATABASE"])
    try:
        original_path = Path(
            lookup.execute(
                "SELECT managed_path FROM attachments WHERE id=?", (attachment["id"],)
            ).fetchone()["managed_path"]
        )
    finally:
        lookup.close()
    original_path.replace(final_path)
    now = utc_now()
    safe_snapshot = {
        "resource_refs": [
            {"type": "invoice_item", "id": item_id, "version": item["version"]}
        ],
        "artifact_available": False,
        "warning_codes": [],
    }
    db = connect_db(app.config["DATABASE"])
    try:
        db.execute(
            "UPDATE attachments SET managed_path=? WHERE id=?",
            (str(final_path), attachment["id"]),
        )
        db.execute(
            """INSERT INTO agent_operations(
                   operation_id,operation_name,request_fingerprint,status,
                   operation_result_json,created_at,updated_at
               ) VALUES(?,?,?,'in_progress',?,?,?)""",
            (
                operation_id,
                "add_invoice_attachment",
                "f" * 64,
                json_dump(safe_snapshot),
                now,
                now,
            ),
        )
        db.execute(
            """INSERT INTO file_operation_staging(
                   operation_id,tool_name,phase,file_sha256,mime_type,display_basename,
                   attachment_kind,target_item_id,expected_item_version,expected_batch_version,
                   managed_file_id,recognition_result_json,recognition_error,resource_type,
                   resource_id,resource_version,created_at,updated_at
               ) VALUES(?,?,'resource_applied',?,?,?,?,?,?,?,?,?,NULL,'invoice_item',?,?,?,?)""",
            (
                operation_id,
                "add_invoice_attachment",
                attachment["sha256"],
                attachment["mime_type"],
                "applied.png",
                "invoice",
                item_id,
                item["version"] - 1,
                None,
                f"{operation_id}/source.png",
                "{}",
                item_id,
                item["version"],
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()

    with app.app_context():
        recovered = recover_incomplete_file_operations(app)
    assert recovered["recovered"] == 1
    detail = client.get(f"/api/items/{item_id}").get_json()["item"]
    assert len(detail["attachments"]) == 1
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert operation["status"] == "succeeded"
    assert operation["operation_result"]["resource_refs"] == [
        {"type": "invoice_item", "id": item_id, "version": item["version"]}
    ]


def test_reachable_resource_applied_crash_recovers_without_duplicate(
    app, client, monkeypatch
):
    class SimulatedCrash(BaseException):
        pass

    item_id = create_confirmed_item(client, merchant="真实 resource phase")
    version = item_version_fields(client, item_id)["expected_version"]
    operation_id = str(uuid.uuid4())
    real_apply = file_operations._apply_resource

    def crash_after_resource_commit(*args, **kwargs):
        real_apply(*args, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(file_operations, "_apply_resource", crash_after_resource_commit)
    with pytest.raises(SimulatedCrash):
        client.post(
            f"/api/items/{item_id}/attachments",
            data={
                "expected_version": str(version),
                "category": "invoice",
                "file": (image_bytes("resource-applied"), "resource-applied.png"),
            },
            headers=agent_headers("add_invoice_attachment", operation_id),
            content_type="multipart/form-data",
        )

    db = connect_db(app.config["DATABASE"])
    try:
        stage = db.execute(
            "SELECT phase,resource_id,resource_version FROM file_operation_staging WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert stage["phase"] == "resource_applied"
        assert stage["resource_id"] == item_id
        applied_version = int(stage["resource_version"])
        assert db.execute(
            "SELECT COUNT(*) AS n FROM attachments WHERE expense_item_id=?", (item_id,)
        ).fetchone()["n"] == 1
        assert db.execute(
            "SELECT status FROM agent_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()["status"] == "in_progress"
        with transaction(db):
            newer = db.execute(
                """UPDATE expense_items SET purpose='concurrent follow-up',row_version=row_version+1,
                          updated_at=? WHERE id=? RETURNING row_version""",
                (utc_now(), item_id),
            ).fetchone()["row_version"]
    finally:
        db.close()

    monkeypatch.setattr(file_operations, "_apply_resource", real_apply)
    with app.app_context():
        report = recover_incomplete_file_operations(app)
    assert report["recovered"] == 1
    detail = client.get(f"/api/items/{item_id}").get_json()["item"]
    assert len(detail["attachments"]) == 1
    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert operation["status"] == "succeeded"
    assert newer > applied_version
    assert operation["operation_result"]["resource_refs"][0]["version"] == applied_version


def test_preexisting_file_destination_is_never_adopted_or_deleted(app, client):
    operation_id = str(uuid.uuid4())
    external_dir = Path(app.config["IMPORT_DIR"]) / "operations" / operation_id
    external_dir.mkdir(parents=True)
    external_bytes = image_bytes("external-owner").getvalue()
    external_source = external_dir / "source.png"
    external_source.write_bytes(external_bytes)
    sentinel = external_dir / "sentinel.bin"
    sentinel.write_bytes(b"external-sentinel")
    headers = agent_headers("import_invoice_file", operation_id)
    form = {
        "external_processing_notice_version": "deepseek-v1",
        "external_processing_ack": "true",
        "file": (BytesIO(external_bytes), "external.png"),
    }
    first = client.post(
        "/api/imports", data=form, headers=headers, content_type="multipart/form-data"
    )
    assert first.status_code == 409
    assert first.get_json()["error"] == "operation_destination_exists"
    assert external_source.read_bytes() == external_bytes
    assert sentinel.read_bytes() == b"external-sentinel"

    replay = client.post(
        "/api/imports",
        data={
            "external_processing_notice_version": "deepseek-v1",
            "external_processing_ack": "true",
            "file": (BytesIO(external_bytes), "external.png"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert replay.status_code == 409
    assert replay.get_json()["error"] == "operation_destination_exists"
    assert replay.get_json()["meta"] == {"replayed": True}
    assert external_source.read_bytes() == external_bytes
    assert sentinel.read_bytes() == b"external-sentinel"


def test_invalid_upload_failure_is_durable_and_does_not_leak_paths(app, client):
    operation_id = str(uuid.uuid4())
    headers = agent_headers("import_invoice_file", operation_id)
    malformed = b"not a png despite the declared extension"

    def post_file(contents):
        return client.post(
            "/api/imports",
            data={
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": "true",
                "file": (BytesIO(contents), "malformed.png"),
            },
            headers=headers,
            content_type="multipart/form-data",
        )

    first = post_file(malformed)
    assert first.status_code == 415
    assert first.get_json()["error"] == "invalid_file_content"
    encoded = str(first.get_json())
    assert str(app.config["DATA_DIR"]) not in encoded
    assert "operation-staging" not in encoded

    operation = client.get(f"/api/agent-operations/{operation_id}").get_json()["operation"]
    assert (
        operation["status"],
        operation["http_status"],
        operation["error_code"],
        operation["outcome"],
    ) == ("failed", 415, "invalid_file_content", "not_applied")

    replay = post_file(malformed)
    assert replay.status_code == 415
    assert replay.get_json()["error"] == "invalid_file_content"
    assert replay.get_json()["meta"] == {"replayed": True}

    mismatch = post_file(image_bytes("now-valid").getvalue())
    assert mismatch.status_code == 409
    assert mismatch.get_json()["error"] == "idempotency_mismatch"


def test_file_cleanup_quarantine_preserves_replacement_directory(app, monkeypatch):
    operation_id = str(uuid.uuid4())
    digest = "a" * 64
    row = {
        "operation_id": operation_id,
        "managed_file_id": f"{operation_id}/incoming-{uuid.uuid4().hex}.png",
        "display_basename": "source.png",
        "file_sha256": digest,
    }
    stage = file_operations._stage_path(
        app, operation_id, row["managed_file_id"]
    )
    stage.parent.mkdir(parents=True)
    stage.write_bytes(b"owned-stage")
    final_dir = file_operations._final_path(app, row).parent
    final_dir.mkdir(parents=True)
    (final_dir / ".invoice-agent-owner").write_text(
        f"invoice-agent-file-v1\n{operation_id}\n{digest}\n",
        encoding="utf-8",
        newline="\n",
    )
    (final_dir / "source.png").write_bytes(b"owned")

    real_rename = file_operations.os.rename
    replacement = b"external-replacement"

    def rename_then_replace(source, target):
        real_rename(source, target)
        if Path(source).resolve() == final_dir.resolve():
            final_dir.mkdir()
            (final_dir / "sentinel.bin").write_bytes(replacement)

    monkeypatch.setattr(file_operations.os, "rename", rename_then_replace)
    assert file_operations._cleanup_paths(app, row) is True
    assert (final_dir / "sentinel.bin").read_bytes() == replacement
