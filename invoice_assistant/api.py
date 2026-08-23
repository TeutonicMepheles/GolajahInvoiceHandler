from __future__ import annotations

import os
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from . import AppError
from .batch_service import export_batch, recalculate_batch_total, serialize_batch
from .db import audit, get_db, json_dump, transaction, utc_now
from .domain import (
    CATEGORY_META,
    MATERIAL_META,
    find_duplicate_candidates,
    get_materials,
    get_requirements_version,
    get_rules,
    material_status,
    money_to_cents,
    payment_rmb_cents,
    project_exists,
    refresh_foreign_payment_amount,
    reimbursement_cents,
    serialize_item,
    validate_item_payload,
)
from .idempotency import (
    complete_operation,
    fail_reserved_operation,
    operation_result,
    request_fingerprint,
    reserve_operation,
    validate_operation_id,
)
from .pagination import KeysetCursor, decode_cursor, encode_cursor, filter_hash, parse_limit
from .review import (
    build_review,
    consume_overflow_token,
    create_review_session,
    next_review_session_page,
    public_review,
    require_current_review,
    require_merge_target,
    validate_confirmation,
)
from .http import json_body as body
from .features.recognition import explain_recognition_failure, recognition_status, recognize_file
from .file_operations import run_agent_attachment, run_agent_import
from .persistence import create_database_backup, storage_status
from .storage import (
    bind_attachment_file,
    custom_rename_attachment,
    delete_managed_attachment,
    enqueue_file_cleanup,
    ensure_archive_root,
    import_uploaded_file,
    process_file_cleanup_queue,
    refresh_item_attachment_names,
    storage_reconciliation,
)


api = Blueprint("api", __name__)
MAX_IMPORT_FILES = 20


def _operation_id(payload: dict | None = None) -> str | None:
    if payload is not None and "operation_id" in payload:
        raise AppError("operation_id 只能通过 Idempotency-Key header 提供。", 400, "operation_id_conflict")
    raw = request.environ.get("HTTP_IDEMPOTENCY_KEY")
    if raw and "," in raw:
        raise AppError("Idempotency-Key 只能提供一次。", 400, "duplicate_operation_id")
    return validate_operation_id(raw, required=False)


def _integer_field(payload: dict, name: str, *, required_code: str = "version_required") -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppError(f"必须提供有效的 {name}。", 400, required_code)
    return value


def _form_integer(name: str, *, required: bool = True) -> int | None:
    value = request.form.get(name)
    if value in (None, "") and not required:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AppError(f"必须提供有效的 {name}。", 400, "version_required")
    if parsed < 0 or str(parsed) != str(value).strip():
        raise AppError(f"必须提供有效的 {name}。", 400, "version_required")
    return parsed


def _agent_file_request(expected_tool: str) -> str:
    if "operation_id" in request.form:
        raise AppError("operation_id 只能通过 Idempotency-Key header 提供。", 400, "operation_id_conflict")
    operation_id = _operation_id()
    if not operation_id:
        raise AppError("Agent 写操作必须提供 Idempotency-Key。", 400, "operation_id_required")
    if request.headers.get("X-Invoice-Agent-Tool") != expected_tool:
        raise AppError("Agent tool header 与目标写操作不匹配。", 400, "agent_tool_mismatch")
    return operation_id


def _single_form_value(name: str, *, required: bool = False) -> str | None:
    values = request.form.getlist(name)
    if len(values) > 1:
        raise AppError(f"{name} 只能提供一次。", 400, "duplicate_field")
    if not values:
        if required:
            raise AppError(f"必须提供 {name}。", 400, "field_required")
        return None
    return values[0]


def _agent_form_integer(name: str, *, required: bool = True) -> int | None:
    value = _single_form_value(name, required=required)
    if value in (None, "") and not required:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AppError(f"必须提供有效的 {name}。", 400, "version_required")
    if parsed < 0 or str(parsed) != str(value).strip():
        raise AppError(f"必须提供有效的 {name}。", 400, "version_required")
    return parsed


def _require_external_processing_ack() -> tuple[str, bool]:
    notice = _single_form_value("external_processing_notice_version")
    acknowledgement = _single_form_value("external_processing_ack")
    if notice != "deepseek-v1" or acknowledgement != "true":
        raise AppError(
            "必须确认当前 DeepSeek 文件处理披露后才能继续。",
            400,
            "external_processing_ack_required",
        )
    return notice, True


def _single_agent_upload():
    uploads = request.files.getlist("file")
    if len(uploads) != 1 or set(request.files) != {"file"}:
        raise AppError("Agent 文件工具每次必须且只能提供一个 file。", 400, "file_required")
    return uploads[0]


def _item_context(db, item_id: int):
    return db.execute(
        """SELECT i.id,i.status,i.row_version,bi.batch_id,b.row_version AS batch_version,b.export_token
           FROM expense_items i
           LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
           LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
           WHERE i.id=?""",
        (item_id,),
    ).fetchone()


def _require_item_version(
    db,
    item_id: int,
    expected_version: int,
    expected_batch_version: int | None = None,
):
    row = _item_context(db, item_id)
    if not row:
        raise AppError("条目不存在。", 404, "item_not_found")
    if int(row["row_version"]) != expected_version:
        raise AppError("条目已被其他操作修改，请刷新后重试。", 409, "stale_version")
    if row["batch_id"] is not None:
        if expected_batch_version is None:
            raise AppError("批次内条目写入必须提供 expected_batch_version。", 400, "version_required")
        if int(row["batch_version"]) != expected_batch_version:
            raise AppError("报销包已被其他操作修改，请刷新后重试。", 409, "stale_version")
        if row["export_token"]:
            raise AppError("报销包正在生成归档，请完成后再修改条目。", 409, "batch_exporting")
    elif expected_batch_version is not None:
        raise AppError("条目当前不属于报销包。", 409, "stale_version")
    return row


def _require_batch_version(db, batch_id: int, expected_version: int):
    row = db.execute("SELECT * FROM reimbursement_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise AppError("报销包不存在。", 404, "batch_not_found")
    if int(row["row_version"]) != expected_version:
        raise AppError("报销包已被其他操作修改，请刷新后重试。", 409, "stale_version")
    if row["export_token"]:
        raise AppError("报销包正在生成归档，请完成后再修改。", 409, "batch_exporting")
    return row


def _touch_item(db, item_id: int) -> int:
    row = db.execute(
        "UPDATE expense_items SET row_version=row_version+1,updated_at=? WHERE id=? RETURNING row_version",
        (utc_now(), item_id),
    ).fetchone()
    if not row:
        raise AppError("条目不存在。", 404, "item_not_found")
    return int(row["row_version"])


def _database_operation(db, operation_name: str, parameters: dict, callback, *, http_status: int = 200):
    operation_id = _operation_id()
    supplied_tool = request.headers.get("X-Invoice-Agent-Tool")
    if not operation_id:
        if supplied_tool:
            raise AppError("Agent 写操作必须提供 Idempotency-Key。", 400, "operation_id_required")
        with transaction(db):
            business_payload, _safe_result = callback()
        return business_payload, None, False, http_status
    fingerprint = request_fingerprint(operation_name, parameters)
    terminal_error = None
    with transaction(db):
        reservation = reserve_operation(db, operation_id, operation_name, fingerprint)
        if reservation.replayed:
            return None, reservation.operation_result, True, reservation.http_status or http_status
        db.execute("SAVEPOINT agent_operation_business")
        try:
            if supplied_tool != operation_name:
                raise AppError(
                    "Agent tool header 与目标写操作不匹配。",
                    400,
                    "agent_tool_mismatch",
                )
            business_payload, safe_result = callback()
            complete_operation(db, reservation, safe_result, http_status=http_status)
        except AppError as exc:
            db.execute("ROLLBACK TO SAVEPOINT agent_operation_business")
            db.execute("RELEASE SAVEPOINT agent_operation_business")
            fail_reserved_operation(
                db,
                http_status=exc.status_code,
                error_code=exc.code,
                outcome="not_applied",
                reservation=reservation,
            )
            terminal_error = exc
        else:
            db.execute("RELEASE SAVEPOINT agent_operation_business")
    if terminal_error is not None:
        raise terminal_error
    return business_payload, safe_result, False, http_status


def _operation_response(business_payload: dict | None, safe_result: dict | None, replayed: bool):
    if safe_result is None:
        return business_payload
    if replayed:
        return {"operation_result": safe_result, "meta": {"replayed": True}}
    return {**(business_payload or {}), "operation_result": safe_result, "meta": {"replayed": False}}


def _expand_agent_money_fields(payload: dict) -> dict:
    """Accept integer-cent Agent fields while preserving the browser decimal contract."""
    if not request.environ.get("HTTP_IDEMPOTENCY_KEY"):
        return dict(payload)
    result = dict(payload)
    for cents_key, decimal_key in (
        ("amount_cents", "amount"),
        ("converted_amount_cents", "converted_amount"),
    ):
        if cents_key not in result:
            continue
        if decimal_key in result:
            raise AppError(
                f"{cents_key} 与 {decimal_key} 不能同时提供。", 400, "amount_field_conflict"
            )
        cents = result.pop(cents_key)
        if cents is None and cents_key == "converted_amount_cents":
            result[decimal_key] = None
            continue
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise AppError(f"{cents_key} 必须是非负整数。", 400, "invalid_amount")
        result[decimal_key] = format(Decimal(cents) / Decimal(100), "f")
    return result


def _bounded_text(value, label: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise AppError(f"{label}不能为空。", 400, "field_required")
    if len(text) > maximum:
        raise AppError(f"{label}最多 {maximum} 个字符。", 400, "field_too_long")
    return text


def _backup_before_delete() -> str:
    snapshot = create_database_backup(
        current_app.config["DATABASE"],
        current_app.config["BACKUP_DIR"],
        retention=current_app.config["BACKUP_RETENTION"],
        manual=True,
    )
    return snapshot.name


def _normalize_item_ids(values) -> list[int]:
    if not isinstance(values, list) or not values:
        raise AppError("请选择至少一条要删除的记录。", 400, "items_required")
    try:
        item_ids = list(dict.fromkeys(int(value) for value in values))
    except (TypeError, ValueError):
        raise AppError("条目编号无效。", 400, "invalid_item_ids")
    if len(item_ids) > 500:
        raise AppError("单次最多删除 500 条记录。", 400, "too_many_items")
    return item_ids


def _assert_item_mutable(item: dict) -> None:
    if item.get("batch_exporting"):
        raise AppError("报销包正在生成归档，请完成后再修改条目。", 409, "batch_exporting")


def _assert_batch_mutable(batch: dict) -> None:
    if batch.get("exporting"):
        raise AppError("报销包正在生成归档，请完成后再修改。", 409, "batch_exporting")


def _assert_item_mutable_now(db, item_id: int) -> None:
    row = db.execute(
        """SELECT b.export_token FROM batch_items bi
           JOIN reimbursement_batches b ON b.id=bi.batch_id WHERE bi.expense_item_id=?""",
        (item_id,),
    ).fetchone()
    if row and row["export_token"]:
        raise AppError("报销包正在生成归档，请完成后再修改条目。", 409, "batch_exporting")


def _assert_batch_mutable_now(db, batch_id: int) -> None:
    row = db.execute("SELECT export_token FROM reimbursement_batches WHERE id=?", (batch_id,)).fetchone()
    if row and row["export_token"]:
        raise AppError("报销包正在生成归档，请完成后再修改。", 409, "batch_exporting")


def _delete_item_records(db, item_versions: dict[int, int], allowed_statuses: set[str]) -> dict:
    item_ids = list(item_versions)
    placeholders = ",".join("?" for _ in item_ids)
    snapshot_name = _backup_before_delete()
    with transaction(db):
        rows = db.execute(
            f"""SELECT i.id,i.status,i.merchant,i.row_version,bi.batch_id FROM expense_items i
                LEFT JOIN batch_items bi ON bi.expense_item_id=i.id WHERE i.id IN ({placeholders})""",
            item_ids,
        ).fetchall()
        if len(rows) != len(item_ids):
            raise AppError("部分记录不存在，请刷新后重试。", 404, "item_not_found")
        if any(int(row["row_version"]) != item_versions[row["id"]] for row in rows):
            raise AppError("部分条目已变化，请刷新后重试。", 409, "stale_version")
        if any(row["status"] not in allowed_statuses or row["batch_id"] is not None for row in rows):
            raise AppError("处理中、已提交或已报销的记录不能直接删除；请先退出报销包。", 409, "item_delete_locked")
        merged_rows = db.execute(
            f"SELECT id FROM expense_items WHERE merged_into_item_id IN ({placeholders})",
            item_ids,
        ).fetchall()
        all_ids = list(dict.fromkeys([*item_ids, *(row["id"] for row in merged_rows)]))
        all_placeholders = ",".join("?" for _ in all_ids)
        managed_paths = [
            row["managed_path"]
            for row in db.execute(
                f"SELECT managed_path FROM attachments WHERE expense_item_id IN ({all_placeholders})",
                all_ids,
            ).fetchall()
        ]
        for path in managed_paths:
            enqueue_file_cleanup(
                db,
                path,
                "managed_file",
                current_app.config["IMPORT_DIR"],
                "删除报销条目后回收受管附件",
            )
        db.execute(
            f"DELETE FROM audit_logs WHERE object_type='item' AND object_id IN ({all_placeholders})",
            all_ids,
        )
        db.execute(f"DELETE FROM expense_items WHERE id IN ({all_placeholders})", all_ids)
        audit(
            db,
            "system",
            0,
            "item_records_deleted",
            {
                "item_ids": item_ids,
                "merged_source_ids": [row["id"] for row in merged_rows],
                "merchants": [row["merchant"] for row in rows],
                "snapshot": snapshot_name,
            },
        )

    cleanup = process_file_cleanup_queue(db)
    cleanup_warnings = [entry["code"] for entry in cleanup["failed"]]
    return {
        "deleted": True,
        "deleted_ids": item_ids,
        "deleted_count": len(item_ids),
        "cleanup_warnings": cleanup_warnings,
    }


def _default_project_id(db):
    row = db.execute("SELECT id FROM projects WHERE enabled=1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _create_draft_with_attachment(db, imported: dict, recognition: dict | None, recognition_error: str | None):
    recognition = recognition or {}
    raw_payload = {
        "merchant": recognition.get("merchant") or "",
        "expense_date": recognition.get("expense_date") or "",
        "amount": recognition.get("amount") or 0,
        "currency": recognition.get("currency") or "CNY",
        "converted_amount": recognition.get("converted_amount"),
        "purpose": recognition.get("purpose") or "",
        "project_id": _default_project_id(db),
    }
    try:
        values = validate_item_payload(raw_payload)
    except AppError:
        raw_payload.update({"amount": 0, "expense_date": "", "currency": "CNY"})
        values = validate_item_payload(raw_payload)
        recognition.setdefault("uncertainties", []).append("识别字段格式异常，请手工核对。")
    now = utc_now()
    def insert_records():
        cursor = db.execute(
            """INSERT INTO expense_items(
                merchant,expense_date,amount,amount_cents,currency,converted_amount,converted_amount_cents,purpose,project_id,status,
                ai_raw_json,uncertainties_json,recognition_error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'pending_confirmation',?,?,?,?,?)""",
            (
                values["merchant"],
                values["expense_date"],
                values["amount"],
                values["amount_cents"],
                values["currency"],
                values["converted_amount"],
                values["converted_amount_cents"],
                values["purpose"],
                values["project_id"],
                json_dump(recognition) if recognition else None,
                json_dump(recognition.get("uncertainties", [])),
                recognition_error,
                now,
                now,
            ),
        )
        item_id = cursor.lastrowid
        category = recognition.get("document_type", "unknown")
        if category not in CATEGORY_META:
            category = "unknown"
        attachment_cursor = db.execute(
            """INSERT INTO attachments(
                expense_item_id,category,original_name,normalized_name,managed_path,sha256,mime_type,size_bytes,
                ai_raw_json,recognition_error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item_id,
                category,
                imported["original_name"],
                imported["original_name"],
                imported["managed_path"],
                imported["sha256"],
                imported["mime_type"],
                imported["size_bytes"],
                json_dump(recognition) if recognition else None,
                recognition_error,
                now,
                now,
            ),
        )
        bind_attachment_file(db, attachment_cursor.lastrowid)
        action = "recognized" if recognition_error is None else "recognition_failed_manual_fallback"
        audit(db, "item", item_id, action, {"attachment_id": attachment_cursor.lastrowid, "error": recognition_error})
        return item_id

    if db.in_transaction:
        item_id = insert_records()
    else:
        with transaction(db):
            item_id = insert_records()
    return item_id


@api.get("/bootstrap")
def bootstrap():
    db = get_db()
    projects = [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY enabled DESC,name,id").fetchall()]
    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM settings").fetchall()}
    return jsonify(
        {
            "capabilities": {
                "document_intake_association": True,
                "inline_attachment_preview": True,
            },
            "dashboard": _dashboard(db),
            "projects": projects,
            "rules": get_rules(db),
            "requirements_version": get_requirements_version(db),
            "settings": settings,
            "recognition": recognition_status(),
            "categories": [{"code": code, **meta} for code, meta in CATEGORY_META.items()],
            "materials": get_materials(db),
        }
    )


def _dashboard(db):
    counts = {
        "pending_confirmation": db.execute("SELECT COUNT(*) AS n FROM expense_items WHERE status='pending_confirmation'").fetchone()["n"],
        "pending_reimbursement": db.execute("SELECT COUNT(*) AS n FROM expense_items WHERE status='pending_reimbursement'").fetchone()["n"],
        "submitted_unreimbursed": db.execute("SELECT COUNT(*) AS n FROM reimbursement_batches WHERE status='submitted'").fetchone()["n"],
    }
    counts["missing_materials"] = _count_incomplete_materials(db)
    integrity = _data_integrity_report(db)
    counts["data_anomalies"] = integrity["count"]
    recent_rows = db.execute(
        """SELECT b.*,p.name AS project_name,p.code AS project_code FROM reimbursement_batches b
           LEFT JOIN projects p ON p.id=b.project_id ORDER BY b.updated_at DESC LIMIT 5"""
    ).fetchall()
    return {
        "counts": counts,
        "recent_batches": [serialize_batch(db, row, detail=False) for row in recent_rows],
        "integrity": integrity,
    }


def _count_incomplete_materials(db) -> int:
    rules = get_rules(db)
    material_categories = {entry["code"]: set(entry["categories"]) for entry in get_materials(db)}
    rows = db.execute(
        """SELECT i.id,i.currency,i.amount_cents,i.converted_amount_cents,GROUP_CONCAT(a.category) AS categories
           FROM expense_items i LEFT JOIN attachments a ON a.expense_item_id=i.id
           WHERE i.status IN ('pending_reimbursement','in_batch') GROUP BY i.id"""
    ).fetchall()
    incomplete = 0
    for row in rows:
        categories = set((row["categories"] or "").split(",")) - {""}
        foreign = row["currency"] != "CNY"
        cents = row["amount_cents"] if not foreign else row["converted_amount_cents"]
        amount = (int(cents) / 100) if cents is not None else 0
        rule = next(
            (
                entry
                for entry in rules
                if amount >= entry["min_amount"] and (entry["max_amount"] is None or amount < entry["max_amount"])
            ),
            None,
        )
        missing = rule is None or (foreign and (not cents or "payment_record" not in categories))
        if rule and not missing:
            missing = any(
                not (material_categories.get(code, set()) & categories)
                for code in rule["required"]
                if not (foreign and code == "payment_record")
            )
        incomplete += int(missing)
    return incomplete


def _data_integrity_report(db) -> dict:
    foreign_rows = db.execute(
        """SELECT i.id,i.merchant,i.currency,i.status,b.id AS batch_id,b.name AS batch_name
           FROM expense_items i
           LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
           LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
           WHERE i.currency<>'CNY' AND i.status IN ('submitted','reimbursed')
             AND (COALESCE(i.converted_amount_cents,0)<=0 OR NOT EXISTS(
                 SELECT 1 FROM attachments a WHERE a.expense_item_id=i.id AND a.category='payment_record'
             )) ORDER BY i.id LIMIT 50"""
    ).fetchall()
    total_rows = db.execute(
        """SELECT b.id,b.name,b.total_amount_cents,
                  COALESCE(SUM(CASE WHEN i.currency='CNY' THEN i.amount_cents ELSE COALESCE(i.converted_amount_cents,0) END),0) AS calculated_cents
           FROM reimbursement_batches b
           LEFT JOIN batch_items bi ON bi.batch_id=b.id
           LEFT JOIN expense_items i ON i.id=bi.expense_item_id
           GROUP BY b.id HAVING b.total_amount_cents<>calculated_cents ORDER BY b.id LIMIT 50"""
    ).fetchall()
    status_rows = db.execute(
        """SELECT i.id,i.status,b.id AS batch_id,b.status AS batch_status FROM expense_items i
           LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
           LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
           WHERE (i.status='in_batch' AND COALESCE(b.status,'')<>'draft')
              OR (i.status='submitted' AND COALESCE(b.status,'')<>'submitted')
              OR (i.status='reimbursed' AND COALESCE(b.status,'')<>'reimbursed')
              OR (i.status IN ('pending_confirmation','pending_reimbursement','merged') AND b.id IS NOT NULL)
           ORDER BY i.id LIMIT 50"""
    ).fetchall()
    superseded_rows = db.execute(
        """SELECT id,name FROM reimbursement_batches
           WHERE superseded_archive_path IS NOT NULL ORDER BY id LIMIT 50"""
    ).fetchall()
    entries = [
        {
            "code": "foreign_payment_missing",
            "item_id": row["id"],
            "batch_id": row["batch_id"],
            "message": f"{row['merchant']}（#{row['id']}）缺少可核验的人民币实付记录",
        }
        for row in foreign_rows
    ]
    entries.extend(
        {
            "code": "batch_total_mismatch",
            "batch_id": row["id"],
            "message": f"{row['name']}（#{row['id']}）合计与条目不一致",
        }
        for row in total_rows
    )
    entries.extend(
        {
            "code": "status_mismatch",
            "item_id": row["id"],
            "batch_id": row["batch_id"],
            "message": f"条目 #{row['id']} 状态与报销包不一致",
        }
        for row in status_rows
    )
    entries.extend(
        {
            "code": "superseded_archive_pending",
            "batch_id": row["id"],
            "message": f"{row['name']}（#{row['id']}）仍有旧版归档待处理",
        }
        for row in superseded_rows
    )
    return {"healthy": not entries, "count": len(entries), "entries": entries}


@api.get("/dashboard")
def dashboard():
    return jsonify(_dashboard(get_db()))


@api.post("/imports")
def import_files():
    if request.environ.get("HTTP_IDEMPOTENCY_KEY") or request.headers.get("X-Invoice-Agent-Tool"):
        allowed_fields = {"external_processing_notice_version", "external_processing_ack"}
        if set(request.form) - allowed_fields:
            raise AppError("Agent 导入请求包含未知字段。", 400, "invalid_request")
        operation_id = _agent_file_request("import_invoice_file")
        notice, acknowledged = _require_external_processing_ack()
        upload = _single_agent_upload()
        outcome = run_agent_import(
            get_db(),
            current_app,
            operation_id,
            upload,
            notice_version=notice,
            acknowledged=acknowledged,
            recognize=recognize_file,
            explain_failure=explain_recognition_failure,
        )
        return jsonify(
            _operation_response(outcome.business_payload, outcome.safe_result, outcome.replayed)
        ), outcome.http_status
    uploads = request.files.getlist("files") or ([request.files["file"]] if "file" in request.files else [])
    if not uploads:
        raise AppError("请选择至少一个文件。", 400, "file_required")
    if len(uploads) > MAX_IMPORT_FILES:
        raise AppError(f"单次最多导入 {MAX_IMPORT_FILES} 个文件。", 400, "too_many_files")
    db = get_db()
    prepared = []
    try:
        for upload in uploads:
            imported = import_uploaded_file(upload)
            recognition = None
            error_message = None
            try:
                recognition = recognize_file(imported["managed_path"], imported["mime_type"], imported["original_name"])
            except Exception as exc:
                error_message = explain_recognition_failure(exc)
            prepared.append({"imported": imported, "recognition": recognition, "error": error_message})
        with transaction(db):
            item_ids = [
                _create_draft_with_attachment(db, entry["imported"], entry["recognition"], entry["error"])
                for entry in prepared
            ]
    except Exception:
        for entry in prepared:
            delete_managed_attachment(entry["imported"]["managed_path"])
        raise
    results = []
    for item_id, entry in zip(item_ids, prepared):
        item = serialize_item(db, item_id)
        results.append(
            {
                "item": item,
                "matches": find_duplicate_candidates(db, item_id),
                "imported": True,
                "recognition_succeeded": entry["error"] is None,
                "message": "导入成功，已保存受管副本。" if entry["error"] is None else "导入成功，已保存副本；识别失败，可手工补录。",
            }
        )
    return jsonify({"results": results}), 201


@api.post("/items/manual")
def create_manual_draft():
    db = get_db()
    raw_payload = body()
    parameters = dict(raw_payload)
    parameters.setdefault("converted_amount_cents", None)

    def apply():
        if "operation_id" in raw_payload:
            raise AppError(
                "operation_id 只能通过 Idempotency-Key header 提供。",
                400,
                "operation_id_conflict",
            )
        payload = _expand_agent_money_fields(raw_payload)
        allowed_fields = {
            "merchant",
            "expense_date",
            "amount",
            "currency",
            "converted_amount",
            "purpose",
            "project_id",
        }
        if request.environ.get("HTTP_IDEMPOTENCY_KEY") and set(payload) - allowed_fields:
            raise AppError("Agent 草稿请求包含未知字段。", 400, "invalid_request")
        payload.setdefault("project_id", _default_project_id(db))
        values = validate_item_payload(
            payload,
            reject_json_float=bool(request.environ.get("HTTP_IDEMPOTENCY_KEY")),
        )
        if values["project_id"] is not None and not project_exists(db, values["project_id"]):
            raise AppError("报销项目不存在或已停用。", 400, "invalid_project")
        now = utc_now()
        cursor = db.execute(
            """INSERT INTO expense_items(
               merchant,expense_date,amount,amount_cents,currency,converted_amount,converted_amount_cents,purpose,project_id,status,uncertainties_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'pending_confirmation','[]',?,?)""",
            (
                values["merchant"], values["expense_date"], values["amount"], values["amount_cents"], values["currency"],
                values["converted_amount"], values["converted_amount_cents"], values["purpose"], values["project_id"], now, now,
            ),
        )
        audit(db, "item", cursor.lastrowid, "manual_draft_created", {})
        item = serialize_item(db, cursor.lastrowid)
        return {"item": item}, operation_result(
            [{"type": "invoice_item", "id": item["id"], "version": item["version"]}]
        )

    business_payload, safe_result, replayed, status = _database_operation(
        db, "create_manual_invoice_draft", parameters, apply, http_status=201
    )
    return jsonify(_operation_response(business_payload, safe_result, replayed)), status


@api.get("/drafts")
def list_drafts():
    db = get_db()
    if request.args.get("cursor") is not None or (request.args.get("limit") is not None and request.args.get("offset") is None):
        limit = parse_limit(request.args.get("limit"))
        filters_digest = filter_hash("drafts", {"status": "pending_confirmation"})
        supplied_cursor = request.args.get("cursor")
        if supplied_cursor:
            cursor = decode_cursor(supplied_cursor, "drafts", filters_digest)
        else:
            snapshot = db.execute(
                "SELECT COALESCE(MAX(id),0) AS max_id FROM expense_items WHERE status='pending_confirmation'"
            ).fetchone()["max_id"]
            cursor = KeysetCursor(snapshot_max_id=int(snapshot))
        clauses = ["status='pending_confirmation'", "id<=?"]
        params: list = [cursor.snapshot_max_id]
        if cursor.created_at is not None:
            clauses.append("(created_at<? OR (created_at=? AND id<?))")
            params.extend([cursor.created_at, cursor.created_at, cursor.row_id])
        rows = db.execute(
            f"SELECT id,created_at FROM expense_items WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC,id DESC LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        visible = rows[:limit]
        items = [serialize_item(db, row["id"]) for row in visible]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = encode_cursor(
                "drafts",
                filters_digest,
                KeysetCursor(cursor.snapshot_max_id, last["created_at"], int(last["id"])),
            )
        return jsonify(
            {
                "items": items,
                "meta": {"pagination": {"next_cursor": next_cursor, "has_more": has_more}},
            }
        )
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        raise AppError("分页参数无效。", 400, "invalid_pagination")
    total = db.execute("SELECT COUNT(*) AS n FROM expense_items WHERE status='pending_confirmation'").fetchone()["n"]
    ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM expense_items WHERE status='pending_confirmation' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    ]
    items = []
    for item_id in ids:
        item = serialize_item(db, item_id)
        item["matches"] = find_duplicate_candidates(db, item_id)
        items.append(item)
    return jsonify({"items": items, "count": total, "limit": limit, "offset": offset})


@api.get("/items/<int:item_id>")
def get_item(item_id: int):
    db = get_db()
    item = serialize_item(db, item_id, include_audit=True)
    if item["status"] == "pending_confirmation":
        item["review"] = public_review(build_review(db, item_id))
    return jsonify({"item": item})


@api.post("/items/<int:item_id>/duplicate-review-sessions")
def create_duplicate_review_session(item_id: int):
    db = get_db()
    payload = body()
    if set(payload) != {"expected_version", "review_token"}:
        raise AppError("创建完整重复核对会话需要 expected_version 和 review_token。", 400, "invalid_request")
    expected_version = _integer_field(payload, "expected_version")
    with transaction(db):
        page = create_review_session(db, item_id, expected_version, payload.get("review_token"))
    return jsonify(page), 201


@api.post("/items/<int:item_id>/duplicate-review-sessions/<session_id>/next")
def next_duplicate_review_session(item_id: int, session_id: str):
    db = get_db()
    payload = body()
    if set(payload) != {"cursor"} or not isinstance(payload.get("cursor"), str):
        raise AppError("必须提交上一页返回的 cursor。", 400, "invalid_review_cursor")
    with transaction(db):
        page = next_review_session_page(db, item_id, session_id, payload["cursor"])
    return jsonify(page)


@api.delete("/items/<int:item_id>")
def delete_item(item_id: int):
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    return jsonify(
        _delete_item_records(get_db(), {item_id: expected_version}, {"pending_confirmation", "pending_reimbursement"})
    )


@api.post("/items/bulk-delete")
def bulk_delete_items():
    payload = body()
    entries = payload.get("items")
    if not isinstance(entries, list) or not entries:
        raise AppError("请选择至少一条要删除的记录。", 400, "items_required")
    if len(entries) > 500:
        raise AppError("单次最多删除 500 条记录。", 400, "too_many_items")
    versions = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"item_id", "expected_version"}:
            raise AppError("批量删除条目必须逐项提供 item_id 和 expected_version。", 400, "version_required")
        item_id = entry["item_id"]
        version = entry["expected_version"]
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            raise AppError("条目编号无效。", 400, "invalid_item_ids")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise AppError("必须逐项提供有效版本。", 400, "version_required")
        if item_id in versions:
            raise AppError("批量删除不能包含重复条目。", 400, "invalid_item_ids")
        versions[item_id] = version
    return jsonify(_delete_item_records(get_db(), versions, {"pending_confirmation", "pending_reimbursement"}))


@api.patch("/items/<int:item_id>")
def update_item(item_id: int):
    db = get_db()
    raw_payload = body()
    parameters = {"item_id": item_id, "payload": raw_payload}

    def apply():
        if "operation_id" in raw_payload:
            raise AppError(
                "operation_id 只能通过 Idempotency-Key header 提供。",
                400,
                "operation_id_conflict",
            )
        payload = _expand_agent_money_fields(raw_payload)
        expected_version = _integer_field(payload, "expected_version")
        expected_batch_version = (
            _integer_field(payload, "expected_batch_version")
            if "expected_batch_version" in payload
            else None
        )
        changes = {
            key: value
            for key, value in payload.items()
            if key not in {"expected_version", "expected_batch_version"}
        }
        allowed_changes = {
            "merchant",
            "expense_date",
            "amount",
            "currency",
            "converted_amount",
            "purpose",
            "project_id",
        }
        if set(changes) - allowed_changes:
            raise AppError("条目更新包含未知字段。", 400, "invalid_request")
        context = _require_item_version(db, item_id, expected_version, expected_batch_version)
        current = serialize_item(db, item_id)
        if current["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
            raise AppError("已提交或已报销条目不可修改。", 409, "item_locked")
        if request.environ.get("HTTP_IDEMPOTENCY_KEY") and any(
            isinstance(value, float) for value in changes.values()
        ):
            raise AppError("Agent 写请求不得使用 JSON 浮点数。", 400, "json_float_not_allowed")
        values = validate_item_payload(
            {**current, **changes},
            require_complete=current["status"] != "pending_confirmation",
            reject_json_float=False,
        )
        if values["project_id"] and not project_exists(db, values["project_id"]):
            raise AppError("报销项目不存在或已停用。", 400, "invalid_project")
        db.execute(
            """UPDATE expense_items SET merchant=?,expense_date=?,amount=?,amount_cents=?,currency=?,converted_amount=?,converted_amount_cents=?,purpose=?,project_id=?,updated_at=? WHERE id=?""",
            (
                values["merchant"], values["expense_date"], values["amount"], values["amount_cents"], values["currency"],
                values["converted_amount"], values["converted_amount_cents"], values["purpose"], values["project_id"], utc_now(), item_id,
            ),
        )
        refresh_item_attachment_names(db, item_id)
        new_version = _touch_item(db, item_id)
        batch_version = None
        if context["batch_id"]:
            batch_version = recalculate_batch_total(db, context["batch_id"])
        audit(db, "item", item_id, "edited", {"before": {k: current[k] for k in values}, "after": values})
        refs = [{"type": "invoice_item", "id": item_id, "version": new_version}]
        if context["batch_id"]:
            refs.append({"type": "reimbursement_batch", "id": context["batch_id"], "version": batch_version})
        return {"item": serialize_item(db, item_id, include_audit=True)}, operation_result(refs)

    business_payload, safe_result, replayed, status = _database_operation(
        db, "update_invoice_item", parameters, apply
    )
    return jsonify(_operation_response(business_payload, safe_result, replayed)), status


@api.post("/items/<int:item_id>/confirm")
def confirm_item(item_id: int):
    db = get_db()
    raw_payload = body()
    fingerprint_payload = dict(raw_payload)
    fingerprint_payload.setdefault("overflow_review_token", None)
    parameters = {"item_id": item_id, "payload": fingerprint_payload}

    def apply():
        payload = dict(raw_payload)
        expected_version = _integer_field(payload, "expected_version")
        review_token = payload.get("review_token")
        allowed = {
            "expected_version",
            "review_token",
            "duplicate_resolution",
            "acknowledged_uncertainty_ids",
            "acknowledged_duplicate_ids",
            "overflow_review_token",
        }
        if set(payload) - allowed:
            raise AppError(
                "确认操作不能同时修改发票字段；请先保存修改并重新读取。",
                400,
                "confirm_fields_not_allowed",
            )
        review = require_current_review(db, item_id, expected_version, review_token)
        current = serialize_item(db, item_id)
        if current["status"] != "pending_confirmation":
            raise AppError("只有待确认条目可以执行确认。", 409, "invalid_item_status")
        overflow = consume_overflow_token(
            db, item_id, expected_version, review, payload.get("overflow_review_token")
        )
        validate_confirmation(review, payload, overflow_authorized=overflow["authorized"])
        values = validate_item_payload(current, require_complete=True)
        if not project_exists(db, values["project_id"]):
            raise AppError("报销项目不存在或已停用。", 400, "invalid_project")
        now = utc_now()
        db.execute(
            """UPDATE expense_items SET status='pending_reimbursement',confirmed_json=?,confirmed_at=?,updated_at=?
               WHERE id=? AND status='pending_confirmation'""",
            (
                json_dump(values), now, now, item_id,
            ),
        )
        new_version = _touch_item(db, item_id)
        refresh_item_attachment_names(db, item_id)
        audit(db, "item", item_id, "confirmed", {"from": "pending_confirmation", "to": "pending_reimbursement", "snapshot": values})
        return {"item": serialize_item(db, item_id, include_audit=True)}, operation_result(
            [{"type": "invoice_item", "id": item_id, "version": new_version}]
        )

    business_payload, safe_result, replayed, status = _database_operation(
        db, "confirm_invoice_item", parameters, apply
    )
    return jsonify(_operation_response(business_payload, safe_result, replayed)), status


@api.post("/items/<int:item_id>/merge/<int:target_id>")
def merge_item(item_id: int, target_id: int):
    db = get_db()
    raw_payload = body()
    fingerprint_payload = dict(raw_payload)
    fingerprint_payload.setdefault("overflow_review_token", None)
    parameters = {
        "source_id": item_id,
        "target_id": target_id,
        "payload": fingerprint_payload,
    }

    def apply():
        payload = dict(raw_payload)
        source_version = _integer_field(payload, "source_version")
        target_version = _integer_field(payload, "target_version")
        expected_batch_version = (
            _integer_field(payload, "expected_batch_version")
            if "expected_batch_version" in payload
            else None
        )
        allowed = {
            "source_version",
            "target_version",
            "expected_batch_version",
            "source_review_token",
            "overflow_review_token",
        }
        if set(payload) - allowed:
            raise AppError("合并请求包含未知字段。", 400, "invalid_request")
        if item_id == target_id:
            raise AppError("草稿不能合并到自身。", 409, "invalid_merge_target")
        review = require_current_review(db, item_id, source_version, payload.get("source_review_token"))
        source = serialize_item(db, item_id)
        if source["status"] != "pending_confirmation":
            raise AppError("只有待确认草稿可以合并。", 409, "source_not_draft")
        candidate = require_merge_target(review, target_id)
        overflow = consume_overflow_token(
            db, item_id, source_version, review, payload.get("overflow_review_token")
        )
        if overflow["authorized"] and target_id not in overflow["allowed_merge_ids"]:
            raise AppError("合并目标不在已完整核对的允许集合中。", 409, "merge_target_not_reviewed")
        if candidate["version"] != target_version:
            raise AppError("合并目标已变化，请重新读取核对。", 409, "review_changed")
        target_context = _require_item_version(db, target_id, target_version, expected_batch_version)
        target = serialize_item(db, target_id)
        if target["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
            raise AppError("目标条目已锁定。", 409, "target_locked")
        attachment_ids = [row["id"] for row in db.execute("SELECT id FROM attachments WHERE expense_item_id=?", (item_id,)).fetchall()]
        db.execute("UPDATE attachments SET expense_item_id=?,updated_at=? WHERE expense_item_id=?", (target_id, utc_now(), item_id))
        db.execute("UPDATE expense_items SET status='merged',merged_into_item_id=?,updated_at=? WHERE id=?", (target_id, utc_now(), item_id))
        for attachment_id in attachment_ids:
            bind_attachment_file(db, attachment_id, "重复匹配后关联到已有条目")
        refresh_foreign_payment_amount(db, target_id)
        source_new_version = _touch_item(db, item_id)
        target_new_version = _touch_item(db, target_id)
        batch_version = None
        if target_context["batch_id"]:
            batch_version = recalculate_batch_total(db, target_context["batch_id"])
        audit(db, "item", item_id, "merged_into_existing", {"target_item_id": target_id, "attachment_ids": attachment_ids})
        audit(db, "item", target_id, "attachments_merged_from_draft", {"source_item_id": item_id, "attachment_ids": attachment_ids})
        refs = [
            {"type": "invoice_item", "id": item_id, "version": source_new_version},
            {"type": "invoice_item", "id": target_id, "version": target_new_version},
        ]
        if target_context["batch_id"]:
            refs.append(
                {"type": "reimbursement_batch", "id": target_context["batch_id"], "version": batch_version}
            )
        return {"item": serialize_item(db, target_id, include_audit=True)}, operation_result(refs)

    business_payload, safe_result, replayed, status = _database_operation(
        db, "merge_invoice_draft", parameters, apply
    )
    return jsonify(_operation_response(business_payload, safe_result, replayed)), status


@api.get("/items")
def list_items():
    db = get_db()
    status = request.args.get("status", "pending_reimbursement")
    allowed_status = {"pending_confirmation", "pending_reimbursement", "in_batch", "submitted", "reimbursed"}
    if status not in allowed_status:
        raise AppError("状态筛选无效。", 400, "invalid_status")
    clauses = ["i.status=?"]
    params: list = [status]
    for cents_arg, legacy_arg, operator in (
        ("amount_min_cents", "amount_min", ">="),
        ("amount_max_cents", "amount_max", "<="),
    ):
        raw_cents = request.args.get(cents_arg)
        if raw_cents in (None, ""):
            continue
        if request.args.get(legacy_arg) not in (None, ""):
            raise AppError(f"{cents_arg} 与 {legacy_arg} 不能同时提供。", 400, "invalid_filter")
        try:
            cents = int(raw_cents)
        except ValueError:
            raise AppError(f"筛选参数 {cents_arg} 无效。", 400, "invalid_filter")
        if cents < 0 or str(cents) != raw_cents.strip():
            raise AppError(f"筛选参数 {cents_arg} 无效。", 400, "invalid_filter")
        clauses.append(
            f"COALESCE(CASE WHEN i.currency='CNY' THEN i.amount_cents ELSE i.converted_amount_cents END,-1){operator}?"
        )
        params.append(cents)
    mappings = [
        ("project_id", "i.project_id=?", int),
        ("date_from", "i.expense_date>=?", str),
        ("date_to", "i.expense_date<=?", str),
        ("amount_min", "COALESCE(CASE WHEN i.currency='CNY' THEN i.amount_cents ELSE i.converted_amount_cents END,-1)>=?", float),
        ("amount_max", "COALESCE(CASE WHEN i.currency='CNY' THEN i.amount_cents ELSE i.converted_amount_cents END,-1)<=?", float),
    ]
    for arg, clause, converter in mappings:
        value = request.args.get(arg)
        if value not in (None, ""):
            try:
                value = converter(value)
            except ValueError:
                raise AppError(f"筛选参数 {arg} 无效。", 400, "invalid_filter")
            if converter is float and not math.isfinite(value):
                raise AppError(f"筛选参数 {arg} 无效。", 400, "invalid_filter")
            if arg.startswith("amount_"):
                value = money_to_cents(value)
            if arg.startswith("date_"):
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise AppError(f"筛选参数 {arg} 无效。", 400, "invalid_filter")
            clauses.append(clause)
            params.append(value)
    search = _bounded_text(request.args.get("search", ""), "搜索关键词", 200)
    if search:
        clauses.append("(i.merchant LIKE ? OR i.purpose LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    material_filter = request.args.get("material")
    if material_filter not in (None, "", "complete", "missing"):
        raise AppError("材料状态筛选无效。", 400, "invalid_filter")
    if request.args.get("cursor") is not None or request.args.get("limit") is not None:
        limit = parse_limit(request.args.get("limit"))
        normalized_filters = {
            "status": status,
            "project_id": request.args.get("project_id") or None,
            "date_from": request.args.get("date_from") or None,
            "date_to": request.args.get("date_to") or None,
            "amount_min": request.args.get("amount_min") or None,
            "amount_max": request.args.get("amount_max") or None,
            "amount_min_cents": request.args.get("amount_min_cents") or None,
            "amount_max_cents": request.args.get("amount_max_cents") or None,
            "search": search or None,
            "material": material_filter or None,
        }
        filters_digest = filter_hash("items", normalized_filters)
        supplied_cursor = request.args.get("cursor")
        if supplied_cursor:
            page_cursor = decode_cursor(supplied_cursor, "items", filters_digest)
        else:
            snapshot = db.execute(
                f"SELECT COALESCE(MAX(i.id),0) AS max_id FROM expense_items i WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()["max_id"]
            page_cursor = KeysetCursor(snapshot_max_id=int(snapshot))

        matches = []
        scan_created_at = page_cursor.created_at
        scan_id = page_cursor.row_id
        exhausted = False
        while len(matches) < limit + 1 and not exhausted:
            page_clauses = [*clauses, "i.id<=?"]
            page_params = [*params, page_cursor.snapshot_max_id]
            if scan_created_at is not None:
                page_clauses.append("(i.created_at<? OR (i.created_at=? AND i.id<?))")
                page_params.extend([scan_created_at, scan_created_at, scan_id])
            rows = db.execute(
                f"""SELECT i.*, p.name AS project_name,p.code AS project_code,
                            bi.batch_id,b.name AS batch_name,b.status AS batch_status,
                            b.export_token AS batch_export_token,b.row_version AS batch_row_version
                     FROM expense_items i LEFT JOIN projects p ON p.id=i.project_id
                     LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
                     LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
                     WHERE {' AND '.join(page_clauses)}
                     ORDER BY i.created_at DESC,i.id DESC LIMIT 200""",
                page_params,
            ).fetchall()
            if not rows:
                exhausted = True
                break
            for row in rows:
                item = serialize_item(db, row)
                if material_filter == "complete" and not item["material"]["complete"]:
                    continue
                if material_filter == "missing" and item["material"]["complete"]:
                    continue
                matches.append((row, item))
                if len(matches) >= limit + 1:
                    break
            scan_created_at = rows[-1]["created_at"]
            scan_id = int(rows[-1]["id"])
            exhausted = len(rows) < 200
        visible = matches[:limit]
        has_more = len(matches) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1][0]
            next_cursor = encode_cursor(
                "items",
                filters_digest,
                KeysetCursor(page_cursor.snapshot_max_id, last["created_at"], int(last["id"])),
            )
        return jsonify(
            {
                "items": [entry[1] for entry in visible],
                "count": len(visible),
                "meta": {"pagination": {"next_cursor": next_cursor, "has_more": has_more}},
            }
        )
    sql = f"""SELECT i.*, p.name AS project_name,p.code AS project_code,
                     bi.batch_id,b.name AS batch_name,b.status AS batch_status,
                     b.export_token AS batch_export_token,b.row_version AS batch_row_version
              FROM expense_items i LEFT JOIN projects p ON p.id=i.project_id
              LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
              LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
              WHERE {' AND '.join(clauses)} ORDER BY i.expense_date DESC,i.created_at DESC,i.id DESC LIMIT 500"""
    items = [serialize_item(db, row) for row in db.execute(sql, params).fetchall()]
    if material_filter == "complete":
        items = [item for item in items if item["material"]["complete"]]
    elif material_filter == "missing":
        items = [item for item in items if not item["material"]["complete"]]
    return jsonify({"items": items, "count": len(items)})


@api.post("/items/<int:item_id>/attachments")
def upload_attachment(item_id: int):
    db = get_db()
    if request.environ.get("HTTP_IDEMPOTENCY_KEY") or request.headers.get("X-Invoice-Agent-Tool"):
        allowed_fields = {
            "expected_version", "expected_batch_version", "category",
            "external_processing_notice_version", "external_processing_ack",
        }
        if set(request.form) - allowed_fields:
            raise AppError("Agent 附件请求包含未知字段。", 400, "invalid_request")
        operation_id = _agent_file_request("add_invoice_attachment")
        expected_version = _agent_form_integer("expected_version")
        expected_batch_version = _agent_form_integer("expected_batch_version", required=False)
        category = _single_form_value("category", required=True)
        if category not in CATEGORY_META:
            raise AppError("材料类型无效。", 400, "invalid_category")
        notice = _single_form_value("external_processing_notice_version")
        ack_raw = _single_form_value("external_processing_ack")
        acknowledged = None
        if category == "payment_record":
            notice, acknowledged = _require_external_processing_ack()
        elif notice is not None or ack_raw is not None:
            if notice != "deepseek-v1" or ack_raw != "true":
                raise AppError("外部处理确认字段无效。", 400, "external_processing_ack_required")
            acknowledged = True
        upload = _single_agent_upload()
        outcome = run_agent_attachment(
            db,
            current_app,
            operation_id,
            upload,
            item_id=item_id,
            expected_version=expected_version,
            expected_batch_version=expected_batch_version,
            category=category,
            notice_version=notice,
            acknowledged=acknowledged,
            recognize=recognize_file,
            explain_failure=explain_recognition_failure,
        )
        return jsonify(
            _operation_response(outcome.business_payload, outcome.safe_result, outcome.replayed)
        ), outcome.http_status
    expected_version = _form_integer("expected_version")
    expected_batch_version = _form_integer("expected_batch_version", required=False)
    item = serialize_item(db, item_id)
    if item["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
        raise AppError("该条目已锁定，不能增添附件。", 409, "item_locked")
    _assert_item_mutable(item)
    _require_item_version(db, item_id, expected_version, expected_batch_version)
    upload = request.files.get("file")
    if not upload:
        raise AppError("请选择附件。", 400, "file_required")
    category = request.form.get("category", "unknown")
    if category not in CATEGORY_META:
        raise AppError("材料类型无效。", 400, "invalid_category")
    imported = import_uploaded_file(upload)
    recognition = None
    recognition_error = None
    if category == "payment_record":
        try:
            recognition = recognize_file(imported["managed_path"], imported["mime_type"], imported["original_name"])
        except Exception as exc:
            recognition_error = explain_recognition_failure(exc)
    now = utc_now()
    try:
        with transaction(db):
            context = _require_item_version(db, item_id, expected_version, expected_batch_version)
            cursor = db.execute(
                """INSERT INTO attachments(expense_item_id,category,original_name,normalized_name,managed_path,sha256,mime_type,size_bytes,ai_raw_json,recognition_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, category, imported["original_name"], imported["original_name"], imported["managed_path"],
                    imported["sha256"], imported["mime_type"], imported["size_bytes"],
                    json_dump(recognition) if recognition else None, recognition_error, now, now,
                ),
            )
            bind_attachment_file(db, cursor.lastrowid)
            if category == "payment_record":
                refresh_foreign_payment_amount(db, item_id)
            _touch_item(db, item_id)
            if context["batch_id"]:
                recalculate_batch_total(db, context["batch_id"])
            audit(db, "item", item_id, "attachment_added", {"attachment_id": cursor.lastrowid, "category": category, "original_name": imported["original_name"]})
    except Exception:
        Path(imported["managed_path"]).unlink(missing_ok=True)
        raise
    message = "导入成功，已保存受管副本。"
    if category == "payment_record":
        message = (
            "支付记录已导入，并识别人民币实付金额。"
            if recognition and payment_rmb_cents(recognition)
            else "支付记录已导入，但未识别到人民币实付金额，请手工填写。"
        )
    return jsonify({"item": serialize_item(db, item_id), "message": message, "recognition_error": recognition_error}), 201


@api.patch("/attachments/<int:attachment_id>")
def update_attachment(attachment_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    item = serialize_item(db, row["expense_item_id"])
    if item["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
        raise AppError("该附件已归档，不能修改。", 409, "attachment_locked")
    _assert_item_mutable(item)
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    expected_batch_version = (
        _integer_field(payload, "expected_batch_version") if "expected_batch_version" in payload else None
    )
    _require_item_version(db, row["expense_item_id"], expected_version, expected_batch_version)
    category = payload.get("category", row["category"])
    if category not in CATEGORY_META:
        raise AppError("材料类型无效。", 400, "invalid_category")
    recognition = None
    recognition_error = None
    if category == "payment_record" and (category != row["category"] or not row["ai_raw_json"]):
        try:
            recognition = recognize_file(row["managed_path"], row["mime_type"], row["original_name"])
        except Exception as exc:
            recognition_error = explain_recognition_failure(exc)
    with transaction(db):
        context = _require_item_version(
            db, row["expense_item_id"], expected_version, expected_batch_version
        )
        changes = {
            "attachment_id": attachment_id,
            "before": {"category": row["category"], "normalized_name": row["normalized_name"]},
        }
        if category != row["category"]:
            db.execute(
                "UPDATE attachments SET category=?,ai_raw_json=COALESCE(?,ai_raw_json),recognition_error=?,updated_at=? WHERE id=?",
                (category, json_dump(recognition) if recognition else None, recognition_error, utc_now(), attachment_id),
            )
            bind_attachment_file(db, attachment_id, "用户修改材料类型")
        if payload.get("normalized_name"):
            custom_rename_attachment(db, attachment_id, payload["normalized_name"])
        updated = db.execute("SELECT category,normalized_name FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        changes["after"] = {"category": updated["category"], "normalized_name": updated["normalized_name"]}
        refresh_foreign_payment_amount(db, row["expense_item_id"])
        _touch_item(db, row["expense_item_id"])
        if context["batch_id"]:
            recalculate_batch_total(db, context["batch_id"])
        audit(db, "item", row["expense_item_id"], "attachment_updated", changes)
    return jsonify({"item": serialize_item(db, row["expense_item_id"])})


@api.post("/attachments/<int:attachment_id>/recognize-payment")
def recognize_payment_attachment(attachment_id: int):
    db = get_db()
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    expected_batch_version = (
        _integer_field(payload, "expected_batch_version") if "expected_batch_version" in payload else None
    )
    row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    if row["category"] != "payment_record":
        raise AppError("只有支付记录可以识别人民币实付金额。", 409, "not_payment_record")
    item = serialize_item(db, row["expense_item_id"])
    if item["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
        raise AppError("该附件已归档，请先退回报销包后再重新识别。", 409, "attachment_locked")
    _assert_item_mutable(item)
    _require_item_version(db, row["expense_item_id"], expected_version, expected_batch_version)

    recognition = None
    recognition_error = None
    try:
        recognition = recognize_file(row["managed_path"], row["mime_type"], row["original_name"])
        if not payment_rmb_cents(recognition):
            recognition_error = "支付记录中未识别到明确的人民币实际扣款，请核对文件或手工填写。"
    except Exception as exc:
        recognition_error = explain_recognition_failure(exc)

    with transaction(db):
        context = _require_item_version(
            db, row["expense_item_id"], expected_version, expected_batch_version
        )
        db.execute(
            "UPDATE attachments SET ai_raw_json=?,recognition_error=?,updated_at=? WHERE id=?",
            (json_dump(recognition) if recognition else None, recognition_error, utc_now(), attachment_id),
        )
        refresh_foreign_payment_amount(db, row["expense_item_id"])
        _touch_item(db, row["expense_item_id"])
        if context["batch_id"]:
            recalculate_batch_total(db, context["batch_id"])
        audit(
            db,
            "item",
            row["expense_item_id"],
            "payment_recognition_retried",
            {"attachment_id": attachment_id, "succeeded": recognition_error is None, "error": recognition_error},
        )
    updated_item = serialize_item(db, row["expense_item_id"], include_audit=True)
    if recognition_error:
        return jsonify(
            {
                "message": recognition_error,
                "warning_codes": ["payment_recognition_failed"],
                "item": updated_item,
            }
        )
    return jsonify({"item": updated_item, "message": "已重新识别人民币实付金额。"})


@api.delete("/attachments/<int:attachment_id>")
def delete_attachment(attachment_id: int):
    db = get_db()
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    expected_batch_version = (
        _integer_field(payload, "expected_batch_version") if "expected_batch_version" in payload else None
    )
    row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row:
        raise AppError("附件不存在。", 404, "attachment_not_found")
    item = serialize_item(db, row["expense_item_id"])
    if item["status"] not in ("pending_confirmation", "pending_reimbursement", "in_batch"):
        raise AppError("该附件已归档，不能删除。", 409, "attachment_locked")
    _assert_item_mutable(item)
    with transaction(db):
        context = _require_item_version(
            db, row["expense_item_id"], expected_version, expected_batch_version
        )
        enqueue_file_cleanup(
            db,
            row["managed_path"],
            "managed_file",
            current_app.config["IMPORT_DIR"],
            "删除单份附件后移入回收站",
        )
        db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        refresh_foreign_payment_amount(db, row["expense_item_id"])
        _touch_item(db, row["expense_item_id"])
        if context["batch_id"]:
            recalculate_batch_total(db, context["batch_id"])
        audit(db, "item", row["expense_item_id"], "attachment_removed", {"attachment_id": attachment_id, "original_name": row["original_name"]})
    cleanup = process_file_cleanup_queue(db)
    return jsonify({"item": serialize_item(db, row["expense_item_id"]), "cleanup": cleanup})


@api.get("/attachments/<int:attachment_id>/download")
def download_attachment(attachment_id: int):
    row = get_db().execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not row or not Path(row["managed_path"]).is_file():
        raise AppError("附件文件不存在。", 404, "attachment_file_not_found")
    return send_file(row["managed_path"], as_attachment=True, download_name=row["normalized_name"], mimetype=row["mime_type"])


@api.post("/batches")
def create_batch():
    db = get_db()
    raw_payload = body()
    parameters = dict(raw_payload)
    parameters.setdefault("purpose", "")
    parameters.setdefault("notes", "")

    def apply():
        payload = dict(raw_payload)
        if "operation_id" in payload:
            raise AppError(
                "operation_id 只能通过 Idempotency-Key header 提供。",
                400,
                "operation_id_conflict",
            )
        allowed = {"name", "purpose", "notes", "project_id", "items"}
        if request.environ.get("HTTP_IDEMPOTENCY_KEY") and set(payload) - allowed:
            raise AppError("Agent 报销包请求包含未知字段。", 400, "invalid_request")
        entries = payload.get("items")
        if not isinstance(entries, list) or not entries:
            raise AppError("请至少选择一笔待报销条目。", 400, "items_required")
        if len(entries) > 200:
            raise AppError("单个报销包最多包含 200 笔条目。", 400, "too_many_items")
        item_versions = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"item_id", "expected_version"}:
                raise AppError(
                    "创建报销包必须逐项提供 item_id 和 expected_version。",
                    400,
                    "version_required",
                )
            entry_item_id = entry["item_id"]
            version = entry["expected_version"]
            if (
                isinstance(entry_item_id, bool)
                or not isinstance(entry_item_id, int)
                or entry_item_id <= 0
            ):
                raise AppError("条目编号无效。", 400, "invalid_item_ids")
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                raise AppError("条目版本无效。", 400, "version_required")
            if entry_item_id in item_versions:
                raise AppError("同一条目不能重复加入报销包。", 400, "invalid_item_ids")
            item_versions[entry_item_id] = version
        item_ids = list(item_versions)
        placeholders = ",".join("?" for _ in item_ids)
        name = _bounded_text(
            payload.get("name") or f"报销包-{date.today().isoformat()}",
            "报销包名称",
            120,
            required=True,
        )
        purpose = _bounded_text(payload.get("purpose"), "用途说明", 2000)
        notes = _bounded_text(payload.get("notes"), "备注", 4000)
        requested_project_id = payload.get("project_id")
        rows = db.execute(f"SELECT * FROM expense_items WHERE id IN ({placeholders})", item_ids).fetchall()
        if len(rows) != len(item_ids):
            raise AppError("所选条目中包含不存在条目，请刷新后重试。", 409, "items_unavailable")
        by_id = {row["id"]: row for row in rows}
        if any(int(by_id[item_id]["row_version"]) != item_versions[item_id] for item_id in item_ids):
            raise AppError("所选条目版本已变化，请刷新后重试。", 409, "stale_version")
        if any(by_id[item_id]["status"] != "pending_reimbursement" for item_id in item_ids):
            raise AppError("所选条目已被处理，请刷新后重试。", 409, "items_unavailable")
        project_id = requested_project_id or by_id[item_ids[0]]["project_id"]
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            raise AppError("报销项目编号无效。", 400, "invalid_project")
        if not project_exists(db, project_id):
            raise AppError("报销项目不存在或已停用。", 400, "invalid_project")
        now = utc_now()
        try:
            cursor = db.execute(
                """INSERT INTO reimbursement_batches(
                       name,project_id,purpose,notes,status,total_amount,total_amount_cents,created_at,updated_at
                   ) VALUES(?,?,?,?, 'draft',0,0,?,?)""",
                (name, project_id, purpose, notes, now, now),
            )
            batch_id = cursor.lastrowid
            db.executemany(
                "INSERT INTO batch_items(batch_id,expense_item_id,sort_order) VALUES(?,?,?)",
                [(batch_id, item_id, index) for index, item_id in enumerate(item_ids)],
            )
            new_item_versions = {}
            for item_id in item_ids:
                updated = db.execute(
                    """UPDATE expense_items SET status='in_batch',updated_at=?,row_version=row_version+1
                       WHERE id=? AND status='pending_reimbursement' AND row_version=? RETURNING row_version""",
                    (now, item_id, item_versions[item_id]),
                ).fetchone()
                if not updated:
                    raise AppError("所选条目状态已变化，请刷新后重试。", 409, "items_unavailable")
                new_item_versions[item_id] = int(updated["row_version"])
            recalculate_batch_total(db, batch_id, bump_version=False)
            audit(db, "batch", batch_id, "created", {"item_ids": item_ids})
            for item_id in item_ids:
                audit(db, "item", item_id, "added_to_batch", {"batch_id": batch_id})
        except AppError:
            raise
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise AppError("所选条目已进入其他报销包，请刷新后重试。", 409, "items_unavailable")
            raise
        batch = serialize_batch(db, batch_id)
        refs = [{"type": "reimbursement_batch", "id": batch_id, "version": batch["version"]}]
        refs.extend(
            {"type": "invoice_item", "id": item_id, "version": new_item_versions[item_id]}
            for item_id in item_ids
        )
        return {"batch": batch}, operation_result(refs)

    business_payload, safe_result, replayed, status = _database_operation(
        db, "create_reimbursement_batch", parameters, apply, http_status=201
    )
    return jsonify(_operation_response(business_payload, safe_result, replayed)), status


@api.get("/batches")
def list_batches():
    db = get_db()
    status = request.args.get("status")
    params = []
    filters = []
    if status:
        if status not in ("draft", "submitted", "reimbursed"):
            raise AppError("报销包状态无效。", 400, "invalid_status")
        filters.append("b.status=?")
        params.append(status)
    project_id = request.args.get("project_id")
    if project_id not in (None, ""):
        try:
            parsed_project_id = int(project_id)
        except ValueError:
            raise AppError("筛选参数 project_id 无效。", 400, "invalid_filter")
        if parsed_project_id <= 0 or str(parsed_project_id) != project_id.strip():
            raise AppError("筛选参数 project_id 无效。", 400, "invalid_filter")
        filters.append("b.project_id=?")
        params.append(parsed_project_id)
    clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    if request.args.get("cursor") is not None or request.args.get("limit") is not None:
        limit = parse_limit(request.args.get("limit"))
        filters_digest = filter_hash(
            "batches", {"status": status or None, "project_id": project_id or None}
        )
        supplied_cursor = request.args.get("cursor")
        if supplied_cursor:
            page_cursor = decode_cursor(supplied_cursor, "batches", filters_digest)
        else:
            snapshot = db.execute(
                f"SELECT COALESCE(MAX(b.id),0) AS max_id FROM reimbursement_batches b {clause}", params
            ).fetchone()["max_id"]
            page_cursor = KeysetCursor(snapshot_max_id=int(snapshot))
        page_clauses = []
        page_params = []
        page_clauses.extend(filters)
        page_params.extend(params)
        page_clauses.append("b.id<=?")
        page_params.append(page_cursor.snapshot_max_id)
        if page_cursor.created_at is not None:
            page_clauses.append("(b.created_at<? OR (b.created_at=? AND b.id<?))")
            page_params.extend([page_cursor.created_at, page_cursor.created_at, page_cursor.row_id])
        rows = db.execute(
            f"""SELECT b.*,p.name AS project_name,p.code AS project_code FROM reimbursement_batches b
                LEFT JOIN projects p ON p.id=b.project_id WHERE {' AND '.join(page_clauses)}
                ORDER BY b.created_at DESC,b.id DESC LIMIT ?""",
            [*page_params, limit + 1],
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = encode_cursor(
                "batches",
                filters_digest,
                KeysetCursor(page_cursor.snapshot_max_id, last["created_at"], int(last["id"])),
            )
        return jsonify(
            {
                "batches": [serialize_batch(db, row) for row in visible],
                "meta": {"pagination": {"next_cursor": next_cursor, "has_more": has_more}},
            }
        )
    rows = db.execute(
        f"""SELECT b.*,p.name AS project_name,p.code AS project_code FROM reimbursement_batches b
            LEFT JOIN projects p ON p.id=b.project_id {clause} ORDER BY b.updated_at DESC,b.id DESC""",
        params,
    ).fetchall()
    return jsonify({"batches": [serialize_batch(db, row) for row in rows]})


@api.get("/batches/<int:batch_id>")
def get_batch(batch_id: int):
    return jsonify({"batch": serialize_batch(get_db(), batch_id)})


@api.patch("/batches/<int:batch_id>")
def update_batch(batch_id: int):
    db = get_db()
    current = serialize_batch(db, batch_id)
    if current["status"] != "draft":
        raise AppError("已提交报销包不可修改。", 409, "batch_locked")
    _assert_batch_mutable(current)
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    try:
        project_id = int(payload.get("project_id", current["project_id"]))
    except (TypeError, ValueError):
        raise AppError("报销项目编号无效。", 400, "invalid_project")
    if not project_exists(db, project_id):
        raise AppError("报销项目不存在或已停用。", 400, "invalid_project")
    name = _bounded_text(payload.get("name", current["name"]), "报销包名称", 120, required=True)
    purpose = _bounded_text(payload.get("purpose", current["purpose"]), "用途说明", 2000)
    notes = _bounded_text(payload.get("notes", current["notes"]), "备注", 4000)
    with transaction(db):
        _require_batch_version(db, batch_id, expected_version)
        changed = db.execute(
            """UPDATE reimbursement_batches SET name=?,project_id=?,purpose=?,notes=?,updated_at=?,row_version=row_version+1
               WHERE id=? AND export_token IS NULL AND row_version=? RETURNING row_version""",
            (
                name, project_id,
                purpose, notes,
                utc_now(), batch_id, expected_version,
            ),
        ).fetchone()
        if not changed:
            raise AppError("报销包已变化，请刷新后重试。", 409, "stale_version")
        audit(db, "batch", batch_id, "edited", payload)
    return jsonify({"batch": serialize_batch(db, batch_id)})


@api.delete("/batches/<int:batch_id>")
def delete_batch(batch_id: int):
    db = get_db()
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    batch = serialize_batch(db, batch_id)
    item_ids = [item["id"] for item in batch["items"]]

    if batch["status"] == "draft":
        _assert_batch_mutable(batch)
        superseded_archive = Path(batch["superseded_archive_path"]).resolve() if batch.get("superseded_archive_path") else None
        archive_root = None
        if superseded_archive:
            setting = db.execute("SELECT value FROM settings WHERE key='archive_root'").fetchone()
            archive_root = Path(setting["value"]).resolve()
            if superseded_archive == archive_root or not superseded_archive.is_relative_to(archive_root):
                raise AppError("旧版归档不在当前归档目录内，请先手工处理后再删除报销包。", 409, "unsafe_archive_path")
        snapshot_name = _backup_before_delete()
        with transaction(db):
            _require_batch_version(db, batch_id, expected_version)
            if superseded_archive:
                enqueue_file_cleanup(
                    db,
                    superseded_archive,
                    "archive_tree",
                    archive_root,
                    "删除退回修正中的报销包后回收旧版归档",
                )
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                db.execute(
                    f"""UPDATE expense_items SET status='pending_reimbursement',updated_at=?,row_version=row_version+1
                        WHERE id IN ({placeholders})""",
                    [utc_now(), *item_ids],
                )
                for item_id in item_ids:
                    audit(db, "item", item_id, "batch_deleted_returned_to_pool", {"batch_id": batch_id})
            db.execute("DELETE FROM audit_logs WHERE object_type='batch' AND object_id=?", (batch_id,))
            db.execute("DELETE FROM reimbursement_batches WHERE id=?", (batch_id,))
            audit(
                db,
                "system",
                0,
                "draft_batch_deleted",
                {"batch_id": batch_id, "name": batch["name"], "item_ids": item_ids, "snapshot": snapshot_name},
            )
        cleanup = process_file_cleanup_queue(db)
        return jsonify(
            {
                "deleted": True,
                "returned_item_ids": item_ids,
                "archive_deleted": bool(superseded_archive and not superseded_archive.exists()),
                "cleanup_warnings": cleanup["failed"],
            }
        )

    if str(payload.get("confirmation") or "") != batch["name"]:
        raise AppError("请输入完整的报销包名称以确认删除。", 409, "delete_confirmation_mismatch")
    delete_archive = payload.get("delete_archive") is True
    archive_target = None
    if delete_archive and batch["archive_path"]:
        archive_target = Path(batch["archive_path"]).resolve()
        setting = db.execute("SELECT value FROM settings WHERE key='archive_root'").fetchone()
        archive_root = Path(setting["value"]).resolve()
        try:
            archive_target.relative_to(archive_root)
        except ValueError:
            raise AppError("归档路径超出当前归档目录，拒绝自动删除。", 409, "unsafe_archive_path")
        if archive_target == archive_root:
            raise AppError("拒绝删除归档根目录。", 409, "unsafe_archive_path")

    snapshot_name = _backup_before_delete()

    placeholders = ",".join("?" for _ in item_ids) if item_ids else "NULL"
    merged_rows = (
        db.execute(f"SELECT id FROM expense_items WHERE merged_into_item_id IN ({placeholders})", item_ids).fetchall()
        if item_ids
        else []
    )
    all_item_ids = list(dict.fromkeys([*item_ids, *(row["id"] for row in merged_rows)]))
    all_placeholders = ",".join("?" for _ in all_item_ids) if all_item_ids else "NULL"
    managed_paths = [
        row["managed_path"]
        for row in db.execute(
            f"SELECT managed_path FROM attachments WHERE expense_item_id IN ({all_placeholders})",
            all_item_ids,
        ).fetchall()
    ] if all_item_ids else []

    with transaction(db):
        _require_batch_version(db, batch_id, expected_version)
        for path in managed_paths:
            enqueue_file_cleanup(
                db,
                path,
                "managed_file",
                current_app.config["IMPORT_DIR"],
                "删除历史报销记录后回收受管附件",
            )
        if archive_target:
            enqueue_file_cleanup(
                db,
                archive_target,
                "archive_tree",
                archive_root,
                "删除历史报销记录后回收归档",
            )
        db.execute("DELETE FROM audit_logs WHERE object_type='batch' AND object_id=?", (batch_id,))
        if all_item_ids:
            db.execute(
                f"DELETE FROM audit_logs WHERE object_type='item' AND object_id IN ({all_placeholders})",
                all_item_ids,
            )
        db.execute("DELETE FROM reimbursement_batches WHERE id=?", (batch_id,))
        if all_item_ids:
            db.execute(f"DELETE FROM expense_items WHERE id IN ({all_placeholders})", all_item_ids)
        audit(
            db,
            "system",
            0,
            "history_batch_deleted",
            {
                "batch_id": batch_id,
                "name": batch["name"],
                "status": batch["status"],
                "item_ids": item_ids,
                "archive_requested": delete_archive,
                "snapshot": snapshot_name,
            },
        )

    cleanup = process_file_cleanup_queue(db)
    cleanup_warnings = [entry["code"] for entry in cleanup["failed"]]
    archive_deleted = bool(archive_target and not archive_target.exists())
    return jsonify(
        {
            "deleted": True,
            "deleted_item_ids": item_ids,
            "archive_deleted": archive_deleted,
            "cleanup_warnings": cleanup_warnings,
        }
    )


@api.post("/batches/<int:batch_id>/items/<int:item_id>/remove")
def remove_batch_item(batch_id: int, item_id: int):
    db = get_db()
    payload = body()
    expected_batch_version = _integer_field(payload, "expected_version")
    expected_item_version = _integer_field(payload, "expected_item_version")
    batch = serialize_batch(db, batch_id)
    if batch["status"] != "draft":
        raise AppError("已提交报销包不可移除条目。", 409, "batch_locked")
    _assert_batch_mutable(batch)
    linked = db.execute("SELECT 1 FROM batch_items WHERE batch_id=? AND expense_item_id=?", (batch_id, item_id)).fetchone()
    if not linked:
        raise AppError("条目不在该报销包中。", 404, "batch_item_not_found")
    with transaction(db):
        _require_batch_version(db, batch_id, expected_batch_version)
        item_context = _require_item_version(
            db, item_id, expected_item_version, expected_batch_version
        )
        if item_context["batch_id"] != batch_id:
            raise AppError("条目不在该报销包中。", 404, "batch_item_not_found")
        db.execute("DELETE FROM batch_items WHERE batch_id=? AND expense_item_id=?", (batch_id, item_id))
        db.execute("UPDATE expense_items SET status='pending_reimbursement',updated_at=? WHERE id=?", (utc_now(), item_id))
        _touch_item(db, item_id)
        audit(db, "batch", batch_id, "item_removed", {"item_id": item_id})
        audit(db, "item", item_id, "removed_from_batch", {"batch_id": batch_id})
        remaining = db.execute("SELECT COUNT(*) AS n FROM batch_items WHERE batch_id=?", (batch_id,)).fetchone()["n"]
        if remaining:
            recalculate_batch_total(db, batch_id)
        else:
            audit(db, "batch", batch_id, "empty_batch_removed", {})
            db.execute("DELETE FROM reimbursement_batches WHERE id=?", (batch_id,))
    if not remaining:
        return jsonify({"batch": None, "deleted": True})
    return jsonify({"batch": serialize_batch(db, batch_id), "deleted": False})


@api.post("/batches/<int:batch_id>/export")
def export_batch_route(batch_id: int):
    payload = body()
    expected_version = payload.get("expected_version")
    expected_requirements_version = payload.get("expected_requirements_version")
    confirmation_name = payload.get("confirmation_name")
    operation_id = _operation_id()
    supplied_tool = request.headers.get("X-Invoice-Agent-Tool")
    if not operation_id and supplied_tool:
        raise AppError("Agent 写操作必须提供 Idempotency-Key。", 400, "operation_id_required")
    parameters = {
        "batch_id": batch_id,
        "payload": payload,
    }
    fingerprint = (
        request_fingerprint("export_reimbursement_batch", parameters) if operation_id else None
    )
    return jsonify(
        export_batch(
            get_db(),
            batch_id,
            expected_version=expected_version,
            expected_requirements_version=expected_requirements_version,
            confirmation_name=confirmation_name,
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            request_fields=set(payload),
            agent_tool_valid=(
                supplied_tool == "export_reimbursement_batch" if operation_id else True
            ),
        )
    )


@api.post("/batches/<int:batch_id>/reopen")
def reopen_batch(batch_id: int):
    db = get_db()
    batch = serialize_batch(db, batch_id)
    if batch["status"] not in ("submitted", "reimbursed"):
        raise AppError("只有历史报销包可以退回修正。", 409, "invalid_batch_status")
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    if str(payload.get("confirmation") or "") != batch["name"]:
        raise AppError("请输入完整的报销包名称以确认退回修正。", 409, "reopen_confirmation_mismatch")
    snapshot_name = _backup_before_delete()
    now = utc_now()
    with transaction(db):
        _require_batch_version(db, batch_id, expected_version)
        changed = db.execute(
            """UPDATE reimbursement_batches SET status='draft',
                   superseded_archive_path=archive_path,superseded_pdf_path=pdf_path,
                   archive_path=NULL,pdf_path=NULL,export_time=NULL,submitted_date=NULL,reimbursed_date=NULL,
                   reimbursement_notes='',export_error=NULL,updated_at=?,row_version=row_version+1
               WHERE id=? AND status IN ('submitted','reimbursed') AND export_token IS NULL AND row_version=?""",
            (now, batch_id, expected_version),
        )
        if changed.rowcount != 1:
            raise AppError("报销包状态已变化，请刷新后重试。", 409, "batch_changed")
        db.execute(
            """UPDATE expense_items SET status='in_batch',submitted_at=NULL,reimbursed_at=NULL,updated_at=?,row_version=row_version+1
               WHERE id IN (SELECT expense_item_id FROM batch_items WHERE batch_id=?)""",
            (now, batch_id),
        )
        audit(
            db,
            "batch",
            batch_id,
            "reopened_for_correction",
            {"from": batch["status"], "snapshot": snapshot_name, "previous_archive": batch["archive_path"]},
        )
        for item in batch["items"]:
            audit(db, "item", item["id"], "reopened_for_correction", {"batch_id": batch_id, "from": item["status"]})
        recalculate_batch_total(db, batch_id, bump_version=False)
    return jsonify({"batch": serialize_batch(db, batch_id), "previous_archive_preserved": True})


@api.post("/batches/<int:batch_id>/mark-reimbursed")
def mark_reimbursed(batch_id: int):
    db = get_db()
    batch = serialize_batch(db, batch_id)
    if batch["status"] != "submitted":
        raise AppError("只有已提交报销包可以标记为已报销。", 409, "invalid_batch_status")
    payload = body()
    expected_version = _integer_field(payload, "expected_version")
    reimbursed_date = str(payload.get("reimbursed_date") or date.today().isoformat())
    try:
        date.fromisoformat(reimbursed_date)
    except ValueError:
        raise AppError("到账日期格式不正确。", 400, "invalid_date")
    notes = _bounded_text(payload.get("notes"), "到账备注", 2000)
    now = utc_now()
    with transaction(db):
        _require_batch_version(db, batch_id, expected_version)
        changed = db.execute(
            """UPDATE reimbursement_batches SET status='reimbursed',reimbursed_date=?,reimbursement_notes=?,updated_at=?,row_version=row_version+1
               WHERE id=? AND status='submitted' AND row_version=?""",
            (reimbursed_date, notes, now, batch_id, expected_version),
        )
        if changed.rowcount != 1:
            raise AppError("报销包状态已变化，请刷新后重试。", 409, "batch_changed")
        db.execute(
            """UPDATE expense_items SET status='reimbursed',reimbursed_at=?,updated_at=?,row_version=row_version+1
               WHERE id IN (SELECT expense_item_id FROM batch_items WHERE batch_id=?)""",
            (now, now, batch_id),
        )
        audit(db, "batch", batch_id, "marked_reimbursed", {"date": reimbursed_date, "notes": notes})
        for item in batch["items"]:
            audit(db, "item", item["id"], "status_changed", {"from": "submitted", "to": "reimbursed", "batch_id": batch_id})
    return jsonify({"batch": serialize_batch(db, batch_id)})


@api.get("/history")
def history():
    db = get_db()
    clauses = ["b.status IN ('submitted','reimbursed')"]
    params: list = []
    status = request.args.get("status")
    if status:
        if status not in ("submitted", "reimbursed"):
            raise AppError("历史状态筛选无效。", 400, "invalid_status")
        clauses.append("b.status=?")
        params.append(status)
    project_id = request.args.get("project_id")
    if project_id:
        try:
            project_id = int(project_id)
        except ValueError:
            raise AppError("筛选参数 project_id 无效。", 400, "invalid_filter")
        clauses.append("b.project_id=?")
        params.append(project_id)
    range_filters = [
        ("submitted_from", "b.submitted_date>=?"), ("submitted_to", "b.submitted_date<=?"),
        ("reimbursed_from", "b.reimbursed_date>=?"), ("reimbursed_to", "b.reimbursed_date<=?"),
        ("amount_min", "b.total_amount_cents>=?"), ("amount_max", "b.total_amount_cents<=?"),
    ]
    for arg, clause in range_filters:
        value = request.args.get(arg)
        if value:
            if arg.startswith("amount"):
                try:
                    value = money_to_cents(value)
                except (ValueError, AppError):
                    raise AppError(f"筛选参数 {arg} 无效。", 400, "invalid_filter")
            else:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise AppError(f"筛选参数 {arg} 无效。", 400, "invalid_filter")
            clauses.append(clause)
            params.append(value)
    search = _bounded_text(request.args.get("search", ""), "搜索关键词", 200)
    if search:
        clauses.append(
            """(b.name LIKE ? OR b.purpose LIKE ? OR EXISTS(
               SELECT 1 FROM batch_items bx JOIN expense_items ix ON ix.id=bx.expense_item_id
               WHERE bx.batch_id=b.id AND (ix.merchant LIKE ? OR ix.purpose LIKE ?)))"""
        )
        params.extend([f"%{search}%"] * 4)
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        raise AppError("分页参数无效。", 400, "invalid_pagination")
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM reimbursement_batches b WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()["n"]
    rows = db.execute(
        f"""SELECT b.*,p.name AS project_name,p.code AS project_code FROM reimbursement_batches b
            LEFT JOIN projects p ON p.id=b.project_id WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(b.reimbursed_date,b.submitted_date) DESC,b.id DESC LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()
    return jsonify({"batches": [serialize_batch(db, row) for row in rows], "count": total, "limit": limit, "offset": offset})


@api.get("/batches/<int:batch_id>/pdf")
def download_batch_pdf(batch_id: int):
    batch = serialize_batch(get_db(), batch_id, detail=False)
    if not batch["pdf_path"] or not Path(batch["pdf_path"]).is_file():
        raise AppError("归档 PDF 不存在。", 404, "pdf_not_found")
    return send_file(batch["pdf_path"], as_attachment=False, download_name=Path(batch["pdf_path"]).name, mimetype="application/pdf")


@api.post("/batches/<int:batch_id>/open")
def open_batch_artifact(batch_id: int):
    batch = serialize_batch(get_db(), batch_id, detail=False)
    kind = body().get("kind", "pdf")
    if kind not in ("pdf", "archive"):
        raise AppError("归档目标类型无效。", 400, "invalid_artifact_kind")
    path_value = batch["archive_path"] if kind == "archive" else batch["pdf_path"]
    if not path_value or not Path(path_value).exists():
        raise AppError("归档目标不存在。", 404, "artifact_not_found")
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise AppError("当前系统不支持直接打开，请使用下载入口。", 501, "open_not_supported")
    os.startfile(path_value)  # type: ignore[attr-defined]
    return jsonify({"opened": True, "kind": kind})


@api.get("/settings")
def get_settings():
    db = get_db()
    return jsonify(
        {
            "settings": {row["key"]: row["value"] for row in db.execute("SELECT * FROM settings").fetchall()},
            "projects": [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY enabled DESC,name,id").fetchall()],
            "rules": get_rules(db),
            "requirements_version": get_requirements_version(db),
            "materials": get_materials(db),
            "recognition": recognition_status(),
            "storage": storage_status(
                current_app.config["DATA_DIR"],
                current_app.config["DATABASE"],
                current_app.config["BACKUP_DIR"],
                current_app.config["BACKUP_RETENTION"],
            ),
            "reconciliation": storage_reconciliation(db),
            "integrity": _data_integrity_report(db),
        }
    )


@api.post("/system/backup")
def create_backup():
    snapshot = create_database_backup(
        current_app.config["DATABASE"],
        current_app.config["BACKUP_DIR"],
        retention=current_app.config["BACKUP_RETENTION"],
        manual=True,
    )
    audit(get_db(), "system", 0, "manual_database_backup", {"name": snapshot.name})
    get_db().commit()
    return jsonify(
        {
            "created": True,
            "storage": storage_status(
                current_app.config["DATA_DIR"],
                current_app.config["DATABASE"],
                current_app.config["BACKUP_DIR"],
                current_app.config["BACKUP_RETENTION"],
            ),
        }
    )


@api.post("/system/open-data-directory")
def open_data_directory():
    data_dir = Path(current_app.config["DATA_DIR"])
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise AppError("当前系统不支持直接打开数据目录。", 501, "open_not_supported")
    os.startfile(str(data_dir))  # type: ignore[attr-defined]
    return jsonify({"opened": True})


@api.put("/settings/archive-root")
def update_archive_root():
    db = get_db()
    root = ensure_archive_root(str(body().get("archive_root") or ""))
    with transaction(db):
        db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('archive_root',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (str(root), utc_now()),
        )
        audit(db, "settings", 0, "archive_root_changed", {"path": str(root)})
    return jsonify({"archive_root": str(root)})


@api.post("/projects")
def create_project():
    db = get_db()
    payload = body()
    name = _bounded_text(payload.get("name"), "项目名称", 120, required=True)
    code = _bounded_text(payload.get("code"), "项目编号", 60)
    notes = _bounded_text(payload.get("notes"), "项目备注", 1000)
    now = utc_now()
    try:
        with transaction(db):
            cursor = db.execute(
                "INSERT INTO projects(name,code,notes,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (name, code, notes, 1, now, now),
            )
            audit(db, "project", cursor.lastrowid, "created", payload)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise AppError("同名项目已存在。", 409, "project_exists")
        raise
    return jsonify({"project": dict(db.execute("SELECT * FROM projects WHERE id=?", (cursor.lastrowid,)).fetchone())}), 201


@api.patch("/projects/<int:project_id>")
def update_project(project_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise AppError("项目不存在。", 404, "project_not_found")
    payload = body()
    name = _bounded_text(payload.get("name", row["name"]), "项目名称", 120, required=True)
    code = _bounded_text(payload.get("code", row["code"]), "项目编号", 60)
    notes = _bounded_text(payload.get("notes", row["notes"]), "项目备注", 1000)
    enabled = 1 if payload.get("enabled", bool(row["enabled"])) else 0
    if row["enabled"] and not enabled:
        active_count = db.execute("SELECT COUNT(*) AS n FROM projects WHERE enabled=1").fetchone()["n"]
        if active_count <= 1:
            raise AppError("至少需要保留一个启用的报销项目。", 409, "last_active_project")
    try:
        with transaction(db):
            db.execute(
                "UPDATE projects SET name=?,code=?,notes=?,enabled=?,updated_at=? WHERE id=?",
                (
                    name, code, notes,
                    enabled, utc_now(), project_id,
                ),
            )
            audit(db, "project", project_id, "updated", payload)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise AppError("同名项目已存在。", 409, "project_exists")
        raise
    return jsonify({"project": dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())})


@api.put("/material-rules")
def update_rules():
    db = get_db()
    payload = body()
    expected_requirements_version = _integer_field(
        payload, "expected_requirements_version", required_code="requirements_version_required"
    )
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise AppError("材料规则不能为空。", 400, "rules_required")
    normalized = []
    allowed_materials = set(MATERIAL_META)
    for index, rule in enumerate(rules):
        try:
            low = float(rule["min_amount"])
            high = None if rule.get("max_amount") in (None, "") else float(rule["max_amount"])
        except (KeyError, TypeError, ValueError):
            raise AppError("材料规则金额格式无效。", 400, "invalid_rule")
        required = list(dict.fromkeys(rule.get("required") or []))
        if not math.isfinite(low) or (high is not None and not math.isfinite(high)):
            raise AppError("材料规则金额必须是有限数值。", 400, "invalid_rule")
        if low < 0 or (high is not None and high <= low) or not required or not set(required) <= allowed_materials:
            raise AppError("材料规则区间或材料类型无效。", 400, "invalid_rule")
        normalized.append({"label": str(rule.get("label") or f"规则 {index+1}"), "min": low, "max": high, "required": required})
    normalized.sort(key=lambda entry: entry["min"])
    if normalized[0]["min"] != 0 or normalized[-1]["max"] is not None:
        raise AppError("规则必须从 0 开始且最后一个区间无上限。", 400, "rule_coverage_gap")
    for previous, current in zip(normalized, normalized[1:]):
        if previous["max"] != current["min"]:
            raise AppError("规则区间必须连续且不能重叠。", 400, "rule_coverage_gap")
    with transaction(db):
        current_requirements_version = get_requirements_version(db)
        if current_requirements_version != expected_requirements_version:
            raise AppError("材料规则已变化，请刷新后重试。", 409, "stale_requirements_version")
        db.execute("DELETE FROM material_rules")
        db.executemany(
            "INSERT INTO material_rules(label,min_amount,max_amount,required_json,sort_order,updated_at) VALUES(?,?,?,?,?,?)",
            [(entry["label"], entry["min"], entry["max"], json_dump(entry["required"]), index, utc_now()) for index, entry in enumerate(normalized)],
        )
        changed = db.execute(
            """UPDATE requirements_state SET requirements_version=requirements_version+1,updated_at=?
               WHERE id=1 AND requirements_version=? RETURNING requirements_version""",
            (utc_now(), expected_requirements_version),
        ).fetchone()
        if not changed:
            raise AppError("材料规则已变化，请刷新后重试。", 409, "stale_requirements_version")
        audit(db, "settings", 0, "material_rules_changed", {"rules": normalized})
    return jsonify({"rules": get_rules(db), "requirements_version": int(changed["requirements_version"])})


@api.put("/material-labels")
def update_material_labels():
    db = get_db()
    entries = body().get("materials")
    if not isinstance(entries, list):
        raise AppError("材料名称列表无效。", 400, "invalid_material_labels")
    labels = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("code") not in MATERIAL_META:
            raise AppError("材料类型无效。", 400, "invalid_material_labels")
        label = str(entry.get("label") or "").strip()[:30]
        if not label:
            raise AppError("材料名称不能为空。", 400, "invalid_material_labels")
        labels[entry["code"]] = label
    if set(labels) != set(MATERIAL_META):
        raise AppError("请完整填写所有材料名称。", 400, "invalid_material_labels")
    now = utc_now()
    with transaction(db):
        db.executemany(
            """INSERT INTO material_types(code,label,updated_at) VALUES(?,?,?)
               ON CONFLICT(code) DO UPDATE SET label=excluded.label,updated_at=excluded.updated_at""",
            [(code, label, now) for code, label in labels.items()],
        )
        audit(db, "settings", 0, "material_labels_changed", {"labels": labels})
    return jsonify({"materials": get_materials(db)})
