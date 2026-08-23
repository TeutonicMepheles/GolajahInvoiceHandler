from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event

from PIL import Image, ImageDraw
from pypdf import PdfReader

from invoice_assistant import create_app
from invoice_assistant.db import connect_db, get_db
from invoice_assistant.storage import process_file_cleanup_queue

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


def landscape_image_bytes() -> BytesIO:
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, 1565, 865), outline="#1d6670", width=8)
    draw.text((100, 100), "Payment record / RMB paid 428.36", fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def payment_recognizer(_path, _mime, _name):
    return {
        "merchant": "支付平台",
        "expense_date": "2026-08-05",
        "amount": 428.36,
        "currency": "CNY",
        "converted_amount": None,
        "purpose": "外币交易实付",
        "document_type": "payment_record",
        "uncertainties": [],
        "confidence": 0.99,
    }


def test_frontend_and_api_responses_prevent_stale_or_embedded_content(client):
    for response in (client.get("/"), client.get("/static/app.js"), client.get("/api/bootstrap")):
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store, max-age=0"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_multi_file_import_is_atomic_and_leaves_no_blob(app, client):
    response = client.post(
        "/api/imports",
        data={
            "files": [
                (image_bytes("valid"), "valid.png"),
                (BytesIO(b"not an image"), "broken.png"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 415
    assert client.get("/api/drafts").get_json()["count"] == 0
    assert not list(Path(app.config["IMPORT_DIR"]).rglob("*.*"))


def test_foreign_payment_drives_rmb_total_pdf_and_history_repair(app, client):
    draft = client.post(
        "/api/items/manual",
        json={
            "merchant": "Overseas Vendor",
            "expense_date": "2026-08-05",
            "amount": 24,
            "currency": "USD",
            "purpose": "Cloud service",
            "project_id": 1,
        },
    ).get_json()["item"]
    item_id = draft["id"]
    confirmed = confirm_item(client, item_id)
    assert confirmed.status_code == 200
    assert confirmed.get_json()["item"]["reimbursement_amount"] is None
    add_attachment(client, item_id, "foreign_invoice", "invoice.png")
    batch = create_batch(client, [item_id], name="外币实付测试", project_id=1).get_json()["batch"]
    assert batch["total_amount"] == 0
    assert batch["completeness"]["complete"] is False

    app.config["RECOGNIZER"] = payment_recognizer
    uploaded = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            **{key: str(value) for key, value in item_version_fields(client, item_id).items()},
            "category": "payment_record", "file": (landscape_image_bytes(), "payment.png"),
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201, uploaded.get_json()
    item = uploaded.get_json()["item"]
    payment_id = next(entry["id"] for entry in item["attachments"] if entry["category"] == "payment_record")
    assert item["amount"] == 24
    assert item["currency"] == "USD"
    assert item["reimbursement_amount"] == 428.36
    assert item["material"]["complete"] is True
    assert client.get(f"/api/batches/{batch['id']}").get_json()["batch"]["total_amount"] == 428.36

    removed = client.delete(
        f"/api/attachments/{payment_id}", json=item_version_fields(client, item_id)
    )
    assert removed.status_code == 200
    assert removed.get_json()["item"]["converted_amount"] is None
    assert client.get(f"/api/batches/{batch['id']}").get_json()["batch"]["total_amount"] == 0

    uploaded = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            **{key: str(value) for key, value in item_version_fields(client, item_id).items()},
            "category": "payment_record", "file": (landscape_image_bytes(), "payment-again.png"),
        },
        content_type="multipart/form-data",
    )
    payment_id = next(
        entry["id"] for entry in uploaded.get_json()["item"]["attachments"] if entry["category"] == "payment_record"
    )
    exported = export_batch(client, batch["id"])
    assert exported.status_code == 200, exported.get_json()
    exported_payload = exported.get_json()
    assert exported_payload["batch"]["currency_totals"] == [{"amount": 428.36, "currency": "CNY"}]
    assert exported_payload["pdf_report"]["landscape_pages"] == 1
    pdf_path = Path(exported_payload["batch"]["pdf_path"])
    reader = PdfReader(str(pdf_path))
    directory_text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
    assert "428.36 CNY" in directory_text
    assert "24.00 USD" not in directory_text

    old_archive = Path(exported_payload["batch"]["archive_path"])
    db = connect_db(app.config["DATABASE"])
    db.execute("UPDATE attachments SET category='unknown' WHERE id=?", (payment_id,))
    db.execute(
        "UPDATE expense_items SET converted_amount=NULL,converted_amount_cents=NULL WHERE id=?",
        (item_id,),
    )
    db.commit()
    db.close()
    integrity = client.get("/api/settings").get_json()["integrity"]
    assert any(entry["code"] == "foreign_payment_missing" for entry in integrity["entries"])
    reopened = client.post(
        f"/api/batches/{batch['id']}/reopen",
        json={
            "expected_version": exported_payload["batch"]["version"],
            "confirmation": "外币实付测试",
        },
    )
    assert reopened.status_code == 200, reopened.get_json()
    assert reopened.get_json()["batch"]["status"] == "draft"
    assert old_archive.is_dir()

    repaired = client.patch(
        f"/api/attachments/{payment_id}",
        json={**item_version_fields(client, item_id), "category": "payment_record"},
    )
    assert repaired.status_code == 200
    assert repaired.get_json()["item"]["reimbursement_amount"] == 428.36
    reexported = export_batch(client, batch["id"])
    assert reexported.status_code == 200, reexported.get_json()
    assert Path(reexported.get_json()["batch"]["archive_path"]) != old_archive
    assert not old_archive.exists()
    assert client.get("/api/settings").get_json()["integrity"]["healthy"] is True


def test_failed_payment_recognition_can_be_retried_without_reupload(app, client):
    draft = client.post(
        "/api/items/manual",
        json={
            "merchant": "Retry Vendor",
            "expense_date": "2026-08-06",
            "amount": 30,
            "currency": "USD",
            "purpose": "Subscription",
            "project_id": 1,
        },
    ).get_json()["item"]
    item_id = draft["id"]
    assert confirm_item(client, item_id).status_code == 200
    add_attachment(client, item_id, "foreign_invoice", "retry-invoice.png")
    batch = create_batch(client, [item_id], name="支付重试", project_id=1).get_json()["batch"]

    app.config["RECOGNIZER"] = lambda *_args: (_ for _ in ()).throw(RuntimeError("temporary outage"))
    uploaded = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            **{key: str(value) for key, value in item_version_fields(client, item_id).items()},
            "category": "payment_record", "file": (landscape_image_bytes(), "retry-payment.png"),
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201
    payment = next(
        entry for entry in uploaded.get_json()["item"]["attachments"] if entry["category"] == "payment_record"
    )
    assert payment["recognition_error"]
    assert uploaded.get_json()["item"]["reimbursement_amount"] is None

    app.config["RECOGNIZER"] = payment_recognizer
    retried = client.post(
        f"/api/attachments/{payment['id']}/recognize-payment",
        json=item_version_fields(client, item_id),
    )
    assert retried.status_code == 200, retried.get_json()
    assert retried.get_json()["item"]["reimbursement_amount"] == 428.36
    updated_batch = client.get(f"/api/batches/{batch['id']}").get_json()["batch"]
    assert updated_batch["total_amount"] == 428.36
    assert any(log["action"] == "payment_recognition_retried" for log in retried.get_json()["item"]["audit_logs"])


def test_export_reservation_rejects_late_attachment_and_manifest_matches(app, client, monkeypatch):
    item_id = create_confirmed_item(client, merchant="并发冻结")
    add_attachment(client, item_id, "invoice", "first.png")
    batch = create_batch(client, [item_id], name="导出冻结", project_id=1).get_json()["batch"]
    export_payload = {
        "expected_version": batch["version"],
        "expected_requirements_version": batch["requirements_version"],
        "confirmation_name": batch["name"],
    }

    import invoice_assistant.batch_service as batch_service

    entered_pdf = Event()
    allow_pdf = Event()
    original_generate = batch_service.generate_material_package

    def paused_generate(*args, **kwargs):
        entered_pdf.set()
        assert allow_pdf.wait(10)
        return original_generate(*args, **kwargs)

    monkeypatch.setattr(batch_service, "generate_material_package", paused_generate)

    def export_once():
        with app.test_client() as thread_client:
            response = thread_client.post(
                f"/api/batches/{batch['id']}/export", json=export_payload
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(export_once)
        assert entered_pdf.wait(10)
        late = client.post(
            f"/api/items/{item_id}/attachments",
            data={
                **{key: str(value) for key, value in item_version_fields(client, item_id).items()},
                "category": "invoice", "file": (image_bytes("late"), "late.png"),
            },
            content_type="multipart/form-data",
        )
        assert late.status_code == 409
        assert late.get_json()["error"] == "batch_exporting"
        allow_pdf.set()
        status, payload = future.result(timeout=20)

    assert status == 200
    archive = Path(payload["batch"]["archive_path"])
    manifest = json.loads((archive / "归档清单.json").read_text(encoding="utf-8"))
    assert len(manifest["batch"]["items"][0]["attachments"]) == 1
    assert len(client.get(f"/api/items/{item_id}").get_json()["item"]["attachments"]) == 1


def test_cleanup_failure_is_persisted_and_retried(app, client, monkeypatch):
    imported = client.post(
        "/api/imports",
        data={"files": (image_bytes("cleanup"), "cleanup.png")},
        content_type="multipart/form-data",
    ).get_json()["results"][0]["item"]
    db = connect_db(app.config["DATABASE"])
    managed = Path(db.execute("SELECT managed_path FROM attachments WHERE expense_item_id=?", (imported["id"],)).fetchone()["managed_path"])
    db.close()

    import invoice_assistant.storage as storage

    original_cleanup = storage._cleanup_one
    monkeypatch.setattr(storage, "_cleanup_one", lambda _row: (_ for _ in ()).throw(OSError("locked")))
    deleted = client.delete(
        f"/api/items/{imported['id']}",
        json={"expected_version": imported["version"]},
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["cleanup_warnings"] == ["cleanup_failed"]
    assert managed.is_file()
    settings = client.get("/api/settings").get_json()
    assert settings["reconciliation"]["pending_cleanup_count"] == 1
    assert settings["reconciliation"]["failed_cleanup_count"] == 1

    monkeypatch.setattr(storage, "_cleanup_one", original_cleanup)
    with app.app_context():
        result = process_file_cleanup_queue(get_db())
    assert result["completed"] == 1
    assert not managed.exists()
    assert list(Path(app.config["TRASH_DIR"]).rglob("*.*"))


def test_startup_cleanup_warning_logs_only_safe_summaries(app, monkeypatch, caplog):
    import invoice_assistant.storage as storage

    secret_name = "private-customer-invoice-2026.png"
    raw_error = "locked at C:\\private\\customer\\invoice.png"
    trash_raw_error = "locked at C:\\private\\customer\\trash-backup.png"
    target = Path(app.config["IMPORT_DIR"]) / "blobs" / secret_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"managed")
    db = connect_db(app.config["DATABASE"])
    try:
        storage.enqueue_file_cleanup(
            db,
            target,
            "managed_file",
            app.config["IMPORT_DIR"],
            "startup cleanup log regression",
        )
        db.commit()
        cleanup_id = db.execute(
            "SELECT id FROM file_cleanup_queue WHERE path=?", (str(target.resolve()),)
        ).fetchone()["id"]
    finally:
        db.close()

    def fail_with_sensitive_details(_row):
        raise OSError(raw_error)

    trash_bucket_date = "2000-01-01"
    trash_secret_name = "private-customer-trash-backup.png"
    trash_bucket = Path(app.config["TRASH_DIR"]) / "imports" / trash_bucket_date
    trash_secret = trash_bucket / "opaque" / trash_secret_name
    trash_secret.parent.mkdir(parents=True, exist_ok=True)
    trash_secret.write_bytes(b"trash")

    def fail_trash_purge(_path, *_args, **_kwargs):
        raise OSError(trash_raw_error)

    monkeypatch.setattr(storage, "_cleanup_one", fail_with_sensitive_details)
    monkeypatch.setattr(storage.shutil, "rmtree", fail_trash_purge)
    caplog.set_level("WARNING")
    restarted = create_app(
        {
            "TESTING": True,
            "AUTO_BACKUP": False,
            "DATA_DIR": app.config["DATA_DIR"],
            "DATABASE": app.config["DATABASE"],
            "IMPORT_DIR": app.config["IMPORT_DIR"],
            "DEFAULT_ARCHIVE_DIR": app.config["DEFAULT_ARCHIVE_DIR"],
            "TEMP_DIR": app.config["TEMP_DIR"],
            "TRASH_DIR": app.config["TRASH_DIR"],
            "BACKUP_DIR": app.config["BACKUP_DIR"],
            "RECOGNIZER": app.config["RECOGNIZER"],
        }
    )

    assert restarted is not None
    assert f"'id': {cleanup_id}" in caplog.text
    assert "cleanup_failed" in caplog.text
    assert f"'bucket_date': '{trash_bucket_date}'" in caplog.text
    assert "trash_cleanup_failed" in caplog.text
    assert secret_name not in caplog.text
    assert raw_error not in caplog.text
    assert "C:\\private\\customer" not in caplog.text
    assert trash_secret_name not in caplog.text
    assert trash_raw_error not in caplog.text


def test_exact_historical_duplicate_requires_explicit_acknowledgement(client):
    item_id = create_confirmed_item(client, merchant="历史重复")
    original = image_bytes("same-file")
    content = original.getvalue()
    attached = client.post(
        f"/api/items/{item_id}/attachments",
        data={
            **{key: str(value) for key, value in item_version_fields(client, item_id).items()},
            "category": "invoice",
            "file": (BytesIO(content), "same.png"),
        },
        content_type="multipart/form-data",
    )
    assert attached.status_code == 201
    batch = create_batch(client, [item_id], name="历史重复包", project_id=1).get_json()["batch"]
    assert export_batch(client, batch["id"]).status_code == 200

    imported = client.post(
        "/api/imports",
        data={"files": (BytesIO(content), "same-again.png")},
        content_type="multipart/form-data",
    ).get_json()["results"][0]
    assert any(match["historical"] and match["exact_file"] for match in imported["matches"])
    blocked_payload = confirmation_payload(client, imported["item"]["id"])
    blocked_payload["acknowledged_duplicate_ids"] = []
    blocked = client.post(
        f"/api/items/{imported['item']['id']}/confirm", json=blocked_payload
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "duplicates_not_acknowledged"
    acknowledged = confirm_item(client, imported["item"]["id"])
    assert acknowledged.status_code == 200
