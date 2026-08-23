from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage

import invoice_assistant.storage as storage
from invoice_assistant import AppError


def _pdf_bytes(*, pages: int, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=420, height=595)
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _upload(content: bytes, filename: str, mime_type: str) -> FileStorage:
    return FileStorage(
        stream=BytesIO(content),
        filename=filename,
        content_type=mime_type,
    )


@pytest.mark.parametrize(
    "content",
    [
        b"%PDF-1.7\nthis-is-not-a-pdf\n%%EOF\n",
        _pdf_bytes(pages=0),
        _pdf_bytes(pages=1, password="secret"),
    ],
    ids=["corrupt-page-tree", "zero-pages", "encrypted"],
)
def test_invalid_pdf_is_rejected_without_leaking_blob(app, tmp_path, content):
    destination = tmp_path / "pdf-staging"

    with app.app_context(), pytest.raises(AppError) as raised:
        storage.import_uploaded_file(
            _upload(content, "invoice.pdf", "application/pdf"),
            destination_dir=destination,
            target_name="stored.pdf",
        )

    assert raised.value.status_code == 415
    assert raised.value.code == "invalid_file_content"
    assert raised.value.file_sha256 == hashlib.sha256(content).hexdigest()
    assert raised.value.display_basename == "invoice.pdf"
    assert raised.value.mime_type == "application/pdf"
    assert not (destination / "stored.pdf").exists()
    assert not [path for path in destination.rglob("*") if path.is_file()]


def test_pdf_page_limit_uses_recognition_limit_and_cleans_blob(app, tmp_path):
    destination = tmp_path / "pdf-staging"
    app.config["MAX_RECOGNITION_PDF_PAGES"] = 1

    with app.app_context(), pytest.raises(AppError) as raised:
        storage.import_uploaded_file(
            _upload(_pdf_bytes(pages=2), "invoice.pdf", "application/pdf"),
            destination_dir=destination,
            target_name="stored.pdf",
        )

    assert raised.value.status_code == 413
    assert raised.value.code == "pdf_too_many_pages"
    assert not (destination / "stored.pdf").exists()
    assert not [path for path in destination.rglob("*") if path.is_file()]


def test_valid_pdf_page_tree_is_accepted(app, tmp_path):
    destination = tmp_path / "pdf-staging"

    with app.app_context():
        result = storage.import_uploaded_file(
            _upload(_pdf_bytes(pages=1), "invoice.pdf", "application/pdf"),
            destination_dir=destination,
            target_name="stored.pdf",
        )

    assert result["managed_path"] == str((destination / "stored.pdf").resolve())
    assert Path(result["managed_path"]).is_file()


def test_pillow_decompression_bomb_is_413_and_cleans_blob(app, tmp_path, monkeypatch):
    destination = tmp_path / "image-staging"

    def reject_bomb(_path):
        raise storage.Image.DecompressionBombError("decompression bomb")

    monkeypatch.setattr(storage.Image, "open", reject_bomb)

    with app.app_context(), pytest.raises(AppError) as raised:
        storage.import_uploaded_file(
            _upload(b"non-empty-image", "invoice.png", "image/png"),
            destination_dir=destination,
            target_name="stored.png",
        )

    assert raised.value.status_code == 413
    assert raised.value.code == "image_too_large"
    assert not (destination / "stored.png").exists()
    assert not [path for path in destination.rglob("*") if path.is_file()]


def test_invalid_image_error_is_fixed_path_free_and_carries_safe_evidence(app, tmp_path):
    destination = tmp_path / "sensitive-operation-staging" / "private-operation-id"
    content = b"not-an-image"

    with app.app_context(), pytest.raises(AppError) as raised:
        storage.import_uploaded_file(
            _upload(content, "invoice.png", "image/png"),
            destination_dir=destination,
            target_name="stored.png",
        )

    assert raised.value.status_code == 415
    assert raised.value.code == "invalid_file_content"
    assert raised.value.message == "无法读取上传图片，文件内容无效。"
    assert str(destination) not in raised.value.message
    assert raised.value.file_sha256 == hashlib.sha256(content).hexdigest()
    assert raised.value.display_basename == "invoice.png"
    assert raised.value.mime_type == "image/png"
    assert not (destination / "stored.png").exists()
