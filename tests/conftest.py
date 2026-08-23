from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas

from invoice_assistant import create_app
from invoice_assistant.migrate import migrate_database


def fake_recognizer(_path, _mime, _name):
    return {
        "merchant": "示例科技有限公司",
        "expense_date": "2026-08-01",
        "amount": 128.50,
        "currency": "CNY",
        "converted_amount": None,
        "purpose": "实验耗材",
        "document_type": "invoice",
        "uncertainties": [],
        "confidence": 0.98,
    }


@pytest.fixture()
def app(tmp_path):
    data_dir = tmp_path / "data"
    database = data_dir / "test.sqlite3"
    migrate_database(
        data_dir,
        database_path=database,
        default_archive_dir=data_dir / "archives",
        expect_no_database=True,
    )
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(database),
            "IMPORT_DIR": str(data_dir / "imports"),
            "DEFAULT_ARCHIVE_DIR": str(data_dir / "archives"),
            "TEMP_DIR": str(data_dir / "tmp"),
            "RECOGNIZER": fake_recognizer,
        }
    )
    app.config["TEST_ROOT"] = str(tmp_path)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def image_bytes(label="invoice") -> BytesIO:
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 860, 1160), outline="#1d6670", width=6)
    draw.text((90, 100), label, fill="black")
    draw.text((90, 190), "Amount: 128.50 CNY", fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def pdf_bytes(label="Purchase list") -> BytesIO:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(420, 595))
    pdf.setFont("Helvetica", 15)
    pdf.drawString(50, 520, label)
    pdf.drawString(50, 490, "Line item A / 1,200.00 CNY")
    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer


def confirmation_payload(client, item_id):
    item = client.get(f"/api/items/{item_id}").get_json()["item"]
    review = item["review"]
    blocking = review["blocking_duplicate_ids"]
    return {
        "expected_version": item["version"],
        "review_token": review["token"],
        "duplicate_resolution": "keep_separate" if blocking else "none",
        "acknowledged_uncertainty_ids": [
            entry["uncertainty_id"] for entry in review["uncertainties"]
        ],
        "acknowledged_duplicate_ids": blocking,
    }


def confirm_item(client, item_id):
    return client.post(
        f"/api/items/{item_id}/confirm",
        json=confirmation_payload(client, item_id),
    )


def create_confirmed_item(client, amount=128.5, merchant="示例科技有限公司", purpose="实验耗材", expense_date="2026-08-01"):
    response = client.post(
        "/api/items/manual",
        json={
            "merchant": merchant,
            "expense_date": expense_date,
            "amount": amount,
            "currency": "CNY",
            "purpose": purpose,
            "project_id": 1,
        },
    )
    assert response.status_code == 201, response.get_json()
    item_id = response.get_json()["item"]["id"]
    response = confirm_item(client, item_id)
    assert response.status_code == 200, response.get_json()
    return item_id


def add_attachment(client, item_id, category="invoice", filename="invoice.png", pdf=False):
    content = pdf_bytes(filename) if pdf else image_bytes(filename)
    item = client.get(f"/api/items/{item_id}").get_json()["item"]
    data = {
        "category": category,
        "expected_version": str(item["version"]),
        "file": (content, filename),
    }
    if item["batch_ref"]:
        data["expected_batch_version"] = str(item["batch_ref"]["batch_version"])
    response = client.post(
        f"/api/items/{item_id}/attachments",
        data=data,
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["item"]


def create_batch(client, item_ids, **values):
    payload = {
        "name": values.pop("name", "测试报销包"),
        "project_id": values.pop("project_id", 1),
        "items": [
            {
                "item_id": item_id,
                "expected_version": client.get(f"/api/items/{item_id}").get_json()["item"]["version"],
            }
            for item_id in item_ids
        ],
        **values,
    }
    return client.post("/api/batches", json=payload)


def export_batch(client, batch_id):
    batch = client.get(f"/api/batches/{batch_id}").get_json()["batch"]
    return client.post(
        f"/api/batches/{batch_id}/export",
        json={
            "expected_version": batch["version"],
            "expected_requirements_version": batch["requirements_version"],
            "confirmation_name": batch["name"],
        },
    )


def item_version_fields(client, item_id):
    item = client.get(f"/api/items/{item_id}").get_json()["item"]
    result = {"expected_version": item["version"]}
    if item["batch_ref"]:
        result["expected_batch_version"] = item["batch_ref"]["batch_version"]
    return result
