from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ... import AppError
from ...batch_service import recalculate_batch_total, serialize_batch
from ...db import audit, transaction, utc_now
from ...storage import enqueue_file_cleanup, process_file_cleanup_queue


MAX_BATCH_ITEMS = 200


@dataclass(frozen=True)
class BatchMergeSource:
    batch_id: int
    expected_version: int


@dataclass(frozen=True)
class _ArchivePlan:
    batch_id: int
    archive_path: Path
    pdf_missing: bool


@dataclass(frozen=True)
class _CleanupJob:
    queue_id: int
    batch_id: int


@dataclass(frozen=True)
class BatchMergeOutcome:
    payload: dict
    cleanup_jobs: tuple[_CleanupJob, ...]


def _safe_child(path_value: str | Path, root: Path) -> Path:
    path = Path(path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise AppError(
            "来源报销包的旧版归档超出当前归档目录，不能自动回收。",
            409,
            "unsafe_archive_path",
        )
    if path == root:
        raise AppError("拒绝回收归档根目录。", 409, "unsafe_archive_path")
    return path


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _resolved_optional(row, field: str) -> Path | None:
    value = row[field]
    return Path(value).resolve() if value else None


def _plan_source_archives(
    source_rows: Sequence,
    target_row,
    *,
    archive_root: str | Path,
    discard_source_archives: bool,
) -> list[_ArchivePlan]:
    root_value = Path(archive_root).expanduser()
    if not root_value.is_absolute():
        raise AppError("当前归档目录配置无效。", 409, "unsafe_archive_path")
    root = root_value.resolve()

    protected_target_paths = [
        path
        for field in (
            "archive_path",
            "pdf_path",
            "superseded_archive_path",
            "superseded_pdf_path",
        )
        if (path := _resolved_optional(target_row, field)) is not None
    ]
    planned_archive_paths: list[Path] = []
    plans: list[_ArchivePlan] = []

    for source in source_rows:
        if source["archive_path"] or source["pdf_path"]:
            raise AppError(
                "来源草稿仍关联当前导出产物，请刷新或先完成归档修复。",
                409,
                "source_archive_state_invalid",
            )

        archive_value = source["superseded_archive_path"]
        pdf_value = source["superseded_pdf_path"]
        if not archive_value:
            if pdf_value:
                raise AppError(
                    "来源报销包的旧版 PDF 缺少对应归档目录，不能自动回收。",
                    409,
                    "unsafe_archive_path",
                )
            continue
        if not discard_source_archives:
            raise AppError(
                "来源报销包包含旧版归档，请明确确认移入 30 天回收区。",
                409,
                "source_archive_discard_required",
            )

        archive_path = _safe_child(archive_value, root)
        conflicting_paths = [*protected_target_paths, *planned_archive_paths]
        if any(_paths_overlap(archive_path, other) for other in conflicting_paths):
            raise AppError(
                "来源与目标报销包的旧版归档路径存在冲突，拒绝自动合并。",
                409,
                "archive_path_conflict",
            )
        if not archive_path.exists():
            raise AppError(
                "来源报销包的旧版归档当前不可访问。请恢复归档目录后重试，避免丢失可恢复记录。",
                409,
                "source_archive_unavailable",
            )
        if not archive_path.is_dir():
            raise AppError(
                "来源报销包的旧版归档路径不是目录。",
                409,
                "unsafe_archive_path",
            )

        pdf_missing = False
        if pdf_value:
            pdf_path = Path(pdf_value).resolve()
            try:
                pdf_path.relative_to(archive_path)
            except ValueError:
                raise AppError(
                    "来源报销包的旧版 PDF 不在对应归档目录内。",
                    409,
                    "unsafe_archive_path",
                )
            if pdf_path == archive_path:
                raise AppError(
                    "来源报销包的旧版 PDF 路径无效。",
                    409,
                    "unsafe_archive_path",
                )
            if pdf_path.exists() and not pdf_path.is_file():
                raise AppError(
                    "来源报销包的旧版 PDF 路径不是文件。",
                    409,
                    "unsafe_archive_path",
                )
            pdf_missing = not pdf_path.exists()

        planned_archive_paths.append(archive_path)
        plans.append(
            _ArchivePlan(
                batch_id=int(source["id"]),
                archive_path=archive_path,
                pdf_missing=pdf_missing,
            )
        )
    return plans


def _batch_rows(db, batch_ids: Sequence[int]) -> dict[int, object]:
    placeholders = ",".join("?" for _ in batch_ids)
    rows = db.execute(
        f"SELECT * FROM reimbursement_batches WHERE id IN ({placeholders})",
        list(batch_ids),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def _batch_members(db, batch_id: int) -> list:
    return db.execute(
        """SELECT bi.expense_item_id,bi.sort_order,i.status,i.row_version
             FROM batch_items bi
             JOIN expense_items i ON i.id=bi.expense_item_id
            WHERE bi.batch_id=?
            ORDER BY bi.sort_order,bi.expense_item_id""",
        (batch_id,),
    ).fetchall()


def merge_reimbursement_batches(
    db,
    *,
    target_batch_id: int,
    target_version: int,
    sources: Sequence[BatchMergeSource],
    confirmation: str,
    discard_source_archives: bool,
    archive_root: str | Path,
    snapshot_name: str,
) -> BatchMergeOutcome:
    """Atomically fold editable source batches into one editable target batch."""
    source_ids = [source.batch_id for source in sources]
    if not source_ids:
        raise AppError("请至少选择一个来源报销包。", 400, "sources_required")
    if target_batch_id in source_ids or len(set(source_ids)) != len(source_ids):
        raise AppError("来源报销包不能重复，也不能包含目标报销包。", 400, "invalid_batch_sources")

    cleanup_jobs: list[_CleanupJob] = []
    cleanup_warnings: list[dict] = []
    with transaction(db):
        requested_ids = [target_batch_id, *source_ids]
        rows = _batch_rows(db, requested_ids)
        if len(rows) != len(requested_ids):
            raise AppError("目标或来源报销包不存在，请刷新后重试。", 404, "batch_not_found")

        target = rows[target_batch_id]
        source_rows = [rows[source_id] for source_id in source_ids]
        expected_versions = {source.batch_id: source.expected_version for source in sources}
        if int(target["row_version"]) != target_version or any(
            int(source["row_version"]) != expected_versions[int(source["id"])]
            for source in source_rows
        ):
            raise AppError("报销包已被其他操作修改，请刷新后重试。", 409, "stale_version")
        if confirmation != target["name"]:
            raise AppError(
                "请输入完整的目标报销包名称以确认合并。",
                409,
                "merge_confirmation_mismatch",
            )
        if target["status"] != "draft" or any(source["status"] != "draft" for source in source_rows):
            raise AppError("只有处理中的报销包可以合并。", 409, "invalid_batch_status")
        if target["export_token"] or any(source["export_token"] for source in source_rows):
            raise AppError("报销包正在生成归档，请完成后再合并。", 409, "batch_exporting")
        if any(source["project_id"] != target["project_id"] for source in source_rows):
            raise AppError("只有同一报销项目的报销包可以合并。", 409, "batch_project_mismatch")

        members = {batch_id: _batch_members(db, batch_id) for batch_id in requested_ids}
        if any(not members[source_id] for source_id in source_ids):
            raise AppError("来源报销包没有可合并条目。", 409, "source_batch_empty")
        all_members = [member for batch_id in requested_ids for member in members[batch_id]]
        if len(all_members) > MAX_BATCH_ITEMS:
            raise AppError("合并后的报销包最多包含 200 笔条目。", 400, "too_many_items")
        if any(member["status"] != "in_batch" for member in all_members):
            raise AppError("报销包条目状态已变化，请刷新后重试。", 409, "batch_items_changed")
        member_ids = [int(member["expense_item_id"]) for member in all_members]
        if len(set(member_ids)) != len(member_ids):
            raise AppError("报销包条目归属存在冲突，请刷新后重试。", 409, "batch_items_changed")

        archive_plans = _plan_source_archives(
            source_rows,
            target,
            archive_root=archive_root,
            discard_source_archives=discard_source_archives,
        )
        plan_by_batch = {plan.batch_id: plan for plan in archive_plans}
        for plan in archive_plans:
            enqueue_file_cleanup(
                db,
                plan.archive_path,
                "archive_tree",
                archive_root,
                "合并来源报销包后回收旧版归档",
            )
            queue_row = db.execute(
                "SELECT id FROM file_cleanup_queue WHERE path=? AND kind='archive_tree'",
                (str(plan.archive_path),),
            ).fetchone()
            if not queue_row:
                raise AppError("旧版归档回收任务创建失败。", 409, "cleanup_queue_failed")
            cleanup_jobs.append(
                _CleanupJob(queue_id=int(queue_row["id"]), batch_id=plan.batch_id)
            )
            if plan.pdf_missing:
                cleanup_warnings.append(
                    {"batch_id": plan.batch_id, "code": "superseded_pdf_missing"}
                )
                audit(
                    db,
                    "batch",
                    plan.batch_id,
                    "superseded_pdf_missing_during_merge",
                    {"target_batch_id": target_batch_id},
                )
            db.execute(
                """UPDATE reimbursement_batches
                      SET superseded_archive_path=NULL,superseded_pdf_path=NULL
                    WHERE id=?""",
                (plan.batch_id,),
            )

        target_orders = [int(member["sort_order"]) for member in members[target_batch_id]]
        next_sort_order = (max(target_orders) + 1) if target_orders else 0
        moved_item_ids: list[int] = []
        now = utc_now()
        for source in sources:
            for member in members[source.batch_id]:
                item_id = int(member["expense_item_id"])
                changed = db.execute(
                    """UPDATE batch_items SET batch_id=?,sort_order=?
                        WHERE batch_id=? AND expense_item_id=?""",
                    (target_batch_id, next_sort_order, source.batch_id, item_id),
                )
                if changed.rowcount != 1:
                    raise AppError("报销包条目归属已变化，请刷新后重试。", 409, "batch_items_changed")
                touched = db.execute(
                    """UPDATE expense_items SET updated_at=?,row_version=row_version+1
                         WHERE id=? AND status='in_batch' RETURNING row_version""",
                    (now, item_id),
                ).fetchone()
                if not touched:
                    raise AppError("报销包条目状态已变化，请刷新后重试。", 409, "batch_items_changed")
                moved_item_ids.append(item_id)
                next_sort_order += 1
                audit(
                    db,
                    "item",
                    item_id,
                    "moved_between_batches",
                    {"from_batch_id": source.batch_id, "to_batch_id": target_batch_id},
                )

        source_names: dict[int, str] = {}
        for source in sources:
            source_row = rows[source.batch_id]
            source_names[source.batch_id] = str(source_row["name"])
            source_item_ids = [
                int(member["expense_item_id"]) for member in members[source.batch_id]
            ]
            audit(
                db,
                "batch",
                source.batch_id,
                "merged_into_batch",
                {
                    "target_batch_id": target_batch_id,
                    "target_name": target["name"],
                    "source_version": source.expected_version,
                    "item_ids": source_item_ids,
                    "source_archive_discarded": source.batch_id in plan_by_batch,
                    "snapshot": snapshot_name,
                },
            )
            deleted = db.execute(
                """DELETE FROM reimbursement_batches
                    WHERE id=? AND status='draft' AND export_token IS NULL AND row_version=?""",
                (source.batch_id, source.expected_version),
            )
            if deleted.rowcount != 1:
                raise AppError("来源报销包已变化，请刷新后重试。", 409, "stale_version")

        new_target_version = recalculate_batch_total(db, target_batch_id, bump_version=True)
        audit(
            db,
            "batch",
            target_batch_id,
            "source_batches_merged",
            {
                "source_batch_ids": source_ids,
                "moved_item_ids": moved_item_ids,
                "version": new_target_version,
                "snapshot": snapshot_name,
            },
        )
        audit(
            db,
            "system",
            0,
            "draft_batches_merged",
            {
                "target_batch_id": target_batch_id,
                "target_name": target["name"],
                "source_batches": [
                    {"id": source_id, "name": source_names[source_id]}
                    for source_id in source_ids
                ],
                "moved_item_ids": moved_item_ids,
                "snapshot": snapshot_name,
            },
        )
        batch = serialize_batch(db, target_batch_id)

    return BatchMergeOutcome(
        payload={
            "batch": batch,
            "merged_source_ids": source_ids,
            "moved_item_ids": moved_item_ids,
            "cleanup_warnings": cleanup_warnings,
        },
        cleanup_jobs=tuple(cleanup_jobs),
    )


def complete_merge_cleanup(db, outcome: BatchMergeOutcome) -> dict:
    """Process queued source archives after the database merge has committed."""
    payload = dict(outcome.payload)
    warnings = list(payload["cleanup_warnings"])
    if not outcome.cleanup_jobs:
        return payload

    try:
        process_file_cleanup_queue(db)
        job_ids = [job.queue_id for job in outcome.cleanup_jobs]
        placeholders = ",".join("?" for _ in job_ids)
        rows = db.execute(
            f"SELECT id,status,last_error FROM file_cleanup_queue WHERE id IN ({placeholders})",
            job_ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        for job in outcome.cleanup_jobs:
            row = by_id.get(job.queue_id)
            if not row or row["status"] != "completed":
                warnings.append(
                    {
                        "batch_id": job.batch_id,
                        "code": (row["last_error"] if row and row["last_error"] else "cleanup_pending"),
                    }
                )
    except Exception:
        warnings.extend(
            {"batch_id": job.batch_id, "code": "cleanup_queue_failed"}
            for job in outcome.cleanup_jobs
        )
    payload["cleanup_warnings"] = warnings
    return payload
