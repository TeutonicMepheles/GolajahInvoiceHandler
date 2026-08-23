from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from . import AppError


CURSOR_VERSION = 1


@dataclass(frozen=True)
class KeysetCursor:
    snapshot_max_id: int
    created_at: str | None = None
    row_id: int | None = None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def filter_hash(kind: str, filters: dict) -> str:
    return hashlib.sha256(_canonical_json({"kind": kind, "filters": filters})).hexdigest()


def encode_cursor(kind: str, filters_digest: str, cursor: KeysetCursor) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "kind": kind,
        "filter_hash": filters_digest,
        "snapshot_max_id": cursor.snapshot_max_id,
        "created_at": cursor.created_at,
        "id": cursor.row_id,
    }
    return base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=").decode("ascii")


def decode_cursor(value: str, kind: str, filters_digest: str) -> KeysetCursor:
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "v", "kind", "filter_hash", "snapshot_max_id", "created_at", "id"
        }:
            raise ValueError
        if payload["v"] != CURSOR_VERSION or payload["kind"] != kind:
            raise ValueError
        if payload["filter_hash"] != filters_digest:
            raise ValueError
        snapshot_max_id = payload["snapshot_max_id"]
        created_at = payload["created_at"]
        row_id = payload["id"]
        if isinstance(snapshot_max_id, bool) or not isinstance(snapshot_max_id, int) or snapshot_max_id < 0:
            raise ValueError
        if (created_at is None) != (row_id is None):
            raise ValueError
        if created_at is not None and (not isinstance(created_at, str) or not created_at or len(created_at) > 64):
            raise ValueError
        if row_id is not None and (isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0):
            raise ValueError
        # Reject non-canonical encodings and payload aliases.
        decoded = KeysetCursor(snapshot_max_id=snapshot_max_id, created_at=created_at, row_id=row_id)
        if encode_cursor(kind, filters_digest, decoded) != value:
            raise ValueError
        return decoded
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise AppError("分页游标无效或与当前筛选条件不匹配。", 400, "invalid_cursor")


def parse_limit(value, *, default: int = 50, maximum: int = 100) -> int:
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise AppError("分页参数无效。", 400, "invalid_pagination")
    if (
        isinstance(value, bool)
        or result < 1
        or result > maximum
        or (isinstance(value, str) and str(result) != value.strip())
    ):
        raise AppError(f"分页数量必须在 1 到 {maximum} 之间。", 400, "invalid_pagination")
    return result
