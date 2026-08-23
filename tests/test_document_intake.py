from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from invoice_assistant.db import connect_db
from invoice_assistant.features.document_intake import service as intake_service

from .conftest import add_attachment, confirm_item, image_bytes, pdf_bytes


def _recognizer_for(categories: dict[str, dict]):
    def recognize(_path, _mime, original_name):
        values = categories[original_name]
        return {
            "merchant": values.get("merchant", "测试商户"),
            "expense_date": values.get("expense_date", "2026-08-20"),
            "amount": values.get("amount", 120),
            "currency": values.get("currency", "CNY"),
            "converted_amount": values.get("converted_amount"),
            "purpose": values.get("purpose", "测试材料"),
            "document_type": values["document_type"],
            "uncertainties": [],
            "confidence": 0.98,
        }

    return recognize


def _import(client, filename: str, *, pdf: bool = False) -> dict:
    content = pdf_bytes(filename) if pdf else image_bytes(filename)
    response = client.post(
        "/api/imports",
        data={"files": (content, filename)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["results"][0]["item"]


def _associate(client, source: dict, target: dict, **changes):
    payload = {
        "source_item_id": source["id"],
        "target_item_id": target["id"],
        "source_version": source["version"],
        "target_version": target["version"],
        **changes,
    }
    return client.post("/api/document-intake/associations", json=payload)


def _managed_attachment(app, item_id: int):
    return _managed_attachments(app, item_id)[0]


def _managed_attachments(app, item_id: int):
    db = connect_db(app.config["DATABASE"])
    try:
        return db.execute(
            "SELECT * FROM attachments WHERE expense_item_id=? ORDER BY id",
            (item_id,),
        ).fetchall()
    finally:
        db.close()


def test_association_moves_supplementary_files_without_copying_and_keeps_audit(app, client):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "primary.png": {"document_type": "invoice", "merchant": "主凭据商户"},
            "list.png": {"document_type": "purchase_list", "merchant": "附件商户"},
        }
    )
    target = _import(client, "primary.png")
    source = _import(client, "list.png")
    source = add_attachment(client, source["id"], "unknown", "extra.png")
    source_rows = _managed_attachments(app, source["id"])
    original_files_by_id = {
        row["id"]: (
            Path(row["managed_path"]),
            hashlib.sha256(Path(row["managed_path"]).read_bytes()).hexdigest(),
        )
        for row in source_rows
    }
    original_files = sorted(path.resolve() for path in Path(app.config["IMPORT_DIR"]).rglob("*.*"))

    response = _associate(client, source, target)

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    item = payload["item"]
    assert [entry["category"] for entry in item["attachments"]] == ["invoice", "purchase_list", "unknown"]
    assert payload["association"] == {
        "source_item_id": source["id"],
        "target_item_id": target["id"],
        "attachment_ids": [row["id"] for row in source_rows],
        "source_version": source["version"] + 1,
        "target_version": target["version"] + 1,
    }
    moved = next(entry for entry in item["attachments"] if entry["id"] == source_rows[0]["id"])
    assert moved["preview_url"] == f"/api/attachments/{source_rows[0]['id']}/preview"
    assert moved["thumbnail_url"] == f"/api/attachments/{source_rows[0]['id']}/thumbnail"
    assert "主凭据商户" in moved["normalized_name"]

    db = connect_db(app.config["DATABASE"])
    try:
        source_after = db.execute(
            "SELECT status,merged_into_item_id,row_version FROM expense_items WHERE id=?",
            (source["id"],),
        ).fetchone()
        attachments_after = db.execute(
            "SELECT id,expense_item_id,managed_path FROM attachments WHERE id IN (?,?) ORDER BY id",
            tuple(row["id"] for row in source_rows),
        ).fetchall()
        actions = {
            row["action"]
            for row in db.execute(
                "SELECT action FROM audit_logs WHERE object_type='item' AND object_id IN (?,?)",
                (source["id"], target["id"]),
            ).fetchall()
        }
        count = db.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
    finally:
        db.close()
    assert dict(source_after) == {
        "status": "merged",
        "merged_into_item_id": target["id"],
        "row_version": source["version"] + 1,
    }
    assert all(row["expense_item_id"] == target["id"] for row in attachments_after)
    assert count == 3
    for row in attachments_after:
        original_path, original_hash = original_files_by_id[row["id"]]
        assert Path(row["managed_path"]) == original_path
        assert hashlib.sha256(original_path.read_bytes()).hexdigest() == original_hash
    assert sorted(path.resolve() for path in Path(app.config["IMPORT_DIR"]).rglob("*.*")) == original_files
    assert "supplementary_materials_associated" in actions
    assert "supplementary_materials_received" in actions
    assert source["id"] not in {entry["id"] for entry in client.get("/api/drafts").get_json()["items"]}


def test_association_rejects_primary_source_nonprimary_target_and_nonexact_body(app, client):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "primary-a.png": {"document_type": "invoice"},
            "primary-b.png": {"document_type": "receipt"},
            "aux-a.png": {"document_type": "purchase_list"},
            "aux-b.png": {"document_type": "unknown"},
        }
    )
    primary_a = _import(client, "primary-a.png")
    primary_b = _import(client, "primary-b.png")
    aux_a = _import(client, "aux-a.png")
    aux_b = _import(client, "aux-b.png")

    primary_source = _associate(client, primary_a, primary_b)
    assert primary_source.status_code == 409
    assert primary_source.get_json()["error"] == "source_contains_primary_document"

    no_primary_target = _associate(client, aux_a, aux_b)
    assert no_primary_target.status_code == 409
    assert no_primary_target.get_json()["error"] == "target_primary_document_required"

    empty_source_response = client.post(
        "/api/items/manual",
        json={
            "merchant": "无附件草稿",
            "expense_date": "2026-08-20",
            "amount": 1,
            "currency": "CNY",
            "purpose": "空草稿",
            "project_id": 1,
        },
    )
    empty_source = empty_source_response.get_json()["item"]
    no_files = _associate(client, empty_source, primary_a)
    assert no_files.status_code == 409
    assert no_files.get_json()["error"] == "source_attachment_required"

    same_item = _associate(client, aux_a, aux_a)
    assert same_item.status_code == 409
    assert same_item.get_json()["error"] == "invalid_association"

    invalid_body = client.post(
        "/api/document-intake/associations",
        json={
            "source_item_id": aux_a["id"],
            "target_item_id": primary_a["id"],
            "source_version": aux_a["version"],
            "target_version": primary_a["version"],
            "unexpected": True,
        },
    )
    assert invalid_body.status_code == 400
    assert invalid_body.get_json()["error"] == "invalid_request"


def test_association_requires_current_pending_drafts_and_rolls_back_on_rebind_failure(
    app, client, monkeypatch
):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "primary.png": {"document_type": "invoice"},
            "aux.png": {"document_type": "payment_record"},
            "locked.png": {"document_type": "unknown"},
        }
    )
    target = _import(client, "primary.png")
    source = _import(client, "aux.png")
    locked = _import(client, "locked.png")

    stale = _associate(client, source, target, source_version=source["version"] + 1)
    assert stale.status_code == 409
    assert stale.get_json()["error"] == "stale_version"
    stale_target = _associate(client, source, target, target_version=target["version"] + 1)
    assert stale_target.status_code == 409
    assert stale_target.get_json()["error"] == "stale_version"
    assert _managed_attachment(app, source["id"])["expense_item_id"] == source["id"]

    assert confirm_item(client, locked["id"]).status_code == 200
    locked = client.get(f"/api/items/{locked['id']}").get_json()["item"]
    not_pending = _associate(client, locked, target)
    assert not_pending.status_code == 409
    assert not_pending.get_json()["error"] == "source_not_pending_confirmation"

    source_row = _managed_attachment(app, source["id"])
    monkeypatch.setattr(
        intake_service,
        "bind_attachment_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rebind failed")),
    )
    with pytest.raises(RuntimeError, match="rebind failed"):
        _associate(client, source, target)

    db = connect_db(app.config["DATABASE"])
    try:
        source_after = db.execute(
            "SELECT status,merged_into_item_id,row_version FROM expense_items WHERE id=?",
            (source["id"],),
        ).fetchone()
        target_after = db.execute(
            "SELECT row_version FROM expense_items WHERE id=?", (target["id"],)
        ).fetchone()
        owner = db.execute(
            "SELECT expense_item_id FROM attachments WHERE id=?", (source_row["id"],)
        ).fetchone()["expense_item_id"]
    finally:
        db.close()
    assert dict(source_after) == {
        "status": "pending_confirmation",
        "merged_into_item_id": None,
        "row_version": source["version"],
    }
    assert target_after["row_version"] == target["version"]
    assert owner == source["id"]

    assert confirm_item(client, target["id"]).status_code == 200
    target = client.get(f"/api/items/{target['id']}").get_json()["item"]
    locked_target = _associate(client, source, target)
    assert locked_target.status_code == 409
    assert locked_target.get_json()["error"] == "target_not_pending_confirmation"


def test_payment_record_association_refreshes_foreign_reimbursement_amount(app, client):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "foreign.png": {
                "document_type": "foreign_invoice",
                "currency": "USD",
                "amount": 50,
                "converted_amount": None,
            },
            "payment.png": {
                "document_type": "payment_record",
                "currency": "CNY",
                "amount": 321.09,
                "converted_amount": 321.09,
            },
        }
    )
    target = _import(client, "foreign.png")
    source = _import(client, "payment.png")
    assert target["converted_amount"] is None

    response = _associate(client, source, target)

    assert response.status_code == 200, response.get_json()
    item = response.get_json()["item"]
    assert item["currency"] == "USD"
    assert item["converted_amount"] == 321.09
    assert item["converted_amount_cents"] == 32109
    assert item["reimbursement_amount"] == 321.09


def test_attachment_preview_and_thumbnail_support_images_and_pdf(app, client):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "primary.png": {"document_type": "invoice"},
            "primary.pdf": {"document_type": "receipt"},
        }
    )
    image_item = _import(client, "primary.png")
    pdf_item = _import(client, "primary.pdf", pdf=True)

    for item, expected_mime, prefix in (
        (image_item, "image/png", b"\x89PNG"),
        (pdf_item, "application/pdf", b"%PDF"),
    ):
        attachment = item["attachments"][0]
        preview = client.get(attachment["preview_url"])
        assert preview.status_code == 200
        assert preview.mimetype == expected_mime
        assert preview.headers["Content-Disposition"].startswith("inline")
        assert preview.data.startswith(prefix)
        assert preview.headers["X-Content-Type-Options"] == "nosniff"
        assert preview.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in preview.headers["Content-Security-Policy"]

        preview_head = client.head(attachment["preview_url"])
        assert preview_head.status_code == 200
        assert preview_head.mimetype == expected_mime
        assert preview_head.data == b""
        assert preview_head.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in preview_head.headers["Content-Security-Policy"]

        thumbnail = client.get(attachment["thumbnail_url"])
        assert thumbnail.status_code == 200
        assert thumbnail.mimetype == "image/jpeg"
        assert thumbnail.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in thumbnail.headers["Content-Security-Policy"]
        assert thumbnail.data.startswith(b"\xff\xd8")
        with Image.open(BytesIO(thumbnail.data)) as rendered:
            assert max(rendered.size) <= intake_service.THUMBNAIL_MAX_DIMENSION


def test_bootstrap_advertises_document_intake_capabilities_and_remains_non_frameable(client):
    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    assert response.get_json()["capabilities"] == {
        "document_intake_association": True,
        "inline_attachment_preview": True,
    }
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-src 'self'" in response.headers["Content-Security-Policy"]


def test_preview_rejects_missing_and_out_of_root_managed_files(app, client, tmp_path):
    app.config["RECOGNIZER"] = _recognizer_for(
        {
            "missing.png": {"document_type": "invoice"},
            "unsafe.png": {"document_type": "receipt"},
        }
    )
    missing = _import(client, "missing.png")["attachments"][0]
    unsafe = _import(client, "unsafe.png")["attachments"][0]

    missing_row = _managed_attachment(app, missing["expense_item_id"])
    Path(missing_row["managed_path"]).unlink()
    missing_response = client.get(missing["preview_url"])
    assert missing_response.status_code == 404
    assert missing_response.get_json()["error"] == "attachment_file_not_found"

    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes("outside").getvalue())
    db = connect_db(app.config["DATABASE"])
    try:
        db.execute("UPDATE attachments SET managed_path=? WHERE id=?", (str(outside), unsafe["id"]))
        db.commit()
    finally:
        db.close()
    unsafe_response = client.get(unsafe["thumbnail_url"])
    assert unsafe_response.status_code == 409
    assert unsafe_response.get_json()["error"] == "unsafe_path"
