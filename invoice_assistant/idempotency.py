from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from dataclasses import dataclass
from decimal import Decimal

from . import AppError
from .db import json_dump, json_load, transaction, utc_now


CONTRACT_VERSION = "agent-write-v1"
TERMINAL_STATUSES = {"succeeded", "failed"}


@dataclass(frozen=True)
class OperationReservation:
    operation_id: str
    operation_name: str
    request_fingerprint: str
    replayed: bool = False
    status: str = "in_progress"
    http_status: int | None = None
    operation_result: dict | None = None
    error_code: str | None = None
    error_outcome: str | None = None


class ReplayedOperationFailure(AppError):
    def __init__(self, reservation: OperationReservation):
        super().__init__(
            "该操作已以确定失败结束；请查询操作状态。",
            reservation.http_status or 409,
            reservation.error_code or "operation_failed",
        )
        self.operation_id = reservation.operation_id
        self.outcome = reservation.error_outcome or "not_applied"
        self.replayed = True


def validate_operation_id(value: str | None, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise AppError("写操作必须提供 UUID v4 Idempotency-Key。", 400, "operation_id_required")
        return None
    if not isinstance(value, str) or len(value) != 36:
        raise AppError("Idempotency-Key 必须是规范 UUID v4。", 400, "invalid_operation_id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise AppError("Idempotency-Key 必须是规范 UUID v4。", 400, "invalid_operation_id")
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != value.lower():
        raise AppError("Idempotency-Key 必须是规范 UUID v4。", 400, "invalid_operation_id")
    return str(parsed)


def _canonicalize(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        # Invalid Agent inputs still need a stable fingerprint so their fixed
        # 4xx result can be durably replayed. Business validation rejects the
        # float after the operation row has been reserved.
        if math.isnan(value):
            encoded = "nan"
        elif math.isinf(value):
            encoded = "infinity" if value > 0 else "-infinity"
        else:
            encoded = value.hex()
        return {"$invalid_json_float": encoded}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(entry) for entry in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise AppError("写请求对象键必须是字符串。", 400, "invalid_request")
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    raise AppError("写请求包含不支持的值类型。", 400, "invalid_request")


def request_fingerprint(operation_name: str, parameters: dict) -> str:
    payload = {
        "contract_version": CONTRACT_VERSION,
        "operation_name": operation_name,
        "parameters": _canonicalize(parameters),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reservation_from_row(row, *, replayed: bool) -> OperationReservation:
    return OperationReservation(
        operation_id=row["operation_id"],
        operation_name=row["operation_name"],
        request_fingerprint=row["request_fingerprint"],
        replayed=replayed,
        status=row["status"],
        http_status=row["http_status"],
        operation_result=json_load(row["operation_result_json"], None),
        error_code=row["error_code"],
        error_outcome=row["error_outcome"],
    )


def reserve_operation(db, operation_id: str, operation_name: str, fingerprint: str) -> OperationReservation:
    """Reserve an operation inside the caller's BEGIN IMMEDIATE transaction.

    The lookup intentionally precedes entity version and review checks so a
    successful request can be replayed after the resource has moved on.
    """
    row = db.execute("SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row:
        if not hmac.compare_digest(row["request_fingerprint"], fingerprint) or row["operation_name"] != operation_name:
            raise AppError("同一 operation ID 已用于不同操作或参数。", 409, "idempotency_mismatch")
        reservation = _reservation_from_row(row, replayed=True)
        if reservation.status == "in_progress":
            raise AppError("该操作仍在执行，请稍后查询原 operation ID。", 409, "operation_in_progress")
        if reservation.status == "failed":
            raise ReplayedOperationFailure(reservation)
        return reservation
    now = utc_now()
    db.execute(
        """INSERT INTO agent_operations(
               operation_id,operation_name,request_fingerprint,status,
               operation_result_json,http_status,error_code,error_outcome,
               created_at,updated_at,completed_at
           ) VALUES(?,?,?,'in_progress',NULL,NULL,NULL,NULL,?,?,NULL)""",
        (operation_id, operation_name, fingerprint, now, now),
    )
    return OperationReservation(operation_id, operation_name, fingerprint)


def complete_operation(
    db,
    reservation: OperationReservation,
    operation_result: dict,
    *,
    http_status: int = 200,
) -> None:
    if reservation.replayed:
        return
    now = utc_now()
    changed = db.execute(
        """UPDATE agent_operations
           SET status='succeeded',operation_result_json=?,http_status=?,error_code=NULL,
               error_outcome=NULL,updated_at=?,completed_at=?
           WHERE operation_id=? AND operation_name=? AND request_fingerprint=? AND status='in_progress'""",
        (
            json_dump(operation_result),
            int(http_status),
            now,
            now,
            reservation.operation_id,
            reservation.operation_name,
            reservation.request_fingerprint,
        ),
    )
    if changed.rowcount != 1:
        raise AppError("操作状态发生变化。", 409, "operation_state_changed")


def fail_reserved_operation(
    db,
    reservation: OperationReservation,
    *,
    http_status: int,
    error_code: str,
    outcome: str = "not_applied",
) -> None:
    """Finish a reservation inside the caller's existing write transaction."""
    if reservation.replayed:
        raise ValueError("Cannot fail a replayed operation")
    if outcome not in {"not_applied", "unknown"}:
        raise ValueError("Unsupported operation outcome")
    now = utc_now()
    changed = db.execute(
        """UPDATE agent_operations
           SET status='failed',operation_result_json=NULL,http_status=?,error_code=?,error_outcome=?,
               updated_at=?,completed_at=?
           WHERE operation_id=? AND operation_name=? AND request_fingerprint=? AND status='in_progress'""",
        (
            int(http_status),
            error_code,
            outcome,
            now,
            now,
            reservation.operation_id,
            reservation.operation_name,
            reservation.request_fingerprint,
        ),
    )
    if changed.rowcount != 1:
        raise AppError("操作状态发生变化。", 409, "operation_state_changed")


def fail_operation(
    db,
    operation_id: str,
    operation_name: str,
    fingerprint: str,
    *,
    http_status: int,
    error_code: str,
    outcome: str = "not_applied",
) -> None:
    """Persist a side-effect-free terminal failure in its own short transaction."""
    if outcome not in {"not_applied", "unknown"}:
        raise ValueError("Unsupported operation outcome")
    now = utc_now()
    with transaction(db):
        row = db.execute("SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row:
            if row["operation_name"] != operation_name or not hmac.compare_digest(row["request_fingerprint"], fingerprint):
                return
            if row["status"] == "succeeded":
                return
            db.execute(
                """UPDATE agent_operations SET status='failed',operation_result_json=NULL,http_status=?,
                          error_code=?,error_outcome=?,updated_at=?,completed_at=? WHERE operation_id=?""",
                (int(http_status), error_code, outcome, now, now, operation_id),
            )
            return
        db.execute(
            """INSERT INTO agent_operations(
                   operation_id,operation_name,request_fingerprint,status,operation_result_json,
                   http_status,error_code,error_outcome,created_at,updated_at,completed_at
               ) VALUES(?,?,?,'failed',NULL,?,?,?,?,?,?)""",
            (operation_id, operation_name, fingerprint, int(http_status), error_code, outcome, now, now, now),
        )


def operation_result(resource_refs: list[dict], *, artifact_available: bool = False, warning_codes=None) -> dict:
    safe_refs = []
    for reference in resource_refs:
        resource_type = str(reference.get("type") or "")
        resource_id = reference.get("id")
        version = reference.get("version")
        if not resource_type or isinstance(resource_id, bool) or not isinstance(resource_id, int):
            raise ValueError("Invalid resource reference")
        entry = {"type": resource_type, "id": resource_id}
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("Invalid resource version")
            entry["version"] = version
        safe_refs.append(entry)
    return {
        "resource_refs": safe_refs,
        "artifact_available": bool(artifact_available),
        "warning_codes": [str(code) for code in (warning_codes or [])],
    }


def serialize_operation(row) -> dict:
    if not row:
        raise AppError("操作记录不存在。", 404, "operation_not_found")
    # A file operation stores its safe response snapshot while the resource is
    # already durable but before the ledger is terminal.  That private recovery
    # checkpoint must not be observable as a successful operation result.
    public_result = (
        json_load(row["operation_result_json"], None)
        if row["status"] == "succeeded"
        else None
    )
    return {
        "operation_id": row["operation_id"],
        "operation_name": row["operation_name"],
        "status": row["status"],
        "http_status": row["http_status"],
        "operation_result": public_result,
        "error_code": row["error_code"],
        "outcome": "applied" if row["status"] == "succeeded" else row["error_outcome"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def get_operation(db, operation_id: str) -> dict:
    operation_id = validate_operation_id(operation_id, required=True)
    row = db.execute("SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)).fetchone()
    return serialize_operation(row)
