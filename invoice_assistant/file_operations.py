from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from werkzeug.datastructures import FileStorage

from . import AppError
from .batch_service import recalculate_batch_total
from .db import audit, json_dump, json_load, transaction, utc_now
from .domain import (
    CATEGORY_META,
    money_to_cents,
    normalize_untrusted_text,
    project_exists,
    serialize_item,
    validate_item_payload,
)
from .idempotency import (
    OperationReservation,
    ReplayedOperationFailure,
    complete_operation,
    fail_reserved_operation,
    operation_result,
    request_fingerprint,
    reserve_operation,
)
from .storage import bind_attachment_file, import_uploaded_file


IMPORT_TOOL = "import_invoice_file"
ATTACHMENT_TOOL = "add_invoice_attachment"
RECOVERY_WARNING = "recognition_result_unknown_manual_fallback"
FILE_OWNER_MARKER = ".invoice-agent-owner"


@dataclass(frozen=True)
class FileOperationOutcome:
    business_payload: dict | None
    safe_result: dict
    replayed: bool
    http_status: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extension(display_basename: str) -> str:
    suffix = Path(display_basename).suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
        raise AppError("文件扩展名无效。", 415, "unsupported_file")
    return suffix


def _staging_root(app) -> Path:
    return Path(app.config["DATA_DIR"]).resolve() / "operation-staging"


def _stage_id(operation_id: str, display_basename: str) -> str:
    return PurePosixPath(
        operation_id,
        f"incoming-{uuid.uuid4().hex}{_safe_extension(display_basename)}",
    ).as_posix()


def _stage_path(app, operation_id: str, managed_file_id: str) -> Path:
    relative = PurePosixPath(managed_file_id)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != operation_id
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise AppError("文件操作暂存标识无效。", 409, "unsafe_staging_id")
    root = _staging_root(app)
    result = root.joinpath(*relative.parts).resolve()
    try:
        result.relative_to(root)
    except ValueError:
        raise AppError("文件操作暂存标识无效。", 409, "unsafe_staging_id")
    return result


def _final_path(app, row) -> Path:
    extension = _safe_extension(row["display_basename"])
    root = Path(app.config["IMPORT_DIR"]).resolve()
    result = (root / "operations" / row["operation_id"] / f"source{extension}").resolve()
    try:
        result.relative_to(root)
    except ValueError:
        raise AppError("受管文件目标无效。", 409, "unsafe_path")
    return result


def _publishing_dir(app, row) -> Path:
    operations_root = (Path(app.config["IMPORT_DIR"]).resolve() / "operations").resolve()
    result = (operations_root / f".{row['operation_id']}.publishing").resolve()
    try:
        result.relative_to(operations_root)
    except ValueError:
        raise AppError("受管文件发布目录无效。", 409, "unsafe_path")
    return result


def _owner_marker_text(row) -> str:
    return f"invoice-agent-file-v1\n{row['operation_id']}\n{row['file_sha256']}\n"


def _owned_directory(path: Path, row) -> bool:
    marker = path / FILE_OWNER_MARKER
    try:
        value = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    return hmac.compare_digest(value, _owner_marker_text(row))


def _operation_row(db, operation_id: str):
    return db.execute(
        "SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()


def _reservation_from_row(row) -> OperationReservation:
    return OperationReservation(
        operation_id=row["operation_id"],
        operation_name=row["operation_name"],
        request_fingerprint=row["request_fingerprint"],
        status=row["status"],
        http_status=row["http_status"],
        operation_result=json_load(row["operation_result_json"], None),
        error_code=row["error_code"],
        error_outcome=row["error_outcome"],
    )


def _mark_failure(
    db,
    operation_id: str,
    code: str,
    *,
    cleanup_complete: bool,
    http_status: int = 409,
) -> None:
    now = utc_now()
    with transaction(db):
        if cleanup_complete:
            db.execute("DELETE FROM file_operation_staging WHERE operation_id=?", (operation_id,))
        else:
            db.execute(
                "UPDATE file_operation_staging SET phase='cleanup_pending',updated_at=? WHERE operation_id=?",
                (now, operation_id),
            )
        db.execute(
            """UPDATE agent_operations SET status='failed',operation_result_json=NULL,http_status=?,
                      error_code=?,error_outcome=?,updated_at=?,completed_at=? WHERE operation_id=?""",
            (int(http_status), code, "not_applied" if cleanup_complete else "unknown",
             now, now, operation_id),
        )


def _cleanup_paths(app, row) -> bool:
    try:
        stage = _stage_path(app, row["operation_id"], row["managed_file_id"])
        final_dir = _final_path(app, row).parent
        publishing_dir = _publishing_dir(app, row)
    except AppError:
        return False
    ok = True
    stage_existed = stage.is_file()
    try:
        stage.unlink(missing_ok=True)
    except OSError:
        ok = False
    try:
        stage.parent.rmdir()
    except OSError:
        pass
    # Never remove a pre-existing destination merely because its path matches
    # this operation. Only directories bearing the marker created by the
    # operation's exclusive claim are eligible for recursive cleanup.
    for directory in (publishing_dir, final_dir):
        if not directory.exists():
            continue
        if not _owned_directory(directory, row):
            # An unowned pre-existing final is provably external while the
            # authoritative stage still exists. Any publishing directory, or a
            # final observed after promotion consumed the stage, is uncertain
            # and must keep the operation outcome unknown.
            if directory == publishing_dir or not stage_existed:
                ok = False
            continue
        # Move the verified directory to an unpredictable sibling first, then
        # verify its marker again.  This closes the marker-read -> recursive
        # delete window: an external replacement at the original path is never
        # traversed or removed.
        quarantine = directory.with_name(
            f".{directory.name}.cleanup-{row['operation_id']}-{uuid.uuid4().hex}"
        )
        try:
            os.rename(directory, quarantine)
        except OSError:
            ok = False
            continue
        if not _owned_directory(quarantine, row):
            try:
                if not directory.exists():
                    os.rename(quarantine, directory)
            except OSError:
                pass
            ok = False
            continue
        try:
            shutil.rmtree(quarantine)
        except OSError:
            # Restore the operation-owned directory when possible so startup
            # recovery can retry it from the deterministic path.
            try:
                if not directory.exists():
                    os.rename(quarantine, directory)
            except OSError:
                pass
            ok = False
    return ok


def _fail_and_cleanup(db, app, row, code: str, *, http_status: int = 409) -> bool:
    cleanup_complete = _cleanup_paths(app, row)
    _mark_failure(
        db,
        row["operation_id"],
        code,
        cleanup_complete=cleanup_complete,
        http_status=http_status,
    )
    return cleanup_complete


def _sanitize_recognition(value: dict | None) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {
        "merchant": normalize_untrusted_text(source.get("merchant"))[:200],
        "expense_date": normalize_untrusted_text(source.get("expense_date"))[:10],
        "currency": normalize_untrusted_text(source.get("currency") or "CNY").upper()[:8],
        "purpose": normalize_untrusted_text(source.get("purpose"))[:2000],
        "document_type": source.get("document_type") if source.get("document_type") in CATEGORY_META else "unknown",
        "uncertainties": [],
    }
    for entry in (source.get("uncertainties") or [])[:100]:
        if isinstance(entry, str):
            text = normalize_untrusted_text(entry)[:1000]
        else:
            text = normalize_untrusted_text(
                json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            )[:1000]
        if text:
            result["uncertainties"].append(text)
    try:
        result["amount_cents"] = money_to_cents(source.get("amount", 0))
        converted = source.get("converted_amount")
        result["converted_amount_cents"] = (
            None if converted in (None, "") else money_to_cents(converted, "人民币实付金额")
        )
    except AppError:
        result["amount_cents"] = 0
        result["converted_amount_cents"] = None
        result["uncertainties"].append("识别金额格式异常，请手工核对。")
    return result


def _recognition_for_resource(row) -> dict:
    stored = json_load(row["recognition_result_json"], {})
    if not isinstance(stored, dict):
        stored = {}
    return {
        "merchant": stored.get("merchant", ""),
        "expense_date": stored.get("expense_date", ""),
        "amount": int(stored.get("amount_cents") or 0) / 100,
        "currency": stored.get("currency", "CNY"),
        "converted_amount": (
            None
            if stored.get("converted_amount_cents") is None
            else int(stored["converted_amount_cents"]) / 100
        ),
        "purpose": stored.get("purpose", ""),
        "document_type": stored.get("document_type", "unknown"),
        "uncertainties": list(stored.get("uncertainties") or []),
    }


def _default_project_id(db) -> int | None:
    row = db.execute("SELECT id FROM projects WHERE enabled=1 ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _insert_import_resource(db, row, managed_path: Path) -> tuple[dict, dict]:
    recognition = _recognition_for_resource(row)
    raw = {
        "merchant": recognition.get("merchant") or "",
        "expense_date": recognition.get("expense_date") or "",
        "amount": recognition.get("amount") or 0,
        "currency": recognition.get("currency") or "CNY",
        "converted_amount": recognition.get("converted_amount"),
        "purpose": recognition.get("purpose") or "",
        "project_id": _default_project_id(db),
    }
    try:
        values = validate_item_payload(raw)
    except AppError:
        raw.update({"amount": 0, "expense_date": "", "currency": "CNY"})
        values = validate_item_payload(raw)
        recognition["uncertainties"].append("识别字段格式异常，请手工核对。")
    now = utc_now()
    cursor = db.execute(
        """INSERT INTO expense_items(
               merchant,expense_date,amount,amount_cents,currency,converted_amount,converted_amount_cents,
               purpose,project_id,status,ai_raw_json,uncertainties_json,recognition_error,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,'pending_confirmation',?,?,?,?,?)""",
        (
            values["merchant"], values["expense_date"], values["amount"], values["amount_cents"],
            values["currency"], values["converted_amount"], values["converted_amount_cents"],
            values["purpose"], values["project_id"], json_dump(recognition),
            json_dump(recognition.get("uncertainties", [])),
            "识别结果不可用，请手工核对。" if row["recognition_error"] else None,
            now, now,
        ),
    )
    item_id = int(cursor.lastrowid)
    category = recognition.get("document_type", "unknown")
    if category not in CATEGORY_META:
        category = "unknown"
    attachment = db.execute(
        """INSERT INTO attachments(
               expense_item_id,category,original_name,normalized_name,managed_path,sha256,mime_type,
               size_bytes,ai_raw_json,recognition_error,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, category, row["display_basename"], row["display_basename"], str(managed_path),
            row["file_sha256"], row["mime_type"], managed_path.stat().st_size, json_dump(recognition),
            "识别结果不可用，请手工核对。" if row["recognition_error"] else None, now, now,
        ),
    )
    bind_attachment_file(db, int(attachment.lastrowid))
    audit(
        db,
        "item",
        item_id,
        "recognized" if not row["recognition_error"] else "recognition_failed_manual_fallback",
        {"attachment_id": int(attachment.lastrowid)},
    )
    item = serialize_item(db, item_id)
    warnings = [RECOVERY_WARNING if row["recognition_error"] == RECOVERY_WARNING else "recognition_failed_manual_fallback"] if row["recognition_error"] else []
    safe = operation_result(
        [{"type": "invoice_item", "id": item_id, "version": item["version"]}],
        warning_codes=warnings,
    )
    return {"item": item, "imported": True}, safe


def _payment_cents(recognition: dict) -> int | None:
    for key in ("converted_amount", "rmb_amount", "paid_amount", "amount"):
        value = recognition.get(key)
        if value not in (None, ""):
            try:
                cents = money_to_cents(value, "人民币实付金额")
            except AppError:
                continue
            if cents > 0:
                return cents
    return None


def _refresh_payment_amount(db, item_id: int) -> None:
    item = db.execute("SELECT currency,uncertainties_json FROM expense_items WHERE id=?", (item_id,)).fetchone()
    if not item or item["currency"] == "CNY":
        return
    amounts = set()
    for attachment in db.execute(
        "SELECT ai_raw_json FROM attachments WHERE expense_item_id=? AND category='payment_record'",
        (item_id,),
    ).fetchall():
        value = _payment_cents(json_load(attachment["ai_raw_json"], {}))
        if value:
            amounts.add(value)
    uncertainties = [
        entry
        for entry in json_load(item["uncertainties_json"], [])
        if entry != "支付记录识别到多个不同的人民币实付金额，请手工核对。"
    ]
    if len(amounts) == 1:
        cents = next(iter(amounts))
        db.execute(
            "UPDATE expense_items SET converted_amount=?,converted_amount_cents=?,uncertainties_json=?,updated_at=? WHERE id=?",
            (cents / 100, cents, json_dump(uncertainties), utc_now(), item_id),
        )
    elif len(amounts) > 1:
        uncertainties.append("支付记录识别到多个不同的人民币实付金额，请手工核对。")
        db.execute(
            "UPDATE expense_items SET converted_amount=NULL,converted_amount_cents=NULL,uncertainties_json=?,updated_at=? WHERE id=?",
            (json_dump(uncertainties), utc_now(), item_id),
        )


def _attachment_context(db, row):
    item = db.execute(
        """SELECT i.id,i.status,i.row_version,bi.batch_id,b.row_version AS batch_version,b.export_token
           FROM expense_items i
           LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
           LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
           WHERE i.id=?""",
        (row["target_item_id"],),
    ).fetchone()
    if not item:
        raise AppError("条目不存在。", 404, "item_not_found")
    if item["status"] not in {"pending_confirmation", "pending_reimbursement", "in_batch"}:
        raise AppError("该条目已锁定，不能增添附件。", 409, "item_locked")
    if int(item["row_version"]) != int(row["expected_item_version"]):
        raise AppError("条目已被其他操作修改，请刷新后重试。", 409, "stale_version")
    if item["batch_id"] is not None:
        if row["expected_batch_version"] is None:
            raise AppError("批次内条目写入必须提供 expected_batch_version。", 400, "version_required")
        if int(item["batch_version"]) != int(row["expected_batch_version"]):
            raise AppError("报销包已被其他操作修改，请刷新后重试。", 409, "stale_version")
        if item["export_token"]:
            raise AppError("报销包正在生成归档，请完成后再修改条目。", 409, "batch_exporting")
    elif row["expected_batch_version"] is not None:
        raise AppError("条目当前不属于报销包。", 409, "stale_version")
    return item


def _insert_attachment_resource(db, row, managed_path: Path) -> tuple[dict, dict]:
    context = _attachment_context(db, row)
    recognition = _recognition_for_resource(row)
    now = utc_now()
    cursor = db.execute(
        """INSERT INTO attachments(
               expense_item_id,category,original_name,normalized_name,managed_path,sha256,mime_type,
               size_bytes,ai_raw_json,recognition_error,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["target_item_id"], row["attachment_kind"], row["display_basename"],
            row["display_basename"], str(managed_path), row["file_sha256"], row["mime_type"],
            managed_path.stat().st_size,
            json_dump(recognition) if row["attachment_kind"] == "payment_record" else None,
            "识别结果不可用，请手工核对。" if row["recognition_error"] else None,
            now, now,
        ),
    )
    attachment_id = int(cursor.lastrowid)
    bind_attachment_file(db, attachment_id)
    if row["attachment_kind"] == "payment_record":
        _refresh_payment_amount(db, int(row["target_item_id"]))
    changed = db.execute(
        """UPDATE expense_items SET row_version=row_version+1,updated_at=?
           WHERE id=? AND row_version=? RETURNING row_version""",
        (now, row["target_item_id"], row["expected_item_version"]),
    ).fetchone()
    if not changed:
        raise AppError("条目已被其他操作修改，请刷新后重试。", 409, "stale_version")
    refs = [
        {"type": "invoice_item", "id": int(row["target_item_id"]), "version": int(changed["row_version"])}
    ]
    if context["batch_id"] is not None:
        batch_version = recalculate_batch_total(db, int(context["batch_id"]))
        refs.append(
            {"type": "reimbursement_batch", "id": int(context["batch_id"]), "version": batch_version}
        )
    audit(
        db,
        "item",
        int(row["target_item_id"]),
        "attachment_added",
        {"attachment_id": attachment_id, "category": row["attachment_kind"]},
    )
    warnings = []
    if row["recognition_error"]:
        warnings.append(
            RECOVERY_WARNING if row["recognition_error"] == RECOVERY_WARNING else "payment_recognition_failed"
        )
    safe = operation_result(refs, warning_codes=warnings)
    return {"item": serialize_item(db, int(row["target_item_id"]))}, safe


def _promote(app, row) -> Path:
    stage = _stage_path(app, row["operation_id"], row["managed_file_id"])
    final = _final_path(app, row)
    final_dir = final.parent
    publishing_dir = _publishing_dir(app, row)
    if final_dir.exists():
        if not _owned_directory(final_dir, row):
            raise AppError("受管文件目标已存在，拒绝覆盖。", 409, "operation_destination_exists")
        if not final.is_file() or not hmac.compare_digest(
            _sha256_file(final), row["file_sha256"]
        ):
            raise AppError("已发布受管文件校验失败。", 409, "staged_file_changed")
        stage.unlink(missing_ok=True)
        return final
    if not stage.is_file() or not hmac.compare_digest(_sha256_file(stage), row["file_sha256"]):
        raise AppError("暂存文件缺失或校验失败。", 409, "staged_file_changed")
    operations_root = final_dir.parent
    operations_root.mkdir(parents=True, exist_ok=True)
    if publishing_dir.exists():
        if not _owned_directory(publishing_dir, row):
            raise AppError("文件发布暂存目标已存在。", 409, "operation_destination_exists")
    else:
        try:
            publishing_dir.mkdir()
            marker = publishing_dir / FILE_OWNER_MARKER
            with marker.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_owner_marker_text(row))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise AppError("文件发布暂存目标已存在。", 409, "operation_destination_exists")
        except OSError as exc:
            raise AppError("无法建立受管文件发布目录。", 500, "file_publish_failed") from exc
    publish_file = publishing_dir / final.name
    if publish_file.exists() and not hmac.compare_digest(
        _sha256_file(publish_file), row["file_sha256"]
    ):
        # The marker proves this partial file belongs to the interrupted
        # operation, so it is safe to replace from the authoritative stage.
        publish_file.unlink()
    if not publish_file.exists():
        try:
            with stage.open("rb") as source, publish_file.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            raise AppError("无法发布受管文件。", 500, "file_publish_failed") from exc
    if not hmac.compare_digest(_sha256_file(publish_file), row["file_sha256"]):
        raise AppError("受管文件校验失败。", 409, "staged_file_changed")
    if final_dir.exists():
        raise AppError("受管文件目标已存在，拒绝覆盖。", 409, "operation_destination_exists")
    try:
        os.rename(publishing_dir, final_dir)
    except OSError as exc:
        if final_dir.exists():
            raise AppError("受管文件目标已存在，拒绝覆盖。", 409, "operation_destination_exists") from exc
        raise AppError("无法原子发布受管文件。", 500, "file_publish_failed") from exc
    if not _owned_directory(final_dir, row) or not final.is_file() or not hmac.compare_digest(
        _sha256_file(final), row["file_sha256"]
    ):
        raise AppError("受管文件发布校验失败。", 409, "staged_file_changed")
    stage.unlink(missing_ok=True)
    return final


def _complete_applied_resource(db, row) -> dict:
    if row["resource_type"] != "invoice_item" or row["resource_id"] is None:
        raise AppError("已应用资源的恢复标识无效。", 409, "operation_state_changed")
    operation = _operation_row(db, row["operation_id"])
    safe = json_load(operation["operation_result_json"], None) if operation else None
    refs = safe.get("resource_refs") if isinstance(safe, dict) else None
    if not isinstance(refs, list) or not refs:
        raise AppError("已应用资源的安全响应快照缺失。", 409, "operation_state_changed")
    primary = refs[0]
    if (
        not isinstance(primary, dict)
        or primary.get("type") != row["resource_type"]
        or primary.get("id") != int(row["resource_id"])
        or row["resource_version"] is None
        or primary.get("version") != int(row["resource_version"])
    ):
        raise AppError("已应用资源的安全响应快照无效。", 409, "operation_state_changed")
    return safe


def _apply_resource(db, app, operation_id: str) -> tuple[dict, dict]:
    """Commit the business resource as a real, recoverable phase."""
    row = db.execute(
        "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if not row or row["phase"] != "recognition_complete":
        raise AppError("文件操作尚未准备应用资源。", 409, "operation_state_changed")
    managed_path = _promote(app, row)
    with transaction(db):
        current = db.execute(
            "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if not current or current["phase"] != "recognition_complete":
            raise AppError("文件操作状态已变化。", 409, "operation_state_changed")
        if current["tool_name"] == IMPORT_TOOL:
            business, safe = _insert_import_resource(db, current, managed_path)
        elif current["tool_name"] == ATTACHMENT_TOOL:
            business, safe = _insert_attachment_resource(db, current, managed_path)
        else:
            raise AppError("文件操作类型无效。", 409, "operation_state_changed")
        primary = safe["resource_refs"][0]
        operation_changed = db.execute(
            """UPDATE agent_operations SET operation_result_json=?,updated_at=?
               WHERE operation_id=? AND status='in_progress'""",
            (json_dump(safe), utc_now(), operation_id),
        )
        if operation_changed.rowcount != 1:
            raise AppError("操作状态发生变化。", 409, "operation_state_changed")
        changed = db.execute(
            """UPDATE file_operation_staging
               SET phase='resource_applied',resource_type=?,resource_id=?,resource_version=?,updated_at=?
               WHERE operation_id=? AND phase='recognition_complete'""",
            (
                primary["type"],
                primary["id"],
                primary.get("version"),
                utc_now(),
                operation_id,
            ),
        )
        if changed.rowcount != 1:
            raise AppError("文件操作状态已变化。", 409, "operation_state_changed")
    return business, safe


def _finalize(db, app, operation_id: str) -> FileOperationOutcome:
    row = db.execute(
        "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if not row:
        operation = _operation_row(db, operation_id)
        if operation and operation["status"] == "succeeded":
            return FileOperationOutcome(
                None, json_load(operation["operation_result_json"], {}), True,
                int(operation["http_status"] or 200),
            )
        raise AppError("文件操作恢复状态不存在。", 409, "operation_state_changed")
    if row["phase"] not in {"recognition_complete", "resource_applied"}:
        raise AppError("文件操作尚未准备提交。", 409, "operation_in_progress")
    business = None
    if row["phase"] == "recognition_complete":
        business, _safe_snapshot = _apply_resource(db, app, operation_id)
    try:
        with transaction(db):
            current = db.execute(
                "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not current or current["phase"] != "resource_applied":
                raise AppError("文件操作状态已变化。", 409, "operation_state_changed")
            reservation = _reservation_from_row(_operation_row(db, operation_id))
            safe = _complete_applied_resource(db, current)
            status = 201
            complete_operation(db, reservation, safe, http_status=status)
            db.execute("DELETE FROM file_operation_staging WHERE operation_id=?", (operation_id,))
        return FileOperationOutcome(business, safe, False, status)
    except Exception:
        # The database transaction either committed the resource and operation
        # together, or rolled both back.  Only the latter leaves the staging row.
        if db.execute(
            "SELECT 1 FROM file_operation_staging WHERE operation_id=?", (operation_id,)
        ).fetchone():
            raise
        operation = _operation_row(db, operation_id)
        return FileOperationOutcome(
            None, json_load(operation["operation_result_json"], {}), True,
            int(operation["http_status"] or 201),
        )


def _delete_incoming_staged_file(app, path_value: str) -> None:
    root = _staging_root(app)
    path = Path(path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise AppError("拒绝清理暂存目录之外的文件。", 409, "unsafe_staging_id")
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _reserve_and_stage(db, app, operation_id: str, tool_name: str, parameters: dict, upload: FileStorage):
    display_basename = Path(upload.filename or "未命名文件").name
    managed_file_id = _stage_id(operation_id, display_basename)
    incoming_path = _stage_path(app, operation_id, managed_file_id)
    try:
        imported = import_uploaded_file(
            upload,
            destination_dir=incoming_path.parent,
            target_name=incoming_path.name,
        )
    except AppError as exc:
        # Once the complete upload has been hashed, content validation failures
        # have a stable request identity and are safe to persist.  Earlier
        # transport/size failures deliberately remain outside the ledger.
        file_sha256 = getattr(exc, "file_sha256", None)
        error_basename = getattr(exc, "display_basename", None)
        if isinstance(file_sha256, str) and isinstance(error_basename, str):
            failed_fingerprint = request_fingerprint(
                tool_name,
                {
                    **parameters,
                    "file_sha256": file_sha256,
                    "display_basename": error_basename,
                },
            )
            with transaction(db):
                failed_reservation = reserve_operation(
                    db, operation_id, tool_name, failed_fingerprint
                )
                if failed_reservation.replayed:
                    return failed_reservation, None, True
                fail_reserved_operation(
                    db,
                    failed_reservation,
                    http_status=exc.status_code,
                    error_code=exc.code,
                    outcome="not_applied",
                )
        raise
    fingerprint = request_fingerprint(
        tool_name,
        {
            **parameters,
            "file_sha256": imported["sha256"],
            "display_basename": imported["original_name"],
        },
    )
    reservation = None
    stage_row = None
    existing = None
    replayed = False
    try:
        with transaction(db):
            existing = _operation_row(db, operation_id)
            if existing:
                if (
                    existing["operation_name"] != tool_name
                    or not hmac.compare_digest(existing["request_fingerprint"], fingerprint)
                ):
                    raise AppError("同一 operation ID 已用于不同操作、参数或文件。", 409, "idempotency_mismatch")
                reservation = _reservation_from_row(existing)
                if existing["status"] == "succeeded":
                    replayed = True
                elif existing["status"] == "failed":
                    raise ReplayedOperationFailure(reservation)
                else:
                    stage_row = db.execute(
                        "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
                    ).fetchone()
                    if not stage_row or stage_row["phase"] != "resume_ready":
                        raise AppError("该操作仍在执行，请稍后查询原 operation ID。", 409, "operation_in_progress")
            else:
                reservation = reserve_operation(db, operation_id, tool_name, fingerprint)
                now = utc_now()
                db.execute(
                    """INSERT INTO file_operation_staging(
                           operation_id,tool_name,phase,file_sha256,mime_type,display_basename,
                           attachment_kind,target_item_id,expected_item_version,expected_batch_version,
                           managed_file_id,recognition_result_json,recognition_error,resource_type,
                           resource_id,resource_version,created_at,updated_at
                       ) VALUES(?,?,'staged',?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?)""",
                    (
                        operation_id, tool_name, imported["sha256"], imported["mime_type"],
                        imported["original_name"], parameters.get("category") or "invoice_import",
                        parameters.get("item_id"), parameters.get("expected_version"),
                        parameters.get("expected_batch_version"), managed_file_id, now, now,
                    ),
                )
                stage_row = db.execute(
                    "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
                ).fetchone()
        if existing:
            _delete_incoming_staged_file(app, imported["managed_path"])
        return reservation, stage_row, replayed
    except Exception:
        if imported and Path(imported["managed_path"]).exists():
            _delete_incoming_staged_file(app, imported["managed_path"])
        raise


def _recognize_then_finalize(
    db,
    app,
    operation_id: str,
    *,
    recognize,
    explain_failure,
    should_recognize: bool,
) -> FileOperationOutcome:
    row = db.execute(
        "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if not row:
        raise AppError("文件操作恢复状态不存在。", 409, "operation_state_changed")
    stage = _stage_path(app, operation_id, row["managed_file_id"])
    if not stage.is_file() or not hmac.compare_digest(_sha256_file(stage), row["file_sha256"]):
        raise AppError("暂存文件缺失或校验失败。", 409, "staged_file_changed")
    recognition = {}
    recognition_error = None
    if should_recognize:
        with transaction(db):
            current = db.execute(
                "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not current:
                raise AppError("文件操作恢复状态不存在。", 409, "operation_state_changed")
            # The version check and durable dispatch marker share one write
            # transaction. A version bump during hashing therefore prevents any
            # external dispatch instead of being discovered only at final apply.
            if current["tool_name"] == ATTACHMENT_TOOL:
                _attachment_context(db, current)
            changed = db.execute(
                """UPDATE file_operation_staging SET phase='recognition_started',updated_at=?
                   WHERE operation_id=? AND phase IN ('staged','resume_ready')""",
                (utc_now(), operation_id),
            )
            if changed.rowcount != 1:
                raise AppError("该操作仍在执行。", 409, "operation_in_progress")
        try:
            recognition = _sanitize_recognition(
                recognize(str(stage), row["mime_type"], row["display_basename"])
            )
        except Exception as exc:
            # Explain the error to preserve current product semantics, but only
            # persist a fixed code so staging never becomes a free-form log.
            explain_failure(exc)
            recognition = _sanitize_recognition(None)
            recognition_error = "recognition_failed"
    else:
        recognition = _sanitize_recognition(None)
    with transaction(db):
        changed = db.execute(
            """UPDATE file_operation_staging
               SET phase='recognition_complete',recognition_result_json=?,recognition_error=?,updated_at=?
               WHERE operation_id=? AND phase IN ('staged','resume_ready','recognition_started')""",
            (json_dump(recognition), recognition_error, utc_now(), operation_id),
        )
        if changed.rowcount != 1:
            raise AppError("文件操作状态已变化。", 409, "operation_state_changed")
    return _finalize(db, app, operation_id)


def run_agent_import(
    db,
    app,
    operation_id: str,
    upload: FileStorage,
    *,
    notice_version: str,
    acknowledged: bool,
    recognize,
    explain_failure,
) -> FileOperationOutcome:
    parameters = {
        "external_processing_notice_version": notice_version,
        "external_processing_ack": acknowledged,
    }
    reservation, row, replayed = _reserve_and_stage(
        db, app, operation_id, IMPORT_TOOL, parameters, upload
    )
    if replayed:
        return FileOperationOutcome(
            None, reservation.operation_result or {}, True, int(reservation.http_status or 201)
        )
    try:
        return _recognize_then_finalize(
            db, app, operation_id, recognize=recognize, explain_failure=explain_failure,
            should_recognize=True,
        )
    except AppError as exc:
        current = db.execute(
            "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if current and current["phase"] == "resource_applied":
            # The resource transaction is already durable. Never delete its
            # managed file or relabel the operation as safely not applied.
            exc.outcome = "unknown"
        elif current and current["phase"] != "recognition_started":
            if not _fail_and_cleanup(
                db, app, current, exc.code, http_status=exc.status_code
            ):
                exc.outcome = "unknown"
        raise


def run_agent_attachment(
    db,
    app,
    operation_id: str,
    upload: FileStorage,
    *,
    item_id: int,
    expected_version: int,
    expected_batch_version: int | None,
    category: str,
    notice_version: str | None,
    acknowledged: bool | None,
    recognize,
    explain_failure,
) -> FileOperationOutcome:
    parameters = {
        "item_id": item_id,
        "expected_version": expected_version,
        "expected_batch_version": expected_batch_version,
        "category": category,
        "external_processing_notice_version": notice_version,
        "external_processing_ack": acknowledged,
    }
    reservation, row, replayed = _reserve_and_stage(
        db, app, operation_id, ATTACHMENT_TOOL, parameters, upload
    )
    if replayed:
        return FileOperationOutcome(
            None, reservation.operation_result or {}, True, int(reservation.http_status or 201)
        )
    try:
        return _recognize_then_finalize(
            db, app, operation_id, recognize=recognize, explain_failure=explain_failure,
            should_recognize=category == "payment_record",
        )
    except AppError as exc:
        current = db.execute(
            "SELECT * FROM file_operation_staging WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if current and current["phase"] == "resource_applied":
            # The attachment is authoritative once this phase commits.
            exc.outcome = "unknown"
        elif current and current["phase"] != "recognition_started":
            if not _fail_and_cleanup(
                db, app, current, exc.code, http_status=exc.status_code
            ):
                exc.outcome = "unknown"
        raise


def _cleanup_orphan_staging_files(db, app) -> int:
    root = _staging_root(app)
    if not root.is_dir():
        return 0
    referenced = set()
    for row in db.execute(
        "SELECT operation_id,managed_file_id FROM file_operation_staging"
    ).fetchall():
        try:
            referenced.add(_stage_path(app, row["operation_id"], row["managed_file_id"]))
        except AppError:
            continue
    cleaned = 0
    for operation_dir in list(root.iterdir()):
        if operation_dir.is_symlink() or not operation_dir.is_dir():
            continue
        try:
            parsed = uuid.UUID(operation_dir.name)
        except ValueError:
            continue
        if parsed.version != 4 or str(parsed) != operation_dir.name:
            continue
        for candidate in list(operation_dir.iterdir()):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if (
                candidate in referenced
                or not candidate.name.startswith("incoming-")
                or candidate.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
            ):
                continue
            try:
                uuid.UUID(candidate.stem.removeprefix("incoming-"))
                candidate.unlink()
                cleaned += 1
            except (ValueError, OSError):
                continue
        try:
            operation_dir.rmdir()
        except OSError:
            pass
    return cleaned


def recover_incomplete_file_operations(app) -> dict:
    """Recover staged writes without ever redispatching an acknowledged recognition."""
    from .db import get_db

    db = get_db()
    recovered = 0
    resumable = 0
    failed = 0
    orphaned_cleaned = _cleanup_orphan_staging_files(db, app)
    rows = db.execute(
        "SELECT * FROM file_operation_staging ORDER BY created_at,operation_id"
    ).fetchall()
    for original in rows:
        row = db.execute(
            "SELECT * FROM file_operation_staging WHERE operation_id=?", (original["operation_id"],)
        ).fetchone()
        if not row:
            continue
        try:
            stage = _stage_path(app, row["operation_id"], row["managed_file_id"])
            final = _final_path(app, row)
            phase = row["phase"]
            if phase in {"awaiting_stage", "staged", "resume_ready"}:
                if stage.is_file() and hmac.compare_digest(_sha256_file(stage), row["file_sha256"]):
                    with transaction(db):
                        db.execute(
                            "UPDATE file_operation_staging SET phase='resume_ready',updated_at=? WHERE operation_id=?",
                            (utc_now(), row["operation_id"]),
                        )
                    resumable += 1
                else:
                    _fail_and_cleanup(db, app, row, "staged_file_changed")
                    failed += 1
                continue
            if phase == "recognition_started":
                # The remote outcome is unknowable.  Persist a fixed fallback and
                # create the local manual resource, never a second dispatch.
                with transaction(db):
                    db.execute(
                        """UPDATE file_operation_staging SET phase='recognition_complete',
                                  recognition_result_json=?,recognition_error=?,updated_at=?
                           WHERE operation_id=? AND phase='recognition_started'""",
                        (json_dump(_sanitize_recognition(None)), RECOVERY_WARNING, utc_now(), row["operation_id"]),
                    )
                _finalize(db, app, row["operation_id"])
                recovered += 1
                continue
            if phase in {"recognition_complete", "resource_applied"}:
                # Once resource_applied commits, the database association and
                # safe result snapshot are authoritative. Completion must not
                # get stuck because a later external event changed the item or
                # damaged the managed file.
                if phase == "recognition_complete" and not stage.is_file() and not final.is_file():
                    raise AppError("暂存文件缺失。", 409, "staged_file_changed")
                _finalize(db, app, row["operation_id"])
                recovered += 1
                continue
            if phase == "cleanup_pending":
                complete = _cleanup_paths(app, row)
                operation = _operation_row(db, row["operation_id"])
                _mark_failure(
                    db,
                    row["operation_id"],
                    (operation["error_code"] if operation else None) or "operation_failed",
                    cleanup_complete=complete,
                    http_status=int(operation["http_status"] or 409) if operation else 409,
                )
                failed += int(complete)
                continue
            raise AppError("未知文件操作阶段。", 409, "operation_state_changed")
        except Exception as exc:
            row = db.execute(
                "SELECT * FROM file_operation_staging WHERE operation_id=?", (original["operation_id"],)
            ).fetchone()
            if row and row["phase"] != "resource_applied":
                code = exc.code if isinstance(exc, AppError) else "file_operation_recovery_failed"
                _fail_and_cleanup(
                    db,
                    app,
                    row,
                    code,
                    http_status=exc.status_code if isinstance(exc, AppError) else 500,
                )
            failed += 1
    return {
        "recovered": recovered,
        "resumable": resumable,
        "failed": failed,
        "orphaned_cleaned": orphaned_cleaned,
    }
