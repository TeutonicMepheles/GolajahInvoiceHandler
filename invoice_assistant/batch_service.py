from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date
from pathlib import Path
from threading import Lock

from flask import current_app

from . import AppError
from .db import audit, connect_db, transaction, utc_now
from .domain import (
    batch_completeness,
    get_requirements_version,
    sanitize_component,
    serialize_audit_logs,
    serialize_item,
)
from .idempotency import (
    OperationReservation,
    ReplayedOperationFailure,
    complete_operation,
    fail_reserved_operation,
    operation_result,
    reserve_operation,
)
from .pdf_service import generate_material_package
from .storage import (
    claim_owned_export_directory_for_cleanup,
    enqueue_file_cleanup,
    ensure_archive_root,
    process_file_cleanup_queue,
    remove_claimed_export_directory,
    restore_owned_export_directory_after_cleanup,
)


EXPORT_OPERATION_NAME = "export_reimbursement_batch"
EXPORT_OWNER_MARKER = ".invoice-export-owner"
EXPORT_CLEANUP_REASON_PREFIX = "invoice-export-owned:"
EXPORT_FAILED_MESSAGE = "导出失败，未提交报销包。"
_EXPORT_LOCKS: dict[int, Lock] = {}
_EXPORT_LOCKS_GUARD = Lock()


def serialize_batch(db, row_or_id: int, detail: bool = True) -> dict:
    if isinstance(row_or_id, int):
        row = db.execute(
            """SELECT b.*, p.name AS project_name, p.code AS project_code
               FROM reimbursement_batches b LEFT JOIN projects p ON p.id=b.project_id WHERE b.id=?""",
            (row_or_id,),
        ).fetchone()
    else:
        row = row_or_id
    if not row:
        raise AppError("报销包不存在。", 404, "batch_not_found")
    result = {
        "id": row["id"],
        "version": int(row["row_version"]) if "row_version" in row.keys() else 0,
        "requirements_version": get_requirements_version(db),
        "name": row["name"],
        "project_id": row["project_id"],
        "project_name": row["project_name"] if "project_name" in row.keys() else None,
        "purpose": row["purpose"],
        "notes": row["notes"],
        "status": row["status"],
        "status_label": (
            "正在生成归档"
            if row["status"] == "draft" and "export_token" in row.keys() and row["export_token"]
            else {"draft": "处理中", "submitted": "已提交", "reimbursed": "已报销"}[row["status"]]
        ),
        "total_amount": row["total_amount"],
        "total_amount_cents": (
            row["total_amount_cents"]
            if "total_amount_cents" in row.keys()
            else round(row["total_amount"] * 100)
        ),
        "archive_path": row["archive_path"],
        "pdf_path": row["pdf_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "export_time": row["export_time"],
        "submitted_date": row["submitted_date"],
        "reimbursed_date": row["reimbursed_date"],
        "reimbursement_notes": row["reimbursement_notes"],
        "exporting": bool(row["export_token"]) if "export_token" in row.keys() else False,
        "export_started_at": row["export_started_at"] if "export_started_at" in row.keys() else None,
        "export_error": row["export_error"] if "export_error" in row.keys() else None,
        "superseded_archive_path": (
            row["superseded_archive_path"] if "superseded_archive_path" in row.keys() else None
        ),
        "pdf_download_url": f"/api/batches/{row['id']}/pdf" if row["pdf_path"] else None,
    }
    result["currency_totals"] = [{"currency": "CNY", "amount": result["total_amount"]}]
    # Summary DTOs (dashboard/list) must not silently turn an omitted value
    # into "incomplete". Completeness is therefore part of every batch shape.
    result["completeness"] = batch_completeness(db, row["id"])
    if detail:
        item_ids = [
            entry["expense_item_id"]
            for entry in db.execute(
                "SELECT expense_item_id FROM batch_items WHERE batch_id=? ORDER BY sort_order,expense_item_id",
                (row["id"],),
            ).fetchall()
        ]
        result["items"] = [serialize_item(db, item_id, include_audit=True) for item_id in item_ids]
        result["audit_logs"] = serialize_audit_logs(db, "batch", row["id"])
    return result


def recalculate_batch_total(db, batch_id: int, *, bump_version: bool = True) -> int:
    total_cents = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN i.currency='CNY' THEN i.amount_cents
                                    ELSE COALESCE(i.converted_amount_cents,0) END),0) AS total FROM expense_items i
           JOIN batch_items bi ON bi.expense_item_id=i.id WHERE bi.batch_id=?""",
        (batch_id,),
    ).fetchone()["total"]
    version_expression = "row_version+1" if bump_version else "row_version"
    row = db.execute(
        f"""UPDATE reimbursement_batches SET total_amount=?,total_amount_cents=?,updated_at=?,
                   row_version={version_expression} WHERE id=? RETURNING row_version""",
        (int(total_cents) / 100, int(total_cents), utc_now(), batch_id),
    ).fetchone()
    if not row:
        raise AppError("报销包不存在。", 404, "batch_not_found")
    return int(row["row_version"])


def _operation_response(payload: dict | None, safe_result: dict | None, *, replayed: bool) -> dict:
    if safe_result is None:
        return payload or {}
    if replayed:
        return {"operation_result": safe_result, "meta": {"replayed": True}}
    return {**(payload or {}), "operation_result": safe_result, "meta": {"replayed": False}}


def _validate_version(value: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AppError("必须提供有效的版本号。", 400, code)
    return value


def _validate_exportable(batch: dict) -> None:
    if not batch["items"]:
        raise AppError("报销包中没有条目。", 409, "empty_batch")
    invalid_statuses = [item for item in batch["items"] if item["status"] != "in_batch"]
    if invalid_statuses:
        raise AppError("报销包条目状态已变化，请刷新后重试。", 409, "batch_changed")
    if not batch["completeness"]["complete"]:
        names = [
            f"{item['merchant']}（{'、'.join(m['label'] for m in item['material']['missing'])}）"
            for item in batch["items"]
            if not item["material"]["complete"]
        ]
        raise AppError("材料未齐全，无法生成：" + "；".join(names), 409, "materials_incomplete")


def _safe_result_for_batch(db, batch_id: int, batch_version: int, *, artifact_available: bool) -> dict:
    refs = [{"type": "reimbursement_batch", "id": batch_id, "version": int(batch_version)}]
    refs.extend(
        {
            "type": "invoice_item",
            "id": int(row["id"]),
            "version": int(row["row_version"]),
        }
        for row in db.execute(
            """SELECT i.id,i.row_version FROM expense_items i
               JOIN batch_items bi ON bi.expense_item_id=i.id
               WHERE bi.batch_id=? ORDER BY bi.sort_order,i.id""",
            (batch_id,),
        ).fetchall()
    )
    return operation_result(refs, artifact_available=artifact_available)


def _artifact_exists(batch_row) -> bool:
    if not batch_row["archive_path"] or not batch_row["pdf_path"]:
        return False
    archive = Path(batch_row["archive_path"]).resolve()
    pdf = Path(batch_row["pdf_path"]).resolve()
    try:
        pdf.relative_to(archive)
    except ValueError:
        return False
    return archive.is_dir() and pdf.is_file()


def _planned_paths(batch_row, token: str) -> dict:
    setting = batch_row["archive_root"]
    if not setting:
        raise AppError("归档根目录设置缺失。", 503, "migration_required")
    archive_root = Path(setting).expanduser()
    if not archive_root.is_absolute():
        raise AppError("归档根目录必须是绝对路径。", 400, "invalid_archive_root")
    archive_root = archive_root.resolve()
    temp_root = Path(current_app.config["TEMP_DIR"]).resolve()
    project = sanitize_component(batch_row["project_name"], "未分项目")
    batch_name = sanitize_component(batch_row["name"], f"报销包{batch_row['id']}")
    folder_base = f"{date.today().isoformat()}_{project}_{batch_name}_{batch_row['id']}_{token}"
    final_dir = archive_root / folder_base
    temp_dir = temp_root / f"export_{batch_row['id']}_{token}"
    pdf_name = f"报销材料包_{project}_{batch_name}.pdf"
    return {
        "archive_root": archive_root,
        "temp_root": temp_root,
        "temp_dir": temp_dir,
        "final_dir": final_dir,
        "temp_pdf": temp_dir / pdf_name,
        "final_pdf": final_dir / pdf_name,
        "publish_dir": archive_root / f".{folder_base}.publishing",
        "pdf_name": pdf_name,
    }


def _export_owner_text(token: str) -> str:
    return f"invoice-export-v1\n{token}\n"


def _owned_export_directory(path: Path, token: str) -> bool:
    try:
        value = (path / EXPORT_OWNER_MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return value == _export_owner_text(token)


def _native_long_path(path: Path) -> str:
    """Use Win32 extended-length syntax only at filesystem call boundaries."""
    value = str(path.resolve())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _claim_export_directory(path: Path, token: str) -> None:
    created = False
    try:
        path.mkdir(parents=False, exist_ok=False)
        created = True
        with (path / EXPORT_OWNER_MARKER).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_export_owner_text(token))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise AppError("导出目标已存在，拒绝覆盖。", 409, "export_destination_exists")
    except OSError as exc:
        if created:
            try:
                path.rmdir()
            except OSError:
                pass
        raise AppError("无法建立导出发布目录。", 500, "export_publish_failed") from exc


def _publishing_path(operation) -> Path:
    final = Path(operation["final_path"]).resolve()
    return final.parent / f".{final.name}.publishing"


def _atomic_publish_export(temp_dir: Path, final_dir: Path, token: str) -> None:
    """Copy cross-volume work into a claimed sibling, then no-replace rename."""
    publish_dir = final_dir.parent / f".{final_dir.name}.publishing"
    if publish_dir.exists():
        if not _owned_export_directory(publish_dir, token):
            raise AppError("导出发布目标已存在，拒绝覆盖。", 409, "export_destination_exists")
        # This function is not used for restart recovery. An owned partial copy
        # can only be from this still-running attempt and is safe to rebuild.
        shutil.rmtree(_native_long_path(publish_dir))
    _claim_export_directory(publish_dir, token)
    try:
        for source in temp_dir.iterdir():
            if source.name == EXPORT_OWNER_MARKER:
                continue
            target = publish_dir / source.name
            if source.is_dir():
                shutil.copytree(_native_long_path(source), _native_long_path(target))
            else:
                shutil.copy2(_native_long_path(source), _native_long_path(target))
        if final_dir.exists():
            raise AppError("导出目标已存在，拒绝覆盖。", 409, "export_destination_exists")
        try:
            # publish_dir and final_dir are siblings on the archive volume.
            # On Windows os.rename has no replace/nesting semantics.
            os.rename(_native_long_path(publish_dir), _native_long_path(final_dir))
        except OSError as exc:
            if final_dir.exists():
                raise AppError("导出目标已存在，拒绝覆盖。", 409, "export_destination_exists") from exc
            raise AppError("无法原子发布导出目录。", 500, "export_publish_failed") from exc
        if not _owned_export_directory(final_dir, token):
            raise AppError("导出目录 ownership 校验失败。", 409, "export_ownership_changed")
    except Exception:
        raise


def _load_export_operation(db, token: str):
    row = db.execute("SELECT * FROM export_operations WHERE token=?", (token,)).fetchone()
    if not row:
        raise AppError("导出恢复记录缺失。", 409, "export_operation_missing")
    return row


def _assert_export_snapshot(db, operation) -> tuple[object, list[int]]:
    if operation["working_batch_version"] is None or operation["expected_requirements_version"] is None:
        raise AppError("导出恢复记录缺少版本前置条件。", 409, "export_precondition_missing")
    batch = db.execute(
        "SELECT * FROM reimbursement_batches WHERE id=?",
        (operation["batch_id"],),
    ).fetchone()
    if not batch:
        raise AppError("报销包不存在。", 404, "batch_not_found")
    if (
        batch["status"] != "draft"
        or batch["export_token"] != operation["token"]
        or int(batch["row_version"]) != int(operation["working_batch_version"])
    ):
        raise AppError("导出期间报销包状态发生变化。", 409, "batch_changed_during_export")
    if get_requirements_version(db) != int(operation["expected_requirements_version"]):
        raise AppError("材料规则已变化，请刷新后重试。", 409, "stale_requirements_version")
    item_rows = db.execute(
        """SELECT i.id,i.status FROM expense_items i
           JOIN batch_items bi ON bi.expense_item_id=i.id
           WHERE bi.batch_id=? ORDER BY bi.sort_order,i.id""",
        (operation["batch_id"],),
    ).fetchall()
    if not item_rows:
        raise AppError("报销包中没有条目。", 409, "empty_batch")
    if any(row["status"] != "in_batch" for row in item_rows):
        raise AppError("导出期间条目状态发生变化。", 409, "batch_changed_during_export")
    completeness = batch_completeness(db, operation["batch_id"])
    if not completeness["complete"]:
        raise AppError("材料规则或附件已变化，报销包不再完整。", 409, "materials_incomplete")
    return batch, [int(row["id"]) for row in item_rows]


def _reserve_export(
    db,
    batch_id: int,
    *,
    expected_version: int,
    expected_requirements_version: int,
    confirmation_name: str,
    operation_id: str | None,
    fingerprint: str | None,
    request_fields: set[str] | None = None,
    agent_tool_valid: bool = True,
) -> dict:
    if bool(operation_id) != bool(fingerprint):
        raise AppError("operation ID 与请求指纹必须同时提供。", 400, "invalid_operation_contract")
    token = uuid.uuid4().hex
    terminal_error = None
    result = None
    with transaction(db):
        agent_reservation = None
        if operation_id:
            try:
                agent_reservation = reserve_operation(
                    db, operation_id, EXPORT_OPERATION_NAME, fingerprint
                )
            except ReplayedOperationFailure as exc:
                if exc.code == "export_failed":
                    exc.message = EXPORT_FAILED_MESSAGE
                    exc.args = (EXPORT_FAILED_MESSAGE,)
                raise
            if agent_reservation.replayed:
                return {
                    "immediate": _operation_response(
                        None, agent_reservation.operation_result, replayed=True
                    )
                }
            db.execute("SAVEPOINT agent_export_reservation")
        try:
            if operation_id and not agent_tool_valid:
                raise AppError(
                    "Agent tool header 与目标写操作不匹配。",
                    400,
                    "agent_tool_mismatch",
                )
            allowed_fields = {
                "expected_version",
                "expected_requirements_version",
                "confirmation_name",
            }
            if request_fields is not None and request_fields - allowed_fields:
                raise AppError("导出请求包含未知字段。", 400, "invalid_request")
            validated_version = _validate_version(expected_version, "version_required")
            validated_requirements_version = _validate_version(
                expected_requirements_version, "requirements_version_required"
            )
            if not isinstance(confirmation_name, str) or not confirmation_name:
                raise AppError(
                    "必须提供报销包确认名称。",
                    400,
                    "batch_confirmation_required",
                )
            batch = db.execute(
                """SELECT b.*,p.name AS project_name,s.value AS archive_root
                   FROM reimbursement_batches b
                   LEFT JOIN projects p ON p.id=b.project_id
                   LEFT JOIN settings s ON s.key='archive_root'
                   WHERE b.id=?""",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise AppError("报销包不存在。", 404, "batch_not_found")
            if confirmation_name != batch["name"]:
                raise AppError(
                    "请输入完全一致的报销包名称以确认导出。",
                    409,
                    "batch_confirmation_mismatch",
                )
            if int(batch["row_version"]) != validated_version:
                raise AppError("报销包已被其他操作修改，请刷新后重试。", 409, "stale_version")
            if get_requirements_version(db) != validated_requirements_version:
                raise AppError("材料规则已变化，请刷新后重试。", 409, "stale_requirements_version")

            if batch["status"] in {"submitted", "reimbursed"}:
                if not _artifact_exists(batch):
                    raise AppError("报销包已提交，但归档 PDF 丢失。", 409, "archive_missing")
                safe_result = _safe_result_for_batch(
                    db, batch_id, int(batch["row_version"]), artifact_available=True
                )
                if agent_reservation:
                    complete_operation(db, agent_reservation, safe_result)
                result = {
                    "immediate": _operation_response(
                        {"batch": serialize_batch(db, batch_id), "idempotent": True},
                        safe_result if agent_reservation else None,
                        replayed=False,
                    )
                }
            else:
                if batch["status"] != "draft":
                    raise AppError("当前报销包状态不可导出。", 409, "batch_locked")
                if batch["export_token"]:
                    raise AppError("报销包正在生成归档，请勿重复操作。", 409, "batch_exporting")

                snapshot = serialize_batch(db, batch_id)
                _validate_exportable(snapshot)
                paths = _planned_paths(batch, token)
                now = utc_now()
                reserved = db.execute(
                    """UPDATE reimbursement_batches
                       SET export_token=?,export_started_at=?,export_error=NULL,updated_at=?,row_version=row_version+1
                       WHERE id=? AND status='draft' AND export_token IS NULL AND row_version=?
                       RETURNING row_version""",
                    (token, now, now, batch_id, validated_version),
                ).fetchone()
                if not reserved:
                    raise AppError("报销包状态已变化，请刷新后重试。", 409, "stale_version")
                working_version = int(reserved["row_version"])
                db.execute(
                    """INSERT INTO export_operations(
                           token,batch_id,temp_path,final_path,pdf_path,state,error,created_at,updated_at,
                           operation_id,expected_batch_version,working_batch_version,
                           expected_requirements_version,phase,outcome,cleanup_error
                       ) VALUES(?,?,?,?,?,'preparing',NULL,?,?,?,?,?,?,'reserved',NULL,NULL)""",
                    (
                        token,
                        batch_id,
                        str(paths["temp_dir"]),
                        str(paths["final_dir"]),
                        str(paths["final_pdf"]),
                        now,
                        now,
                        operation_id,
                        validated_version,
                        working_version,
                        validated_requirements_version,
                    ),
                )
                result = {
                    "token": token,
                    "paths": paths,
                    "agent_reservation": agent_reservation,
                    "working_version": working_version,
                }
        except AppError as exc:
            if not agent_reservation:
                raise
            db.execute("ROLLBACK TO SAVEPOINT agent_export_reservation")
            db.execute("RELEASE SAVEPOINT agent_export_reservation")
            fail_reserved_operation(
                db,
                http_status=exc.status_code,
                error_code=exc.code,
                outcome="not_applied",
                reservation=agent_reservation,
            )
            terminal_error = exc
        else:
            if agent_reservation:
                db.execute("RELEASE SAVEPOINT agent_export_reservation")
    if terminal_error is not None:
        raise terminal_error
    return result


def _safe_child(path_value: str | Path, root_value: str | Path) -> tuple[Path, Path] | None:
    path = Path(path_value).resolve()
    root = Path(root_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path == root:
        return None
    return path, root


def _cleanup_operation_paths(operation, *, include_final: bool) -> tuple[list[tuple], list[str]]:
    pending: list[tuple] = []
    errors: list[str] = []
    token = str(operation["token"])
    temp_root = Path(current_app.config["TEMP_DIR"]).resolve()
    final_path = Path(operation["final_path"]).resolve()
    candidates = [
        (operation["temp_path"], "temporary_tree", temp_root),
        (_publishing_path(operation), "archive_tree", final_path.parent),
    ]
    if include_final:
        candidates.insert(0, (final_path, "archive_tree", final_path.parent))
    for path_value, kind, root in candidates:
        checked = _safe_child(path_value, root)
        if checked is None:
            errors.append("unsafe_cleanup_path")
            continue
        path, allowed_root = checked
        claimed: Path | None = None
        try:
            claimed = claim_owned_export_directory_for_cleanup(path, token)
            if claimed is None:
                continue
            remove_claimed_export_directory(claimed, token)
        except AppError as exc:
            if claimed is not None:
                restore_owned_export_directory_after_cleanup(claimed, path, token)
            errors.append(exc.code)
        except OSError:
            if claimed is not None:
                restore_owned_export_directory_after_cleanup(claimed, path, token)
            pending.append((path, kind, allowed_root))
            errors.append("cleanup_failed")
    return pending, errors


def _set_linked_operation_failure(
    db,
    operation_id: str | None,
    *,
    http_status: int,
    error_code: str,
    outcome: str,
) -> None:
    if not operation_id:
        return
    now = utc_now()
    db.execute(
        """UPDATE agent_operations
           SET status='failed',operation_result_json=NULL,http_status=?,error_code=?,error_outcome=?,
               updated_at=?,completed_at=?
           WHERE operation_id=? AND status<>'succeeded'""",
        (http_status, error_code, outcome, now, now, operation_id),
    )


def _release_export_reservation(
    db,
    token: str,
    *,
    error_code: str,
    http_status: int,
    cleanup_targets: list[tuple] | None = None,
) -> bool:
    with transaction(db):
        operation = _load_export_operation(db, token)
        now = utc_now()
        working_version = operation["working_batch_version"]
        if working_version is None:
            current = db.execute(
                """SELECT row_version FROM reimbursement_batches
                   WHERE id=? AND status='draft' AND export_token=?""",
                (operation["batch_id"], token),
            ).fetchone()
            if not current:
                return False
            working_version = int(current["row_version"])
        released = db.execute(
            """UPDATE reimbursement_batches
               SET export_token=NULL,export_started_at=NULL,export_error=?,updated_at=?,row_version=row_version+1
               WHERE id=? AND status='draft' AND export_token=? AND row_version=?
               RETURNING row_version""",
            (
                error_code,
                now,
                operation["batch_id"],
                token,
                working_version,
            ),
        ).fetchone()
        if not released:
            return False
        for path, kind, root in cleanup_targets or []:
            enqueue_file_cleanup(
                db,
                path,
                kind,
                root,
                f"{EXPORT_CLEANUP_REASON_PREFIX}{token}",
            )
        db.execute(
            """UPDATE export_operations
               SET state='failed',phase='failed',outcome='not_applied',error=?,cleanup_error=NULL,updated_at=?
               WHERE token=? AND state<>'completed'""",
            (error_code, now, token),
        )
        _set_linked_operation_failure(
            db,
            operation["operation_id"],
            http_status=http_status,
            error_code=error_code,
            outcome="not_applied",
        )
        audit(
            db,
            "batch",
            operation["batch_id"],
            "export_released",
            {"error_code": error_code, "version": int(released["row_version"])},
        )
    return True


def _mark_cleanup_pending(
    db,
    token: str,
    *,
    cleanup_targets: list[tuple],
    cleanup_errors: list[str],
) -> None:
    with transaction(db):
        operation = _load_export_operation(db, token)
        for path, kind, root in cleanup_targets:
            enqueue_file_cleanup(
                db,
                path,
                kind,
                root,
                f"{EXPORT_CLEANUP_REASON_PREFIX}{token}",
            )
        now = utc_now()
        cleanup_error = ",".join(sorted(set(cleanup_errors))) or "cleanup_failed"
        db.execute(
            """UPDATE export_operations
               SET state='failed',phase='cleanup_pending',outcome='unknown',error='export_cleanup_pending',
                   cleanup_error=?,updated_at=? WHERE token=? AND state<>'completed'""",
            (cleanup_error, now, token),
        )
        _set_linked_operation_failure(
            db,
            operation["operation_id"],
            http_status=500,
            error_code="export_cleanup_pending",
            outcome="unknown",
        )


def _handle_export_failure(
    db, token: str, exc: Exception, *, include_final: bool
) -> None:
    operation = _load_export_operation(db, token)
    cleanup_targets, cleanup_errors = _cleanup_operation_paths(
        operation, include_final=include_final
    )
    if include_final and cleanup_errors:
        _mark_cleanup_pending(
            db,
            token,
            cleanup_targets=cleanup_targets,
            cleanup_errors=cleanup_errors,
        )
        pending = AppError(
            "导出未提交，但已发布文件仍在受控清理中；结果暂时未知。",
            500,
            "export_cleanup_pending",
        )
        pending.outcome = "unknown"
        raise pending from exc

    error_code = exc.code if isinstance(exc, AppError) else "export_failed"
    http_status = exc.status_code if isinstance(exc, AppError) else 500
    if not _release_export_reservation(
        db,
        token,
        error_code=error_code,
        http_status=http_status,
        cleanup_targets=cleanup_targets,
    ):
        _mark_cleanup_pending(
            db,
            token,
            cleanup_targets=cleanup_targets,
            cleanup_errors=["reservation_state_changed"],
        )
        pending = AppError(
            "导出状态发生变化，结果暂时未知；请查询原 operation ID。",
            500,
            "export_outcome_unknown",
        )
        pending.outcome = "unknown"
        raise pending from exc


def _reservation_for_recovery(db, operation) -> OperationReservation | None:
    if not operation["operation_id"]:
        return None
    row = db.execute(
        "SELECT * FROM agent_operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    if not row or row["status"] != "in_progress":
        raise AppError("关联操作账本状态不允许完成恢复。", 409, "operation_state_changed")
    return OperationReservation(
        operation_id=row["operation_id"],
        operation_name=row["operation_name"],
        request_fingerprint=row["request_fingerprint"],
    )


def _queue_superseded_archive(db, batch, archive_root: Path) -> list[str]:
    superseded = batch["superseded_archive_path"]
    if not superseded:
        return []
    old_archive = Path(superseded).resolve()
    if not old_archive.exists():
        db.execute(
            "UPDATE reimbursement_batches SET superseded_archive_path=NULL,superseded_pdf_path=NULL WHERE id=?",
            (batch["id"],),
        )
        return []
    checked = _safe_child(old_archive, archive_root)
    if checked is None:
        return ["superseded_archive_not_cleaned"]
    enqueue_file_cleanup(
        db,
        old_archive,
        "archive_tree",
        archive_root,
        "修正历史报销后回收旧版归档",
    )
    db.execute(
        "UPDATE reimbursement_batches SET superseded_archive_path=NULL,superseded_pdf_path=NULL WHERE id=?",
        (batch["id"],),
    )
    return ["superseded_archive_cleanup_pending"]


def _finalize_export(
    db,
    token: str,
    *,
    agent_reservation: OperationReservation | None = None,
    recovered: bool = False,
) -> tuple[dict, dict]:
    with transaction(db):
        operation = _load_export_operation(db, token)
        if operation["state"] != "files_ready" or operation["phase"] != "files_ready":
            raise AppError("导出文件尚未进入可恢复提交阶段。", 409, "export_files_not_ready")
        batch, item_ids = _assert_export_snapshot(db, operation)
        final_dir = Path(operation["final_path"]).resolve()
        final_pdf = Path(operation["pdf_path"]).resolve()
        if (
            not _owned_export_directory(final_dir, token)
            or _safe_child(final_pdf, final_dir) is None
            or not final_dir.is_dir()
            or not final_pdf.is_file()
        ):
            raise AppError("导出文件尚未完整发布。", 409, "export_files_incomplete")
        now = utc_now()
        finalized = db.execute(
            """UPDATE reimbursement_batches
               SET status='submitted',archive_path=?,pdf_path=?,export_time=?,submitted_date=?,
                   export_token=NULL,export_started_at=NULL,export_error=NULL,updated_at=?,row_version=row_version+1
               WHERE id=? AND status='draft' AND export_token=? AND row_version=?
               RETURNING row_version""",
            (
                str(final_dir),
                str(final_pdf),
                now,
                date.today().isoformat(),
                now,
                operation["batch_id"],
                token,
                operation["working_batch_version"],
            ),
        ).fetchone()
        if not finalized:
            raise AppError(
                "导出期间报销包状态发生变化。", 409, "batch_changed_during_export"
            )
        updated_items = db.execute(
            """UPDATE expense_items
               SET status='submitted',submitted_at=?,updated_at=?,row_version=row_version+1
               WHERE status='in_batch' AND id IN (
                   SELECT expense_item_id FROM batch_items WHERE batch_id=?
               ) RETURNING id,row_version""",
            (now, now, operation["batch_id"]),
        ).fetchall()
        if len(updated_items) != len(item_ids):
            raise AppError(
                "导出期间条目状态发生变化。", 409, "batch_changed_during_export"
            )
        archive_root = final_dir.parent
        warning_codes = _queue_superseded_archive(db, batch, archive_root)
        action = "interrupted_export_recovered" if recovered else "exported_and_submitted"
        audit(db, "batch", operation["batch_id"], action, {"token": token})
        for item_id in item_ids:
            audit(
                db,
                "item",
                item_id,
                "status_changed",
                {"from": "in_batch", "to": "submitted", "batch_id": operation["batch_id"]},
            )
        db.execute(
            """UPDATE export_operations
               SET state='completed',phase='completed',outcome='applied',error=NULL,cleanup_error=NULL,updated_at=?
               WHERE token=? AND state<>'completed'""",
            (now, token),
        )
        safe_result = _safe_result_for_batch(
            db,
            operation["batch_id"],
            int(finalized["row_version"]),
            artifact_available=True,
        )
        if warning_codes:
            safe_result["warning_codes"] = warning_codes
        if operation["operation_id"]:
            reservation = agent_reservation or _reservation_for_recovery(db, operation)
            complete_operation(db, reservation, safe_result)
        batch_payload = serialize_batch(db, operation["batch_id"])
    return batch_payload, safe_result


def export_batch(
    db,
    batch_id: int,
    *,
    expected_version: int,
    expected_requirements_version: int,
    confirmation_name: str,
    operation_id: str | None = None,
    request_fingerprint: str | None = None,
    request_fields: set[str] | None = None,
    agent_tool_valid: bool = True,
) -> dict:
    with _EXPORT_LOCKS_GUARD:
        lock = _EXPORT_LOCKS.setdefault(batch_id, Lock())
    try:
        with lock:
            return _export_batch_locked(
                db,
                batch_id,
                expected_version=expected_version,
                expected_requirements_version=expected_requirements_version,
                confirmation_name=confirmation_name,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                request_fields=request_fields,
                agent_tool_valid=agent_tool_valid,
            )
    finally:
        with _EXPORT_LOCKS_GUARD:
            if _EXPORT_LOCKS.get(batch_id) is lock and not lock.locked():
                _EXPORT_LOCKS.pop(batch_id, None)


def _export_batch_locked(
    db,
    batch_id: int,
    *,
    expected_version: int,
    expected_requirements_version: int,
    confirmation_name: str,
    operation_id: str | None,
    request_fingerprint: str | None,
    request_fields: set[str] | None,
    agent_tool_valid: bool,
) -> dict:
    reservation = _reserve_export(
        db,
        batch_id,
        expected_version=expected_version,
        expected_requirements_version=expected_requirements_version,
        confirmation_name=confirmation_name,
        operation_id=operation_id,
        fingerprint=request_fingerprint,
        request_fields=request_fields,
        agent_tool_valid=agent_tool_valid,
    )
    if "immediate" in reservation:
        return reservation["immediate"]

    token = reservation["token"]
    paths = reservation["paths"]
    published = False
    try:
        ensure_archive_root(str(paths["archive_root"]))
        with transaction(db):
            operation = _load_export_operation(db, token)
            _assert_export_snapshot(db, operation)
            db.execute(
                "UPDATE export_operations SET phase='building',updated_at=? WHERE token=?",
                (utc_now(), token),
            )

        _claim_export_directory(paths["temp_dir"], token)
        material_dir = paths["temp_dir"] / "原始材料"
        os.makedirs(_native_long_path(material_dir), exist_ok=False)
        existing = serialize_batch(db, batch_id)
        _validate_exportable(existing)
        archive_files: dict[int, Path] = {}
        import_root = Path(current_app.config["IMPORT_DIR"]).resolve()
        for index, item in enumerate(existing["items"], 1):
            item_folder = material_dir / f"{index:03d}_{sanitize_component(item['merchant'], '未知商户')}"
            os.makedirs(_native_long_path(item_folder), exist_ok=True)
            for attachment in item["attachments"]:
                source_row = db.execute(
                    "SELECT managed_path FROM attachments WHERE id=?", (attachment["id"],)
                ).fetchone()
                source_check = _safe_child(source_row["managed_path"], import_root)
                if source_check is None:
                    raise AppError("受管附件路径无效。", 409, "unsafe_managed_attachment")
                source = source_check[0]
                if not source.is_file():
                    raise AppError("受管附件已丢失。", 409, "managed_attachment_missing")
                normalized_name = str(attachment["normalized_name"])
                if Path(normalized_name).name != normalized_name:
                    raise AppError("附件归档名称不安全。", 409, "unsafe_attachment_name")
                target = item_folder / normalized_name
                shutil.copy2(_native_long_path(source), _native_long_path(target))
                archive_files[attachment["id"]] = target
        pdf_report = generate_material_package(
            existing, existing["items"], archive_files, paths["temp_pdf"]
        )
        manifest = {
            "generated_at": utc_now(),
            "batch": existing,
            "pdf": {"name": paths["pdf_name"], **pdf_report},
        }
        (paths["temp_dir"] / "归档清单.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with transaction(db):
            operation = _load_export_operation(db, token)
            _assert_export_snapshot(db, operation)
            if paths["final_dir"].exists():
                raise AppError("导出目标已存在，拒绝覆盖。", 409, "export_destination_exists")
            db.execute(
                "UPDATE export_operations SET phase='prepublish_checked',updated_at=? WHERE token=?",
                (utc_now(), token),
            )
        try:
            _atomic_publish_export(paths["temp_dir"], paths["final_dir"], token)
        finally:
            # Existence alone is not ownership: a concurrent external creator may
            # have won the destination name between the check and atomic rename.
            published = _owned_export_directory(paths["final_dir"], token)
        with transaction(db):
            operation = _load_export_operation(db, token)
            _assert_export_snapshot(db, operation)
            db.execute(
                """UPDATE export_operations SET state='files_ready',phase='files_ready',updated_at=?
                   WHERE token=?""",
                (utc_now(), token),
            )
        batch, safe_result = _finalize_export(
            db,
            token,
            agent_reservation=reservation["agent_reservation"],
        )
    except Exception as exc:
        completed = db.execute(
            "SELECT state FROM export_operations WHERE token=?", (token,)
        ).fetchone()
        if completed and completed["state"] == "completed":
            # The financial commit and safe ledger snapshot are already durable.
            # A lost/failed response must be recovered by replay, never by deleting
            # the published artifact or reopening the batch.
            raise
        failure = (
            exc
            if isinstance(exc, AppError)
            else AppError(EXPORT_FAILED_MESSAGE, 500, "export_failed")
        )
        _handle_export_failure(db, token, failure, include_final=published)
        if failure is exc:
            raise
        raise failure from exc

    operation = _load_export_operation(db, token)
    work_cleanup_targets, work_cleanup_errors = _cleanup_operation_paths(
        operation, include_final=False
    )
    if work_cleanup_targets:
        with transaction(db):
            for path, kind, root in work_cleanup_targets:
                enqueue_file_cleanup(
                    db,
                    path,
                    kind,
                    root,
                    f"{EXPORT_CLEANUP_REASON_PREFIX}{token}",
                )
    try:
        cleanup = process_file_cleanup_queue(db)
    except Exception:
        cleanup = {"failed": [{"id": None, "code": "cleanup_queue_failed"}]}
    payload = {
        "batch": batch,
        "idempotent": False,
        "pdf_report": pdf_report,
        "cleanup_warnings": [
            *({"error": code} for code in work_cleanup_errors),
            *cleanup["failed"],
        ],
    }
    return _operation_response(
        payload,
        safe_result if reservation["agent_reservation"] else None,
        replayed=False,
    )


def _rollback_recovery_operation(db, operation, *, error_code: str) -> bool:
    final_exists = Path(operation["final_path"]).resolve().exists()
    cleanup_targets, cleanup_errors = _cleanup_operation_paths(
        operation, include_final=final_exists
    )
    if final_exists and cleanup_errors:
        _mark_cleanup_pending(
            db,
            operation["token"],
            cleanup_targets=cleanup_targets,
            cleanup_errors=cleanup_errors,
        )
        return False
    return _release_export_reservation(
        db,
        operation["token"],
        error_code=error_code,
        http_status=409,
        cleanup_targets=cleanup_targets,
    )


def recover_incomplete_exports(app) -> dict:
    """Finalize only fully valid published exports; otherwise release safely."""
    db = connect_db(app.config["DATABASE"])
    recovered = 0
    rolled_back = 0
    cleanup_pending = 0
    completed_cleanup_errors: list[dict] = []
    try:
        try:
            process_file_cleanup_queue(db)
        except Exception:
            pass
        rows = db.execute(
            """SELECT o.* FROM reimbursement_batches b
               JOIN export_operations o ON o.token=b.export_token
               WHERE b.status='draft' AND b.export_token IS NOT NULL
               ORDER BY o.created_at,o.token"""
        ).fetchall()
        for operation in rows:
            if operation["phase"] == "cleanup_pending":
                if _rollback_recovery_operation(
                    db, operation, error_code="export_recovery_rolled_back"
                ):
                    rolled_back += 1
                else:
                    cleanup_pending += 1
                continue

            final_dir = Path(operation["final_path"]).resolve()
            final_pdf = Path(operation["pdf_path"]).resolve()
            files_ready = (
                operation["state"] == "files_ready"
                and operation["phase"] == "files_ready"
                and final_dir.is_dir()
                and final_pdf.is_file()
                and _safe_child(final_pdf, final_dir) is not None
                and _owned_export_directory(final_dir, operation["token"])
            )
            if files_ready:
                try:
                    _finalize_export(db, operation["token"], recovered=True)
                except Exception:
                    if _rollback_recovery_operation(
                        db, operation, error_code="export_recovery_precondition_failed"
                    ):
                        rolled_back += 1
                    else:
                        cleanup_pending += 1
                else:
                    recovered += 1
                continue
            if _rollback_recovery_operation(
                db, operation, error_code="export_recovery_rolled_back"
            ):
                rolled_back += 1
            else:
                cleanup_pending += 1

        orphaned = db.execute(
            """SELECT id,export_token,row_version FROM reimbursement_batches
               WHERE status='draft' AND export_token IS NOT NULL
               AND NOT EXISTS(
                   SELECT 1 FROM export_operations o WHERE o.token=reimbursement_batches.export_token
               )"""
        ).fetchall()
        for row in orphaned:
            with transaction(db):
                changed = db.execute(
                    """UPDATE reimbursement_batches
                       SET export_token=NULL,export_started_at=NULL,
                           export_error='export_operation_missing',updated_at=?,row_version=row_version+1
                       WHERE id=? AND export_token=? AND row_version=?""",
                    (utc_now(), row["id"], row["export_token"], row["row_version"]),
                )
                if changed.rowcount:
                    audit(
                        db,
                        "batch",
                        row["id"],
                        "export_recovery_orphan_released",
                        {"error_code": "export_operation_missing"},
                    )
                    rolled_back += 1

        # A process may exit after the financial commit but before the ordinary
        # post-response work-tree cleanup. Completed operations are immutable,
        # so startup may safely scavenge only their marker-owned temporary and
        # publishing trees; the published final artifact is never a candidate.
        completed_operations = db.execute(
            """SELECT * FROM export_operations
               WHERE state='completed' AND phase='completed'
               ORDER BY updated_at,token"""
        ).fetchall()
        for operation in completed_operations:
            cleanup_targets, cleanup_errors = _cleanup_operation_paths(
                operation, include_final=False
            )
            if cleanup_targets:
                with transaction(db):
                    for path, kind, root in cleanup_targets:
                        enqueue_file_cleanup(
                            db,
                            path,
                            kind,
                            root,
                            f"{EXPORT_CLEANUP_REASON_PREFIX}{operation['token']}",
                        )
            if cleanup_errors:
                completed_cleanup_errors.append(
                    {
                        "token": operation["token"],
                        "errors": sorted(set(cleanup_errors)),
                    }
                )
        if completed_operations:
            try:
                queued_cleanup = process_file_cleanup_queue(db)
            except Exception:
                completed_cleanup_errors.append(
                    {"token": None, "errors": ["cleanup_queue_failed"]}
                )
            else:
                completed_cleanup_errors.extend(queued_cleanup["failed"])
    finally:
        db.close()
    if cleanup_pending:
        app.logger.warning(
            "Export recovery still has cleanup-pending operations: count=%s",
            cleanup_pending,
        )
    if completed_cleanup_errors:
        app.logger.warning(
            "Completed export work-tree cleanup requires retry: %s",
            completed_cleanup_errors,
        )
    return {
        "recovered": recovered,
        "rolled_back": rolled_back,
        "cleanup_pending": cleanup_pending,
    }
