from __future__ import annotations

from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4

from invoice_assistant.db import connect_db

from .conftest import (
    add_attachment,
    confirm_item,
    confirmation_payload,
    create_batch,
    create_confirmed_item,
    export_batch,
    image_bytes,
    item_version_fields,
)


def test_dashboard_and_manual_confirmation_gate(client):
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.get_json()["recognition"]["key_exposed_to_browser"] is False
    assert bootstrap.get_json()["recognition"]["provider"] == "deepseek"
    assert bootstrap.get_json()["recognition"]["model"] == "deepseek-v4-flash-vision-exp"

    draft = client.post(
        "/api/items/manual",
        json={"merchant": "", "expense_date": "", "amount": 0, "currency": "CNY", "purpose": "", "project_id": 1},
    ).get_json()["item"]
    assert draft["status"] == "pending_confirmation"
    assert client.get("/api/items?status=pending_reimbursement").get_json()["count"] == 0

    blocked = client.post(
        f"/api/items/{draft['id']}/confirm",
        json=confirmation_payload(client, draft["id"]),
    )
    assert blocked.status_code == 400
    assert blocked.get_json()["error"] == "incomplete_item"

    updated = client.patch(
        f"/api/items/{draft['id']}",
        json={
            **item_version_fields(client, draft["id"]),
            "merchant": "书店", "expense_date": "2026-08-02", "amount": 88,
            "currency": "CNY", "purpose": "购书", "project_id": 1,
        },
    )
    assert updated.status_code == 200
    confirmed = confirm_item(client, draft["id"])
    assert confirmed.status_code == 200
    assert confirmed.get_json()["item"]["status"] == "pending_reimbursement"
    assert confirmed.get_json()["item"]["material"]["missing"][0]["code"] == "primary_receipt"


def test_material_threshold_boundaries(client):
    expected = {
        499.99: ["primary_receipt"],
        500.00: ["primary_receipt"],
        999.99: ["primary_receipt"],
        1000.00: ["primary_receipt", "purchase_list", "payment_record"],
    }
    for amount, codes in expected.items():
        item_id = create_confirmed_item(client, amount=amount, merchant=f"商户{amount}")
        item = client.get(f"/api/items/{item_id}").get_json()["item"]
        assert [entry["code"] for entry in item["material"]["missing"]] == codes
        item = add_attachment(client, item_id, "receipt", f"receipt-{amount}.png")
        remaining = [entry["code"] for entry in item["material"]["missing"]]
        assert remaining == ([] if amount < 1000 else ["purchase_list", "payment_record"])


def test_foreign_currency_keeps_original_and_uses_converted_amount_for_rules(client):
    draft = client.post(
        "/api/items/manual",
        json={
            "merchant": "海外供应商",
            "expense_date": "2026-08-04",
            "amount": 50,
            "currency": "USD",
            "converted_amount": 1200,
            "purpose": "海外软件服务",
            "project_id": 1,
        },
    ).get_json()["item"]
    confirmed = confirm_item(client, draft["id"])
    assert confirmed.status_code == 200
    item = confirmed.get_json()["item"]
    assert item["amount"] == 50
    assert item["currency"] == "USD"
    assert item["converted_amount"] == 1200
    assert [entry["code"] for entry in item["material"]["missing"]] == ["foreign_payment_rmb", "primary_receipt", "purchase_list"]

    batch = create_batch(client, [item["id"]], name="外币报销", project_id=1)
    assert batch.status_code == 201
    assert batch.get_json()["batch"]["currency_totals"] == [{"amount": 1200.0, "currency": "CNY"}]
    assert batch.get_json()["batch"]["total_amount"] == 1200


def test_import_copies_file_and_duplicate_merge(app, client, tmp_path):
    target_id = create_confirmed_item(client)
    original = tmp_path / "source.png"
    original.write_bytes(image_bytes().getvalue())
    with original.open("rb") as stream:
        response = client.post(
            "/api/imports",
            data={"files": (stream, "nonsense-name.png")},
            content_type="multipart/form-data",
        )
    assert response.status_code == 201, response.get_json()
    result = response.get_json()["results"][0]
    draft_id = result["item"]["id"]
    assert result["imported"] is True
    assert result["recognition_succeeded"] is True
    assert any(match["item"]["id"] == target_id and match["high_confidence"] for match in result["matches"])
    original.unlink()

    db = connect_db(app.config["DATABASE"])
    managed = Path(db.execute("SELECT managed_path FROM attachments WHERE expense_item_id=?", (draft_id,)).fetchone()["managed_path"])
    db.close()
    assert managed.exists(), "deleting the source file must not remove the managed copy"

    source = client.get(f"/api/items/{draft_id}").get_json()["item"]
    target_before = client.get(f"/api/items/{target_id}").get_json()["item"]
    merged = client.post(
        f"/api/items/{draft_id}/merge/{target_id}",
        json={
            "source_version": source["version"],
            "target_version": target_before["version"],
            "source_review_token": source["review"]["token"],
        },
    )
    assert merged.status_code == 200, merged.get_json()
    target = merged.get_json()["item"]
    assert len(target["attachments"]) == 1
    assert target["attachments"][0]["original_name"] == "nonsense-name.png"
    assert "示例科技有限公司" in target["attachments"][0]["normalized_name"]
    assert target["attachments"][0]["ai_raw"]["confidence"] == 0.98
    assert client.get(f"/api/attachments/{target['attachments'][0]['id']}/download").status_code == 200
    deleted = client.delete(
        f"/api/items/{target_id}",
        json={"expected_version": target["version"]},
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert client.get(f"/api/items/{target_id}").status_code == 404
    assert client.get(f"/api/items/{draft_id}").status_code == 404


def test_recognition_failure_preserves_manual_draft(app, client):
    def broken(*_args):
        raise RuntimeError("network down")

    app.config["RECOGNIZER"] = broken
    response = client.post(
        "/api/imports",
        data={"files": (image_bytes(), "fallback.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    result = response.get_json()["results"][0]
    assert result["recognition_succeeded"] is False
    assert result["item"]["status"] == "pending_confirmation"
    assert result["item"]["recognition_error"]
    assert result["item"]["attachments"][0]["original_name"] == "fallback.png"


def test_delete_draft_removes_record_and_managed_attachment(app, client):
    imported = client.post(
        "/api/imports",
        data={"files": (image_bytes(), "delete-me.png")},
        content_type="multipart/form-data",
    ).get_json()["results"][0]["item"]
    db = connect_db(app.config["DATABASE"])
    managed_path = Path(db.execute("SELECT managed_path FROM attachments WHERE expense_item_id=?", (imported["id"],)).fetchone()["managed_path"])
    db.close()
    assert managed_path.is_file()

    deleted = client.delete(
        f"/api/items/{imported['id']}",
        json={"expected_version": imported["version"]},
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["deleted_ids"] == [imported["id"]]
    assert not managed_path.exists()
    assert client.get(f"/api/items/{imported['id']}").status_code == 404
    assert client.get("/api/drafts").get_json()["items"] == []
    assert list(Path(app.config["BACKUP_DIR"]).glob("manual-*.sqlite3"))


def test_bulk_delete_pool_records_and_delete_draft_batch_returns_items(client):
    first = create_confirmed_item(client, merchant="待删除 A")
    second = create_confirmed_item(client, merchant="待删除 B")
    deleted = client.post(
        "/api/items/bulk-delete",
        json={"items": [
            {"item_id": first, "expected_version": item_version_fields(client, first)["expected_version"]},
            {"item_id": second, "expected_version": item_version_fields(client, second)["expected_version"]},
        ]},
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["deleted_count"] == 2
    assert client.get("/api/items?status=pending_reimbursement").get_json()["count"] == 0

    locked = create_confirmed_item(client, merchant="处理中条目")
    batch = create_batch(client, [locked], name="待删除处理中包", project_id=1).get_json()["batch"]
    blocked = client.post(
        "/api/items/bulk-delete",
        json={"items": [{
            "item_id": locked,
            "expected_version": item_version_fields(client, locked)["expected_version"],
        }]},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "item_delete_locked"

    removed_batch = client.delete(
        f"/api/batches/{batch['id']}", json={"expected_version": batch["version"]}
    )
    assert removed_batch.status_code == 200, removed_batch.get_json()
    assert removed_batch.get_json()["returned_item_ids"] == [locked]
    pool_ids = [item["id"] for item in client.get("/api/items?status=pending_reimbursement").get_json()["items"]]
    assert locked in pool_ids


def test_batch_export_close_loop_and_history(app, client):
    low_id = create_confirmed_item(client, amount=128.5, merchant="低额商户", purpose="实验耗材")
    high_id = create_confirmed_item(client, amount=1200, merchant="高额商户", purpose="设备配件", expense_date="2026-08-03")
    add_attachment(client, low_id, "invoice", "low-invoice.png")
    add_attachment(client, high_id, "invoice", "high-invoice.png")

    created = create_batch(
        client, [low_id, high_id], name="八月科研报销", project_id=1,
        purpose="实验室采购", notes="闭环测试",
    )
    assert created.status_code == 201, created.get_json()
    batch_id = created.get_json()["batch"]["id"]
    assert created.get_json()["batch"]["completeness"]["complete"] is False
    assert client.get("/api/items?status=pending_reimbursement").get_json()["count"] == 0

    blocked = export_batch(client, batch_id)
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "materials_incomplete"
    current = client.get(f"/api/batches/{batch_id}").get_json()["batch"]
    assert current["status"] == "draft"

    add_attachment(client, high_id, "purchase_list", "purchase-list.pdf", pdf=True)
    add_attachment(client, high_id, "payment_record", "payment.png")
    exported = export_batch(client, batch_id)
    assert exported.status_code == 200, exported.get_json()
    payload = exported.get_json()
    assert payload["batch"]["status"] == "submitted"
    assert payload["idempotent"] is False
    pdf_path = Path(payload["batch"]["pdf_path"])
    archive_path = Path(payload["batch"]["archive_path"])
    assert pdf_path.is_file()
    assert archive_path.is_dir()
    assert (archive_path / "归档清单.json").is_file()
    assert len(list((archive_path / "原始材料").rglob("*.*"))) == 4

    reader = PdfReader(str(pdf_path))
    assert len(reader.pages) >= 1 + 2 + 4
    for page in reader.pages:
        assert abs(float(page.mediabox.width) - A4[0]) < 1.5
        assert abs(float(page.mediabox.height) - A4[1]) < 1.5

    assert client.get("/api/items?status=pending_reimbursement").get_json()["count"] == 0
    assert client.get(f"/api/batches/{batch_id}/pdf").status_code == 200
    second = export_batch(client, batch_id)
    assert second.status_code == 200
    assert second.get_json()["idempotent"] is True
    assert second.get_json()["batch"]["pdf_path"] == str(pdf_path)

    locked = client.patch(
        f"/api/items/{low_id}",
        json={**item_version_fields(client, low_id), "merchant": "不应修改"},
    )
    assert locked.status_code == 409
    reimbursed = client.post(
        f"/api/batches/{batch_id}/mark-reimbursed",
        json={
            "expected_version": second.get_json()["batch"]["version"],
            "reimbursed_date": "2026-08-08", "notes": "已到账",
        },
    )
    assert reimbursed.status_code == 200
    assert reimbursed.get_json()["batch"]["status"] == "reimbursed"

    history = client.get("/api/history?status=reimbursed&search=高额商户&amount_min=1000&amount_max=2000&reimbursed_from=2026-08-08&reimbursed_to=2026-08-08")
    assert history.status_code == 200
    assert history.get_json()["count"] == 1
    record = history.get_json()["batches"][0]
    assert record["reimbursed_date"] == "2026-08-08"
    assert record["items"][0]["audit_logs"]
    assert record["audit_logs"]


def test_delete_history_requires_name_and_optionally_removes_archive(app, client):
    item_id = create_confirmed_item(client, merchant="需要删除的历史")
    item = add_attachment(client, item_id, "invoice", "history-delete.png")
    db = connect_db(app.config["DATABASE"])
    managed_path = Path(db.execute("SELECT managed_path FROM attachments WHERE expense_item_id=?", (item_id,)).fetchone()["managed_path"])
    db.close()
    batch = create_batch(client, [item_id], name="确认删除历史包", project_id=1).get_json()["batch"]
    exported = export_batch(client, batch["id"]).get_json()["batch"]
    archive_path = Path(exported["archive_path"])
    assert archive_path.is_dir()

    mismatch = client.delete(
        f"/api/batches/{batch['id']}",
        json={"expected_version": exported["version"], "confirmation": "名称不匹配", "delete_archive": True},
    )
    assert mismatch.status_code == 409
    assert archive_path.is_dir()

    deleted = client.delete(
        f"/api/batches/{batch['id']}",
        json={"expected_version": exported["version"], "confirmation": "确认删除历史包", "delete_archive": True},
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["archive_deleted"] is True
    assert not archive_path.exists()
    assert not managed_path.exists()
    assert client.get(f"/api/batches/{batch['id']}").status_code == 404
    assert client.get(f"/api/items/{item_id}").status_code == 404
    assert client.get("/api/history").get_json()["count"] == 0
    assert list(Path(app.config["BACKUP_DIR"]).glob("manual-*.sqlite3"))


def test_concurrent_export_creates_only_one_archive(app, client):
    item_id = create_confirmed_item(client, merchant="并发导出商户")
    add_attachment(client, item_id, "invoice", "concurrent.png")
    batch = create_batch(client, [item_id], name="并发幂等", project_id=1).get_json()["batch"]
    export_payload = {
        "expected_version": batch["version"],
        "expected_requirements_version": batch["requirements_version"],
        "confirmation_name": batch["name"],
    }

    def export_once():
        with app.test_client() as thread_client:
            response = thread_client.post(
                f"/api/batches/{batch['id']}/export", json=export_payload
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: export_once(), range(2)))

    assert sorted(status for status, _payload in results) == [200, 409]
    successful = next(payload for status, payload in results if status == 200)
    archive_paths = {successful["batch"]["archive_path"]}
    assert len(archive_paths) == 1
    archive_root = Path(app.config["DEFAULT_ARCHIVE_DIR"])
    assert len([path for path in archive_root.iterdir() if path.is_dir()]) == 1


def test_remove_item_before_export_returns_to_pool(client):
    first = create_confirmed_item(client, merchant="A")
    second = create_confirmed_item(client, merchant="B")
    batch = create_batch(client, [first, second], name="可撤回", project_id=1).get_json()["batch"]
    response = client.post(
        f"/api/batches/{batch['id']}/items/{first}/remove",
        json={
            "expected_version": batch["version"],
            "expected_item_version": item_version_fields(client, first)["expected_version"],
        },
    )
    assert response.status_code == 200
    pool_ids = [item["id"] for item in client.get("/api/items?status=pending_reimbursement").get_json()["items"]]
    assert first in pool_ids and second not in pool_ids

    current_batch = client.get(f"/api/batches/{batch['id']}").get_json()["batch"]
    removed_last = client.post(
        f"/api/batches/{batch['id']}/items/{second}/remove",
        json={
            "expected_version": current_batch["version"],
            "expected_item_version": item_version_fields(client, second)["expected_version"],
        },
    )
    assert removed_last.status_code == 200
    assert removed_last.get_json() == {"batch": None, "deleted": True}
    assert client.get("/api/batches?status=draft").get_json()["batches"] == []
    final_pool_ids = [item["id"] for item in client.get("/api/items?status=pending_reimbursement").get_json()["items"]]
    assert {first, second} <= set(final_pool_ids)


def test_attachment_category_and_custom_name(client):
    item_id = create_confirmed_item(client)
    item = add_attachment(client, item_id, "unknown", "bad name.png")
    attachment = item["attachments"][0]
    updated = client.patch(
        f"/api/attachments/{attachment['id']}",
        json={
            **item_version_fields(client, item_id),
            "category": "receipt", "normalized_name": "2026核对后的Receipt.png",
        },
    )
    assert updated.status_code == 200, updated.get_json()
    final = updated.get_json()["item"]["attachments"][0]
    assert final["category"] == "receipt"
    assert final["normalized_name"] == "2026核对后的Receipt.png"
    assert final["name_locked"] is True
    assert len(final["rename_history"]) >= 3

    batch = create_batch(client, [item_id], name="用户命名归档", project_id=1).get_json()["batch"]
    exported = export_batch(client, batch["id"])
    assert exported.status_code == 200, exported.get_json()
    archive = Path(exported.get_json()["batch"]["archive_path"])
    archived_names = [path.name for path in (archive / "原始材料").rglob("*.*")]
    assert "2026核对后的Receipt.png" in archived_names


def test_invalid_batch_and_history_filters_return_400(client):
    item_id = create_confirmed_item(client)

    invalid_batch = create_batch(
        client, [item_id], name="错误项目编号", project_id="not-a-number"
    )
    assert invalid_batch.status_code == 400
    assert invalid_batch.get_json()["error"] == "invalid_project"

    for query, error in (
        ("status=draft", "invalid_status"),
        ("project_id=not-a-number", "invalid_filter"),
        ("amount_min=not-a-number", "invalid_filter"),
        ("amount_max=Infinity", "invalid_filter"),
        ("submitted_from=not-a-date", "invalid_filter"),
    ):
        response = client.get(f"/api/history?{query}")
        assert response.status_code == 400
        assert response.get_json()["error"] == error

    non_finite = client.post(
        "/api/items/manual",
        json={"merchant": "异常金额", "amount": "NaN", "currency": "CNY", "project_id": 1},
    )
    assert non_finite.status_code == 400
    assert non_finite.get_json()["error"] == "invalid_amount"
