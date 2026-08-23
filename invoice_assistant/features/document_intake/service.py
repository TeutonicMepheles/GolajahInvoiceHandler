from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from ... import AppError
from ...db import audit, transaction, utc_now
from ...domain import (
    CATEGORY_META,
    refresh_foreign_payment_amount,
    serialize_item,
)
from ...storage import ALLOWED_MIME_TYPES, bind_attachment_file


THUMBNAIL_MAX_DIMENSION = 360


@dataclass(frozen=True)
class AttachmentFile:
    path: Path
    mime_type: str
    display_name: str


def _is_primary_category(category: str) -> bool:
    metadata = CATEGORY_META.get(category)
    return bool(metadata and metadata["material"] == "primary_receipt")


def _touch_item(db, item_id: int) -> int:
    row = db.execute(
        "UPDATE expense_items SET row_version=row_version+1,updated_at=? WHERE id=? RETURNING row_version",
        (utc_now(), item_id),
    ).fetchone()
    if not row:
        raise AppError("条目不存在。", 404, "item_not_found")
    return int(row["row_version"])


def associate_supplementary_item(
    db,
    *,
    source_item_id: int,
    target_item_id: int,
    source_version: int,
    target_version: int,
) -> dict:
    """Move every supplementary attachment from one intake draft to a primary draft."""
    if source_item_id == target_item_id:
        raise AppError("附件草稿不能关联到自身。", 409, "invalid_association")

    with transaction(db):
        source = db.execute(
            "SELECT id,status,row_version FROM expense_items WHERE id=?",
            (source_item_id,),
        ).fetchone()
        target = db.execute(
            "SELECT id,status,row_version FROM expense_items WHERE id=?",
            (target_item_id,),
        ).fetchone()
        if not source or not target:
            raise AppError("条目不存在。", 404, "item_not_found")
        if int(source["row_version"]) != source_version or int(target["row_version"]) != target_version:
            raise AppError("条目已被其他操作修改，请刷新后重试。", 409, "stale_version")
        if source["status"] != "pending_confirmation":
            raise AppError("只有待确认的附件草稿可以设定关联。", 409, "source_not_pending_confirmation")
        if target["status"] != "pending_confirmation":
            raise AppError("附件只能关联到待确认的主文件草稿。", 409, "target_not_pending_confirmation")

        source_attachments = db.execute(
            "SELECT id,category FROM attachments WHERE expense_item_id=? ORDER BY page_order,id",
            (source_item_id,),
        ).fetchall()
        if not source_attachments:
            raise AppError("附件草稿中没有可关联的文件。", 409, "source_attachment_required")
        if any(_is_primary_category(row["category"]) for row in source_attachments):
            raise AppError(
                "中国增值税发票、Invoice 或 Receipt 必须作为主文件核对，不能作为普通附件关联。",
                409,
                "source_contains_primary_document",
            )
        target_has_primary = any(
            _is_primary_category(row["category"])
            for row in db.execute(
                "SELECT category FROM attachments WHERE expense_item_id=?",
                (target_item_id,),
            ).fetchall()
        )
        if not target_has_primary:
            raise AppError(
                "关联目标必须包含中国增值税发票、Invoice 或 Receipt 主文件。",
                409,
                "target_primary_document_required",
            )

        attachment_ids = [int(row["id"]) for row in source_attachments]
        now = utc_now()
        db.execute(
            "UPDATE attachments SET expense_item_id=?,updated_at=? WHERE expense_item_id=?",
            (target_item_id, now, source_item_id),
        )
        db.execute(
            "UPDATE expense_items SET status='merged',merged_into_item_id=?,updated_at=? WHERE id=?",
            (target_item_id, now, source_item_id),
        )
        for attachment_id in attachment_ids:
            bind_attachment_file(db, attachment_id, "用户设定主文件与附件关联")
        refresh_foreign_payment_amount(db, target_item_id)
        new_source_version = _touch_item(db, source_item_id)
        new_target_version = _touch_item(db, target_item_id)
        audit(
            db,
            "item",
            source_item_id,
            "supplementary_materials_associated",
            {"target_item_id": target_item_id, "attachment_ids": attachment_ids},
        )
        audit(
            db,
            "item",
            target_item_id,
            "supplementary_materials_received",
            {"source_item_id": source_item_id, "attachment_ids": attachment_ids},
        )
        item = serialize_item(db, target_item_id, include_audit=True)

    return {
        "item": item,
        "association": {
            "source_item_id": source_item_id,
            "target_item_id": target_item_id,
            "attachment_ids": attachment_ids,
            "source_version": new_source_version,
            "target_version": new_target_version,
        },
    }


def resolve_attachment_file(db, attachment_id: int, import_root: str | Path) -> AttachmentFile:
    row = db.execute(
        "SELECT managed_path,mime_type,normalized_name FROM attachments WHERE id=?",
        (attachment_id,),
    ).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    root = Path(import_root).resolve()
    path = Path(row["managed_path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise AppError("附件路径超出受管目录。", 409, "unsafe_path")
    if not path.is_file():
        raise AppError("附件文件不存在。", 404, "attachment_file_not_found")
    mime_type = str(row["mime_type"] or "").lower()
    if mime_type not in ALLOWED_MIME_TYPES:
        raise AppError("附件文件类型不支持预览。", 415, "unsupported_preview_type")
    return AttachmentFile(path=path, mime_type=mime_type, display_name=row["normalized_name"])


def _fit_rgb(image: Image.Image, max_dimension: int) -> Image.Image:
    if "A" in image.getbands():
        rgba = image.convert("RGBA")
        result = Image.new("RGB", rgba.size, "white")
        result.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
    else:
        result = image.convert("RGB")
    result.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return result


def _image_thumbnail(path: Path, max_dimension: int, max_image_pixels: int) -> Image.Image:
    try:
        with Image.open(path) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > max_image_pixels:
                raise AppError("图片尺寸无效或像素过大。", 413, "image_too_large")
            oriented = ImageOps.exif_transpose(source)
            try:
                return _fit_rgb(oriented, max_dimension)
            finally:
                if oriented is not source:
                    oriented.close()
    except AppError:
        raise
    except Image.DecompressionBombError as exc:
        raise AppError("图片解压后的像素数量过大。", 413, "image_too_large") from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise AppError("附件图片无法生成缩略图。", 422, "thumbnail_render_failed") from exc


def _pdf_thumbnail(path: Path, max_dimension: int) -> Image.Image:
    document = None
    page = None
    bitmap = None
    rendered = None
    try:
        document = pdfium.PdfDocument(str(path))
        if len(document) < 1:
            raise AppError("PDF 没有可预览页面。", 422, "thumbnail_render_failed")
        page = document[0]
        width, height = page.get_size()
        longest_side = max(float(width), float(height), 1.0)
        bitmap = page.render(scale=min(2.0, max_dimension / longest_side))
        rendered = bitmap.to_pil()
        return _fit_rgb(rendered, max_dimension)
    except AppError:
        raise
    except Exception as exc:
        raise AppError("PDF 首页无法生成缩略图。", 422, "thumbnail_render_failed") from exc
    finally:
        if rendered is not None:
            rendered.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        if document is not None:
            document.close()


def build_attachment_thumbnail(
    attachment: AttachmentFile,
    *,
    max_dimension: int = THUMBNAIL_MAX_DIMENSION,
    max_image_pixels: int = 60_000_000,
) -> BytesIO:
    if attachment.mime_type == "application/pdf":
        image = _pdf_thumbnail(attachment.path, max_dimension)
    else:
        image = _image_thumbnail(attachment.path, max_dimension, max_image_pixels)
    try:
        output = BytesIO()
        image.save(output, format="JPEG", quality=84, optimize=True)
        output.seek(0)
        return output
    finally:
        image.close()
