from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

import invoice_assistant.storage as storage
from invoice_assistant.db import connect_db, get_db, utc_now

from .conftest import add_attachment, create_batch, create_confirmed_item, export_batch


def make_draft_batch(client, *, name: str, amount: float, project_id: int = 1):
    item_id = create_confirmed_item(
        client,
        amount=amount,
        merchant=f"{name}商户",
        purpose=f"{name}用途",
    )
    add_attachment(client, item_id, "invoice", f"{name}.png")
    response = create_batch(
        client,
        [item_id],
        name=name,
        project_id=project_id,
        purpose=f"{name}说明",
        notes=f"{name}备注",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["batch"], item_id


def merge_payload(target, source_batches, **overrides):
    payload = {
        "target_batch_id": target["id"],
        "target_version": target["version"],
        "sources": [
            {"batch_id": source["id"], "expected_version": source["version"]}
            for source in source_batches
        ],
        "confirmation": target["name"],
        "discard_source_archives": any(
            source.get("superseded_archive_path") for source in source_batches
        ),
    }
    payload.update(overrides)
    return payload


def batch_members(database, batch_id: int) -> list[int]:
    db = connect_db(database)
    try:
        return [
            int(row["expense_item_id"])
            for row in db.execute(
                "SELECT expense_item_id FROM batch_items WHERE batch_id=? ORDER BY sort_order,expense_item_id",
                (batch_id,),
            ).fetchall()
        ]
    finally:
        db.close()


def test_merge_draft_batches_preserves_target_and_supports_unified_export(app, client):
    target, target_item = make_draft_batch(client, name="统一目标", amount=100)
    source_a, item_a = make_draft_batch(client, name="来源甲", amount=120)
    source_b, item_b = make_draft_batch(client, name="来源乙", amount=140)

    db = connect_db(app.config["DATABASE"])
    before_files = {
        int(row["id"]): (row["managed_path"], row["sha256"])
        for row in db.execute(
            "SELECT id,managed_path,sha256 FROM attachments ORDER BY id"
        ).fetchall()
    }
    before_item_versions = {
        int(row["id"]): int(row["row_version"])
        for row in db.execute(
            "SELECT id,row_version FROM expense_items WHERE id IN (?,?,?)",
            (target_item, item_a, item_b),
        ).fetchall()
    }
    db.close()

    response = client.post(
        "/api/batch-management/merge",
        json=merge_payload(target, [source_b, source_a]),
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    merged = payload["batch"]
    assert payload["merged_source_ids"] == [source_b["id"], source_a["id"]]
    assert payload["moved_item_ids"] == [item_b, item_a]
    assert payload["cleanup_warnings"] == []
    assert [item["id"] for item in merged["items"]] == [target_item, item_b, item_a]
    assert merged["name"] == target["name"]
    assert merged["project_id"] == target["project_id"]
    assert merged["purpose"] == target["purpose"]
    assert merged["notes"] == target["notes"]
    assert merged["total_amount"] == 360
    assert merged["version"] == target["version"] + 1
    assert client.get(f"/api/batches/{source_a['id']}").status_code == 404
    assert client.get(f"/api/batches/{source_b['id']}").status_code == 404

    db = connect_db(app.config["DATABASE"])
    after_files = {
        int(row["id"]): (row["managed_path"], row["sha256"])
        for row in db.execute(
            "SELECT id,managed_path,sha256 FROM attachments ORDER BY id"
        ).fetchall()
    }
    item_rows = db.execute(
        "SELECT id,status,row_version FROM expense_items WHERE id IN (?,?,?) ORDER BY id",
        (target_item, item_a, item_b),
    ).fetchall()
    assert before_files == after_files
    assert all(row["status"] == "in_batch" for row in item_rows)
    versions = {int(row["id"]): int(row["row_version"]) for row in item_rows}
    assert versions[target_item] == before_item_versions[target_item]
    assert versions[item_a] == before_item_versions[item_a] + 1
    assert versions[item_b] == before_item_versions[item_b] + 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    target_actions = {
        row["action"]
        for row in db.execute(
            "SELECT action FROM audit_logs WHERE object_type='batch' AND object_id=?",
            (target["id"],),
        ).fetchall()
    }
    assert "source_batches_merged" in target_actions
    for source in (source_a, source_b):
        source_audit = db.execute(
            "SELECT details_json FROM audit_logs WHERE object_type='batch' AND object_id=? AND action='merged_into_batch'",
            (source["id"],),
        ).fetchone()
        assert source_audit
        snapshot_name = json.loads(source_audit["details_json"])["snapshot"]
        assert (Path(app.config["BACKUP_DIR"]) / snapshot_name).is_file()
    db.close()

    exported = export_batch(client, target["id"])
    assert exported.status_code == 200, exported.get_json()
    exported_batch = exported.get_json()["batch"]
    assert exported_batch["status"] == "submitted"
    assert len(exported_batch["items"]) == 3
    assert Path(exported_batch["pdf_path"]).is_file()


def test_merge_validation_failures_leave_membership_unchanged(app, client):
    target, target_item = make_draft_batch(client, name="校验目标", amount=80)
    source, source_item = make_draft_batch(client, name="校验来源", amount=90)
    original_members = {
        target["id"]: [target_item],
        source["id"]: [source_item],
    }
    cases = [
        (merge_payload(target, [source], target_version=target["version"] + 1), 409, "stale_version"),
        (merge_payload(target, [source], confirmation="错误名称"), 409, "merge_confirmation_mismatch"),
        (
            merge_payload(
                target,
                [source],
                sources=[
                    {"batch_id": source["id"], "expected_version": source["version"]},
                    {"batch_id": source["id"], "expected_version": source["version"]},
                ],
            ),
            400,
            "invalid_batch_sources",
        ),
        (
            merge_payload(
                target,
                [source],
                sources=[{"batch_id": target["id"], "expected_version": target["version"]}],
            ),
            400,
            "invalid_batch_sources",
        ),
    ]
    for request_payload, status, error in cases:
        response = client.post("/api/batch-management/merge", json=request_payload)
        assert response.status_code == status, response.get_json()
        assert response.get_json()["error"] == error
        assert batch_members(app.config["DATABASE"], target["id"]) == original_members[target["id"]]
        assert batch_members(app.config["DATABASE"], source["id"]) == original_members[source["id"]]


def test_merge_rejects_cross_project_non_draft_and_exporting_without_partial_move(app, client):
    target, target_item = make_draft_batch(client, name="状态目标", amount=70)
    source, source_item = make_draft_batch(client, name="状态来源", amount=75)
    db = connect_db(app.config["DATABASE"])
    now = utc_now()
    project_id = db.execute(
        "INSERT INTO projects(name,code,notes,enabled,created_at,updated_at) VALUES('其他项目','OTHER','',1,?,?) RETURNING id",
        (now, now),
    ).fetchone()["id"]
    db.execute("UPDATE reimbursement_batches SET project_id=? WHERE id=?", (project_id, source["id"]))
    db.commit()
    db.close()
    cross_project = client.post(
        "/api/batch-management/merge", json=merge_payload(target, [source])
    )
    assert cross_project.status_code == 409
    assert cross_project.get_json()["error"] == "batch_project_mismatch"

    db = connect_db(app.config["DATABASE"])
    db.execute("UPDATE reimbursement_batches SET project_id=1,status='submitted' WHERE id=?", (source["id"],))
    db.commit()
    db.close()
    non_draft = client.post(
        "/api/batch-management/merge", json=merge_payload(target, [source])
    )
    assert non_draft.status_code == 409
    assert non_draft.get_json()["error"] == "invalid_batch_status"

    db = connect_db(app.config["DATABASE"])
    db.execute("UPDATE reimbursement_batches SET status='draft',export_token='busy-token' WHERE id=?", (source["id"],))
    db.commit()
    db.close()
    exporting = client.post(
        "/api/batch-management/merge", json=merge_payload(target, [source])
    )
    assert exporting.status_code == 409
    assert exporting.get_json()["error"] == "batch_exporting"
    assert batch_members(app.config["DATABASE"], target["id"]) == [target_item]
    assert batch_members(app.config["DATABASE"], source["id"]) == [source_item]


def test_merge_rejects_more_than_200_items(app, client):
    target, _ = make_draft_batch(client, name="上限目标", amount=20)
    source, _ = make_draft_batch(client, name="上限来源", amount=30)
    db = connect_db(app.config["DATABASE"])
    now = utc_now()
    next_order = 1
    for index in range(199):
        item_id = db.execute(
            """INSERT INTO expense_items(merchant,expense_date,amount,amount_cents,currency,purpose,project_id,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (f"上限条目{index}", "2026-08-24", 1, 100, "CNY", "上限测试", 1, "in_batch", now, now),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO batch_items(batch_id,expense_item_id,sort_order) VALUES(?,?,?)",
            (target["id"], item_id, next_order),
        )
        next_order += 1
    db.commit()
    db.close()

    response = client.post(
        "/api/batch-management/merge", json=merge_payload(target, [source])
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "too_many_items"
    assert len(batch_members(app.config["DATABASE"], target["id"])) == 200
    assert len(batch_members(app.config["DATABASE"], source["id"])) == 1


def configure_superseded_archive(app, batch_id: int, label: str) -> tuple[Path, Path]:
    archive_root = Path(app.config["DEFAULT_ARCHIVE_DIR"]).resolve()
    archive = archive_root / label
    archive.mkdir(parents=True)
    pdf = archive / f"{label}.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% old archive\n")
    db = connect_db(app.config["DATABASE"])
    db.execute(
        "UPDATE reimbursement_batches SET superseded_archive_path=?,superseded_pdf_path=? WHERE id=?",
        (str(archive), str(pdf), batch_id),
    )
    db.commit()
    db.close()
    return archive, pdf


def test_merge_requires_explicit_recoverable_source_archive_disposition(app, client):
    target, _ = make_draft_batch(client, name="归档目标", amount=55)
    source, _ = make_draft_batch(client, name="归档来源", amount=65)
    source_archive, _ = configure_superseded_archive(app, source["id"], "source-old")
    target_archive, _ = configure_superseded_archive(app, target["id"], "target-old")

    blocked = client.post(
        "/api/batch-management/merge",
        json=merge_payload(target, [source], discard_source_archives=False),
    )
    assert blocked.status_code == 409, blocked.get_json()
    assert blocked.get_json()["error"] == "source_archive_discard_required"
    assert source_archive.is_dir()
    assert target_archive.is_dir()

    merged = client.post(
        "/api/batch-management/merge",
        json=merge_payload(target, [source], discard_source_archives=True),
    )
    assert merged.status_code == 200, merged.get_json()
    assert merged.get_json()["cleanup_warnings"] == []
    assert not source_archive.exists()
    assert target_archive.is_dir()
    assert any(
        path.name == "source-old"
        for path in (Path(app.config["TRASH_DIR"]) / "archives").rglob("source-old")
    )
    db = connect_db(app.config["DATABASE"])
    assert db.execute(
        "SELECT status FROM file_cleanup_queue WHERE path=? AND kind='archive_tree'",
        (str(source_archive),),
    ).fetchone()["status"] == "completed"
    db.close()


def test_unavailable_source_archive_blocks_merge_and_preserves_recovery_reference(app, client):
    target, target_item = make_draft_batch(client, name="离线归档目标", amount=58)
    source, source_item = make_draft_batch(client, name="离线归档来源", amount=68)
    missing_archive = Path(app.config["DEFAULT_ARCHIVE_DIR"]) / "temporarily-offline"
    missing_pdf = missing_archive / "old.pdf"
    db = connect_db(app.config["DATABASE"])
    db.execute(
        "UPDATE reimbursement_batches SET superseded_archive_path=?,superseded_pdf_path=? WHERE id=?",
        (str(missing_archive), str(missing_pdf), source["id"]),
    )
    db.commit()
    db.close()

    response = client.post(
        "/api/batch-management/merge",
        json=merge_payload(target, [source], discard_source_archives=True),
    )
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"] == "source_archive_unavailable"
    assert batch_members(app.config["DATABASE"], target["id"]) == [target_item]
    assert batch_members(app.config["DATABASE"], source["id"]) == [source_item]
    db = connect_db(app.config["DATABASE"])
    source_row = db.execute(
        "SELECT superseded_archive_path,superseded_pdf_path FROM reimbursement_batches WHERE id=?",
        (source["id"],),
    ).fetchone()
    assert source_row["superseded_archive_path"] == str(missing_archive)
    assert source_row["superseded_pdf_path"] == str(missing_pdf)
    assert db.execute("SELECT COUNT(*) AS n FROM file_cleanup_queue").fetchone()["n"] == 0
    db.close()


def test_cleanup_failure_is_reported_and_remains_retryable(app, client, monkeypatch):
    target, _ = make_draft_batch(client, name="重试目标", amount=45)
    source, _ = make_draft_batch(client, name="重试来源", amount=50)
    source_archive, _ = configure_superseded_archive(app, source["id"], "retry-old")
    original_cleanup = storage._cleanup_one

    def fail_cleanup(_row):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(storage, "_cleanup_one", fail_cleanup)
    merged = client.post(
        "/api/batch-management/merge",
        json=merge_payload(target, [source], discard_source_archives=True),
    )
    assert merged.status_code == 200, merged.get_json()
    assert merged.get_json()["cleanup_warnings"] == [
        {"batch_id": source["id"], "code": "cleanup_failed"}
    ]
    assert client.get(f"/api/batches/{source['id']}").status_code == 404
    assert source_archive.is_dir()

    monkeypatch.setattr(storage, "_cleanup_one", original_cleanup)
    with app.app_context():
        retried = storage.process_file_cleanup_queue(get_db())
    assert retried["completed"] >= 1
    assert not source_archive.exists()


@pytest.mark.parametrize("mark_reimbursed", [False, True])
def test_any_history_batch_can_return_to_editing(app, client, mark_reimbursed):
    batch, item_id = make_draft_batch(
        client,
        name="已报销退回" if mark_reimbursed else "普通历史退回",
        amount=110,
    )
    exported = export_batch(client, batch["id"])
    assert exported.status_code == 200, exported.get_json()
    history = exported.get_json()["batch"]
    old_archive = Path(history["archive_path"])
    if mark_reimbursed:
        marked = client.post(
            f"/api/batches/{batch['id']}/mark-reimbursed",
            json={
                "expected_version": history["version"],
                "reimbursed_date": "2026-08-24",
                "notes": "已到账",
            },
        )
        assert marked.status_code == 200, marked.get_json()
        history = marked.get_json()["batch"]

    reopened = client.post(
        f"/api/batches/{batch['id']}/reopen",
        json={"expected_version": history["version"], "confirmation": history["name"]},
    )
    assert reopened.status_code == 200, reopened.get_json()
    reopened_batch = reopened.get_json()["batch"]
    assert reopened_batch["status"] == "draft"
    assert reopened_batch["archive_path"] is None
    assert reopened_batch["superseded_archive_path"] == str(old_archive)
    assert reopened_batch["reimbursed_date"] is None
    assert reopened_batch["reimbursement_notes"] == ""
    assert reopened_batch["items"][0]["id"] == item_id
    assert reopened_batch["items"][0]["status"] == "in_batch"
    assert old_archive.is_dir()


def test_competing_merges_move_a_source_exactly_once(app, client):
    target_a, _ = make_draft_batch(client, name="并发目标甲", amount=40)
    target_b, _ = make_draft_batch(client, name="并发目标乙", amount=41)
    source, source_item = make_draft_batch(client, name="并发来源", amount=42)
    barrier = Barrier(2)

    def attempt(target):
        with app.test_client() as thread_client:
            barrier.wait()
            return thread_client.post(
                "/api/batch-management/merge",
                json=merge_payload(target, [source]),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(attempt, (target_a, target_b)))
    assert any(response.status_code == 200 for response in responses)
    assert sum(response.status_code == 200 for response in responses) == 1
    db = connect_db(app.config["DATABASE"])
    owner = db.execute(
        "SELECT batch_id FROM batch_items WHERE expense_item_id=?",
        (source_item,),
    ).fetchone()["batch_id"]
    assert owner in {target_a["id"], target_b["id"]}
    assert db.execute(
        "SELECT COUNT(*) AS n FROM batch_items WHERE expense_item_id=?",
        (source_item,),
    ).fetchone()["n"] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
