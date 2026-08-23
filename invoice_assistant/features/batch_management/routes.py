from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ... import AppError
from ...db import get_db
from ...http import json_body
from ...persistence import create_database_backup
from .service import (
    BatchMergeSource,
    complete_merge_cleanup,
    merge_reimbursement_batches,
)


batch_management_api = Blueprint("batch_management_api", __name__)


def _integer(value, field: str, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(f"必须提供有效的 {field}。", 400, "invalid_request")
    if value < (1 if positive else 0):
        raise AppError(f"必须提供有效的 {field}。", 400, "invalid_request")
    return value


def _sources(value, target_batch_id: int) -> list[BatchMergeSource]:
    if not isinstance(value, list) or not value:
        raise AppError("请至少选择一个来源报销包。", 400, "sources_required")
    if len(value) > 200:
        raise AppError("来源报销包数量过多。", 400, "too_many_sources")
    result: list[BatchMergeSource] = []
    seen: set[int] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"batch_id", "expected_version"}:
            raise AppError(
                "每个来源报销包必须且只能包含 batch_id 和 expected_version。",
                400,
                "invalid_request",
            )
        batch_id = _integer(entry["batch_id"], "batch_id", positive=True)
        version = _integer(entry["expected_version"], "expected_version", positive=False)
        if batch_id == target_batch_id or batch_id in seen:
            raise AppError(
                "来源报销包不能重复，也不能包含目标报销包。",
                400,
                "invalid_batch_sources",
            )
        seen.add(batch_id)
        result.append(BatchMergeSource(batch_id=batch_id, expected_version=version))
    return result


@batch_management_api.post("/batch-management/merge")
def merge_reimbursement_batches_route():
    payload = json_body()
    required = {
        "target_batch_id",
        "target_version",
        "sources",
        "confirmation",
        "discard_source_archives",
    }
    if set(payload) != required:
        raise AppError("报销包合并请求字段不完整或包含未知字段。", 400, "invalid_request")

    target_batch_id = _integer(payload["target_batch_id"], "target_batch_id", positive=True)
    target_version = _integer(payload["target_version"], "target_version", positive=False)
    confirmation = payload["confirmation"]
    discard_source_archives = payload["discard_source_archives"]
    if not isinstance(confirmation, str):
        raise AppError("合并确认名称必须是字符串。", 400, "invalid_request")
    if not isinstance(discard_source_archives, bool):
        raise AppError("旧版归档处置确认必须是布尔值。", 400, "invalid_request")
    sources = _sources(payload["sources"], target_batch_id)

    db = get_db()
    setting = db.execute("SELECT value FROM settings WHERE key='archive_root'").fetchone()
    archive_root = setting["value"] if setting else current_app.config["DEFAULT_ARCHIVE_DIR"]
    snapshot = create_database_backup(
        current_app.config["DATABASE"],
        current_app.config["BACKUP_DIR"],
        retention=current_app.config["BACKUP_RETENTION"],
        manual=True,
    )
    outcome = merge_reimbursement_batches(
        db,
        target_batch_id=target_batch_id,
        target_version=target_version,
        sources=sources,
        confirmation=confirmation,
        discard_source_archives=discard_source_archives,
        archive_root=archive_root,
        snapshot_name=snapshot.name,
    )
    return jsonify(complete_merge_cleanup(db, outcome))
