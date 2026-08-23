from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from werkzeug.datastructures import FileStorage

from . import AppError
from .db import transaction, utc_now
from .domain import normalized_attachment_name, sanitize_component, serialize_item


ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_MIME_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 60_000_000
DEFAULT_MAX_PDF_PAGES = 20
EXPORT_CLEANUP_REASON_PREFIX = "invoice-export-owned:"
EXPORT_OWNER_MARKER = ".invoice-export-owner"
_atomic_rename = os.rename
SAFE_CLEANUP_FAILURE_CODES = frozenset(
    {"cleanup_failed", "cleanup_ownership_changed", "unsafe_cleanup_path"}
)


def _native_long_path(path: Path) -> str:
    """Use Win32 extended-length syntax only at filesystem call boundaries."""
    value = str(path.resolve())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def export_owner_marker_text(token: str) -> str:
    return f"invoice-export-v1\n{token}\n"


def export_directory_owned(path: Path, token: str) -> bool:
    try:
        value = (path / EXPORT_OWNER_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return value == export_owner_marker_text(token)


def export_cleanup_quarantine_path(path: Path, token: str) -> Path:
    """Return the stable sibling used to resume an interrupted cleanup claim."""
    resolved = path.resolve()
    digest = hashlib.sha256(
        f"{resolved}\0{token}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:24]
    return resolved.parent / f".invoice-export-cleanup-{digest}"


def claim_owned_export_directory_for_cleanup(path: Path, token: str) -> Path | None:
    """Atomically detach an owned tree, then re-check ownership before deletion.

    The quarantine name is deterministic so a cleanup queue or startup recovery
    can resume after a process exits between the rename and the removal.
    """
    original = path.resolve()
    quarantine = export_cleanup_quarantine_path(original, token)
    if quarantine.exists():
        if not export_directory_owned(quarantine, token):
            raise AppError(
                "导出清理隔离目录的 operation ownership 已变化。",
                409,
                "cleanup_ownership_changed",
            )
        return quarantine
    if not original.exists():
        return None
    if not export_directory_owned(original, token):
        raise AppError(
            "导出清理目标的 operation ownership 已变化。",
            409,
            "cleanup_ownership_changed",
        )
    try:
        _atomic_rename(_native_long_path(original), _native_long_path(quarantine))
    except OSError:
        # A prior claimant may have completed the same deterministic rename
        # between the existence check and os.rename.
        if quarantine.exists() and export_directory_owned(quarantine, token):
            return quarantine
        raise
    if export_directory_owned(quarantine, token):
        return quarantine

    # The path was replaced after the first marker check. Move the unowned tree
    # back when possible; regardless of restore success it must never be deleted.
    if not original.exists():
        try:
            _atomic_rename(_native_long_path(quarantine), _native_long_path(original))
        except OSError:
            pass
    raise AppError(
        "导出清理目标在隔离期间发生变化。",
        409,
        "cleanup_ownership_changed",
    )


def restore_owned_export_directory_after_cleanup(
    quarantine: Path, original: Path, token: str
) -> bool:
    """Best-effort restore after a partial cleanup failure."""
    quarantine = quarantine.resolve()
    original = original.resolve()
    if (
        not quarantine.exists()
        or original.exists()
        or not export_directory_owned(quarantine, token)
    ):
        return False
    try:
        _atomic_rename(_native_long_path(quarantine), _native_long_path(original))
    except OSError:
        return False
    return export_directory_owned(original, token)


def remove_claimed_export_directory(quarantine: Path, token: str) -> None:
    """Remove a quarantined tree while retaining its marker until the end."""
    quarantine = quarantine.resolve()
    if not export_directory_owned(quarantine, token):
        raise AppError(
            "导出清理隔离目录的 operation ownership 已变化。",
            409,
            "cleanup_ownership_changed",
        )
    marker = quarantine / EXPORT_OWNER_MARKER
    for child in quarantine.iterdir():
        if child.name == EXPORT_OWNER_MARKER:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(_native_long_path(child))
        else:
            child.unlink()
    # A marker mismatch here means the quarantined path itself was replaced;
    # fail closed instead of deleting its root.
    if not export_directory_owned(quarantine, token):
        raise AppError(
            "导出清理隔离目录的 operation ownership 已变化。",
            409,
            "cleanup_ownership_changed",
        )
    marker.unlink()
    quarantine.rmdir()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validate_pdf_content(path: Path) -> None:
    maximum_pages = int(
        current_app.config.get("MAX_RECOGNITION_PDF_PAGES", DEFAULT_MAX_PDF_PAGES)
    )
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise AppError("不支持加密 PDF。", 415, "invalid_file_content")
            page_count = len(reader.pages)
            if page_count == 0:
                raise AppError("PDF 必须至少包含一页。", 415, "invalid_file_content")
            if page_count > maximum_pages:
                raise AppError(
                    f"PDF 不能超过 {maximum_pages} 页。",
                    413,
                    "pdf_too_many_pages",
                )
            # PdfReader resolves the page tree lazily. Touch every page and its
            # inherited media box while the source stream is open so malformed
            # page structures cannot pass validation based on the header alone.
            for page in reader.pages:
                _ = page.mediabox
    except AppError:
        raise
    except Exception as exc:
        raise AppError("文件扩展名为 PDF，但内容不是有效 PDF。", 415, "invalid_file_content") from exc


def _attach_safe_upload_error(
    exc: AppError,
    *,
    file_sha256: str,
    display_basename: str,
    mime_type: str,
) -> AppError:
    """Attach only path-free evidence after a fully read upload is removed."""
    exc.file_sha256 = file_sha256
    exc.display_basename = display_basename
    exc.mime_type = mime_type
    return exc


def import_uploaded_file(
    upload: FileStorage,
    *,
    destination_dir: str | Path | None = None,
    target_name: str | None = None,
) -> dict:
    original_name = Path(upload.filename or "未命名文件").name
    if len(original_name) > 255:
        raise AppError("文件名最多 255 个字符。", 400, "filename_too_long")
    extension = Path(original_name).suffix.lower()
    declared_mime = (upload.mimetype or "").lower()
    inferred_mime = (mimetypes.guess_type(original_name)[0] or "application/octet-stream").lower()
    mime_type = inferred_mime if declared_mime in {"", "application/octet-stream"} else declared_mime
    if extension not in ALLOWED_EXTENSIONS or mime_type not in ALLOWED_MIME_TYPES:
        raise AppError("仅支持 PDF、PNG、JPG/JPEG 或 WEBP 文件。", 415, "unsupported_file")
    blob_dir = (
        Path(destination_dir).resolve()
        if destination_dir is not None
        else Path(current_app.config["IMPORT_DIR"]) / "blobs" / datetime.now(timezone.utc).strftime("%Y-%m")
    )
    blob_dir.mkdir(parents=True, exist_ok=True)
    if target_name is not None:
        if Path(target_name).name != target_name or Path(target_name).suffix.lower() != extension:
            raise AppError("受管文件目标名称无效。", 409, "unsafe_path")
        target = blob_dir / target_name
    else:
        target = blob_dir / f"{uuid.uuid4().hex}{extension}"
    hasher = hashlib.sha256()
    size = 0
    maximum = int(current_app.config.get("MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE))
    try:
        with target.open("xb") as destination:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise AppError(
                        f"单个文件不能超过 {maximum // (1024 * 1024)} MB。",
                        413,
                        "file_too_large",
                    )
                hasher.update(chunk)
                destination.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise _attach_safe_upload_error(
            AppError("不能导入空文件。", 400, "empty_file"),
            file_sha256=hasher.hexdigest(),
            display_basename=original_name,
            mime_type=mime_type,
        )
    try:
        if extension == ".pdf":
            _validate_pdf_content(target)
        else:
            with Image.open(target) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > int(
                    current_app.config.get("MAX_IMAGE_PIXELS", DEFAULT_MAX_IMAGE_PIXELS)
                ):
                    raise AppError("图片尺寸无效或像素过大。", 413, "image_too_large")
                image.verify()
                actual_format = str(image.format or "").upper()
                allowed_formats = {"PNG"} if extension == ".png" else ({"WEBP"} if extension == ".webp" else {"JPEG"})
                if actual_format not in allowed_formats:
                    raise AppError("图片扩展名与实际内容不一致。", 415, "invalid_file_content")
    except AppError as exc:
        target.unlink(missing_ok=True)
        raise _attach_safe_upload_error(
            exc,
            file_sha256=hasher.hexdigest(),
            display_basename=original_name,
            mime_type=mime_type,
        )
    except Image.DecompressionBombError as exc:
        target.unlink(missing_ok=True)
        error = _attach_safe_upload_error(
            AppError("图片解压后的像素数量过大。", 413, "image_too_large"),
            file_sha256=hasher.hexdigest(),
            display_basename=original_name,
            mime_type=mime_type,
        )
        raise error from exc
    except (OSError, UnidentifiedImageError) as exc:
        target.unlink(missing_ok=True)
        error = _attach_safe_upload_error(
            AppError("无法读取上传图片，文件内容无效。", 415, "invalid_file_content"),
            file_sha256=hasher.hexdigest(),
            display_basename=original_name,
            mime_type=mime_type,
        )
        raise error from exc
    return {
        "original_name": original_name,
        "managed_path": str(target.resolve()),
        "sha256": hasher.hexdigest(),
        "mime_type": mime_type,
        "size_bytes": size,
    }


def bind_attachment_file(db, attachment_id: int, reason: str = "关联条目并规范命名") -> str:
    row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    if "name_locked" in row.keys() and row["name_locked"]:
        return row["normalized_name"]
    item = serialize_item(db, row["expense_item_id"])
    sequence_row = db.execute(
        "SELECT COUNT(*) AS n FROM attachments WHERE expense_item_id=? AND id<=?",
        (row["expense_item_id"], attachment_id),
    ).fetchone()
    normalized = normalized_attachment_name(item, row["category"], row["original_name"], sequence_row["n"])
    root = Path(current_app.config["IMPORT_DIR"]).resolve()
    source = Path(row["managed_path"]).resolve()
    if not _inside(source, root):
        raise AppError("附件路径超出受管目录。", 409, "unsafe_path")
    from .db import json_dump, json_load

    history = json_load(row["rename_history_json"], [])
    if row["normalized_name"] != normalized:
        history.append({"from": row["normalized_name"], "to": normalized, "reason": reason, "at": utc_now()})
    db.execute(
        "UPDATE attachments SET normalized_name=?, rename_history_json=?, updated_at=? WHERE id=?",
        (normalized, json_dump(history), utc_now(), attachment_id),
    )
    return normalized


def refresh_item_attachment_names(db, item_id: int):
    rows = db.execute("SELECT id FROM attachments WHERE expense_item_id=? ORDER BY page_order,id", (item_id,)).fetchall()
    for row in rows:
        bind_attachment_file(db, row["id"], "条目信息变更后更新规范名称")


def custom_rename_attachment(db, attachment_id: int, requested_name: str) -> str:
    row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    current = Path(row["managed_path"]).resolve()
    extension = current.suffix.lower()
    requested_stem = Path(str(requested_name or "")).stem
    safe_stem = sanitize_component(requested_stem, "报销材料", 160)
    normalized = safe_stem + extension
    root = Path(current_app.config["IMPORT_DIR"]).resolve()
    if not _inside(current, root):
        raise AppError("附件名称或路径不安全。", 400, "unsafe_path")
    from .db import json_dump, json_load

    conflict = db.execute(
        "SELECT 1 FROM attachments WHERE expense_item_id=? AND normalized_name=? AND id<>?",
        (row["expense_item_id"], normalized, attachment_id),
    ).fetchone()
    if conflict:
        raise AppError("该条目已有同名附件。", 409, "attachment_name_conflict")
    history = json_load(row["rename_history_json"], [])
    history.append({"from": row["normalized_name"], "to": normalized, "reason": "用户确认/修改规范名称", "at": utc_now()})
    db.execute(
        "UPDATE attachments SET normalized_name=?,rename_history_json=?,name_locked=1,updated_at=? WHERE id=?",
        (normalized, json_dump(history), utc_now(), attachment_id),
    )
    return normalized


def delete_managed_attachment(path_value: str):
    root = Path(current_app.config["IMPORT_DIR"]).resolve()
    path = Path(path_value).resolve()
    if not _inside(path, root):
        raise AppError("拒绝删除受管目录之外的文件。", 409, "unsafe_path")
    path.unlink(missing_ok=True)
    parent = path.parent
    while parent != root and _inside(parent, root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def enqueue_file_cleanup(db, path_value: str, kind: str, allowed_root: str | Path, reason: str) -> None:
    if kind not in {"managed_file", "archive_tree", "temporary_tree"}:
        raise ValueError(f"Unsupported cleanup kind: {kind}")
    db.execute(
        """INSERT INTO file_cleanup_queue(path,kind,allowed_root,reason,status,attempts,created_at)
           VALUES(?,?,?,?,'pending',0,?)
           ON CONFLICT(path,kind) DO UPDATE SET status='pending',last_error=NULL,reason=excluded.reason""",
        (str(Path(path_value).resolve()), kind, str(Path(allowed_root).resolve()), reason, utc_now()),
    )


def _cleanup_one(row) -> None:
    path = Path(row["path"]).resolve()
    root = Path(row["allowed_root"]).resolve()
    if not _inside(path, root) or path == root:
        raise AppError("清理目标超出允许目录。", 409, "unsafe_cleanup_path")
    reason = str(row["reason"] or "")
    cleanup_path = path
    claimed_path: Path | None = None
    owner_token: str | None = None
    if reason.startswith(EXPORT_CLEANUP_REASON_PREFIX):
        owner_token = reason.removeprefix(EXPORT_CLEANUP_REASON_PREFIX)
        claimed_path = claim_owned_export_directory_for_cleanup(path, owner_token)
        if claimed_path is None:
            return
        if not _inside(claimed_path, root) or claimed_path == root:
            restore_owned_export_directory_after_cleanup(claimed_path, path, owner_token)
            raise AppError(
                "导出清理隔离目录超出允许目录。",
                409,
                "unsafe_cleanup_path",
            )
        cleanup_path = claimed_path
    elif not path.exists():
        return
    try:
        if row["kind"] == "managed_file":
            trash_root = Path(current_app.config["TRASH_DIR"]).resolve()
            relative = path.relative_to(root)
            target = trash_root / "imports" / datetime.now(timezone.utc).strftime("%Y-%m-%d") / uuid.uuid4().hex / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cleanup_path), str(target))
            parent = path.parent
            while parent != root and _inside(parent, root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            return
        if row["kind"] == "archive_tree":
            trash_root = Path(current_app.config["TRASH_DIR"]).resolve()
            target = trash_root / "archives" / datetime.now(timezone.utc).strftime("%Y-%m-%d") / uuid.uuid4().hex / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(_native_long_path(cleanup_path), _native_long_path(target))
            return
        if claimed_path is not None and owner_token is not None:
            remove_claimed_export_directory(claimed_path, owner_token)
        elif cleanup_path.is_dir():
            shutil.rmtree(cleanup_path)
        else:
            cleanup_path.unlink()
    except Exception:
        if claimed_path is not None and owner_token is not None:
            restore_owned_export_directory_after_cleanup(claimed_path, path, owner_token)
        raise


def process_file_cleanup_queue(db=None, limit: int = 100) -> dict:
    from .db import get_db

    db = db or get_db()
    rows = db.execute(
        "SELECT * FROM file_cleanup_queue WHERE status='pending' ORDER BY created_at,id LIMIT ?",
        (limit,),
    ).fetchall()
    completed = 0
    failed = []
    for row in rows:
        try:
            _cleanup_one(row)
            with transaction(db):
                db.execute(
                    "UPDATE file_cleanup_queue SET status='completed',attempts=attempts+1,last_error=NULL,completed_at=? WHERE id=?",
                    (utc_now(), row["id"]),
                )
            completed += 1
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, AppError) and exc.code in SAFE_CLEANUP_FAILURE_CODES
                else "cleanup_failed"
            )
            with transaction(db):
                db.execute(
                    "UPDATE file_cleanup_queue SET attempts=attempts+1,last_error=? WHERE id=?",
                    (code, row["id"]),
                )
            failed.append({"id": row["id"], "code": code})
    return {"processed": len(rows), "completed": completed, "failed": failed}


def storage_reconciliation(db) -> dict:
    import_root = Path(current_app.config["IMPORT_DIR"]).resolve()
    referenced = {
        Path(row["managed_path"]).resolve()
        for row in db.execute("SELECT managed_path FROM attachments").fetchall()
    }
    actual = {
        path.resolve()
        for path in import_root.rglob("*")
        if path.is_file()
    } if import_root.exists() else set()
    missing = sorted(path for path in referenced if not path.is_file())
    orphaned = sorted(actual - referenced)
    pending = db.execute("SELECT COUNT(*) AS n FROM file_cleanup_queue WHERE status='pending'").fetchone()["n"]
    failed = db.execute(
        "SELECT COUNT(*) AS n FROM file_cleanup_queue WHERE status='pending' AND last_error IS NOT NULL"
    ).fetchone()["n"]
    return {
        "healthy": not missing and not orphaned and not pending,
        "missing_managed_files": [str(path) for path in missing],
        "orphaned_managed_files": [str(path) for path in orphaned],
        "pending_cleanup_count": pending,
        "failed_cleanup_count": failed,
    }


def purge_expired_trash(retention_days: int = 30) -> dict:
    """Purge only dated trash buckets older than the configured recovery window."""
    trash_root = Path(current_app.config["TRASH_DIR"]).resolve()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
    removed = 0
    failed: list[dict] = []
    for kind_root in (trash_root / "imports", trash_root / "archives"):
        if not kind_root.is_dir():
            continue
        for bucket in kind_root.iterdir():
            if not bucket.is_dir():
                continue
            try:
                bucket_date = datetime.strptime(bucket.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if bucket_date >= cutoff:
                continue
            try:
                shutil.rmtree(bucket)
                removed += 1
            except OSError:
                failed.append(
                    {"bucket_date": bucket.name, "code": "trash_cleanup_failed"}
                )
    return {"removed_buckets": removed, "failed": failed, "retention_days": max(1, retention_days)}


def ensure_archive_root(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise AppError("归档根目录必须是绝对路径。", 400, "invalid_archive_root")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise AppError(f"归档目录不可写：{exc}", 400, "archive_root_unwritable")
    return path.resolve()
