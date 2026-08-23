from __future__ import annotations

import hashlib
import hmac
import json
import base64
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import AppError
from .domain import find_duplicate_candidates, normalize_untrusted_text, serialize_item
from .db import json_dump, json_load, utc_now


REVIEW_CONTRACT_VERSION = "invoice-review-v1"
MAX_DISPLAY_BLOCKING_CANDIDATES = 100
REVIEW_SESSION_TTL_MINUTES = 15


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _uncertainty_text(value) -> str:
    if isinstance(value, str):
        return normalize_untrusted_text(value)
    return normalize_untrusted_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    )


def stable_uncertainties(values) -> list[dict]:
    occurrences: Counter[str] = Counter()
    result = []
    for value in values or []:
        text = _uncertainty_text(value)
        occurrences[text] += 1
        identity = hashlib.sha256(
            _canonical_json({"text": text, "occurrence": occurrences[text]})
        ).hexdigest()[:20]
        result.append({"uncertainty_id": f"u_{identity}", "message": text})
    return result


def _candidate_summary(match: dict) -> dict:
    item = match["item"]
    return {
        "id": int(item["id"]),
        "version": int(item.get("version", 0)),
        "status": item["status"],
        "merchant": normalize_untrusted_text(item.get("merchant")),
        "expense_date": normalize_untrusted_text(item.get("expense_date")),
        "amount_cents": int(item.get("amount_cents") or 0),
        "currency": normalize_untrusted_text(item.get("currency") or "CNY"),
        "purpose": normalize_untrusted_text(item.get("purpose")),
        "confidence": match["confidence"],
        "exact_file": bool(match.get("exact_file")),
        "high_confidence": bool(match.get("high_confidence")),
        "historical": bool(match.get("historical")),
        "reason": list(match.get("reason") or []),
        "merge_allowed": bool(match.get("merge_allowed")),
    }


def _token_candidate_summary(candidate: dict) -> dict:
    return {
        key: candidate[key]
        for key in ("id", "version", "exact_file", "high_confidence", "historical", "reason", "merge_allowed")
    }


def build_review(db, item_id: int, *, display_limit: int = MAX_DISPLAY_BLOCKING_CANDIDATES) -> dict:
    item = serialize_item(db, item_id)
    matches = find_duplicate_candidates(db, item_id, limit=None)
    candidates = [_candidate_summary(match) for match in matches]
    blocking = [candidate for candidate in candidates if any(
        (candidate["exact_file"], candidate["high_confidence"], candidate["historical"])
    )]
    # The digest order is identity based; display order can evolve independently.
    blocking_for_token = sorted(
        (_token_candidate_summary(candidate) for candidate in blocking),
        key=lambda candidate: candidate["id"],
    )
    uncertainties = stable_uncertainties(item.get("uncertainties"))
    snapshot = {
        "contract_version": REVIEW_CONTRACT_VERSION,
        "item": {
            "id": int(item["id"]),
            "version": int(item.get("version", 0)),
            "merchant": normalize_untrusted_text(item.get("merchant")),
            "expense_date": normalize_untrusted_text(item.get("expense_date")),
            "amount_cents": int(item.get("amount_cents") or 0),
            "currency": normalize_untrusted_text(item.get("currency") or "CNY"),
            "converted_amount_cents": item.get("converted_amount_cents"),
            "purpose": normalize_untrusted_text(item.get("purpose")),
            "project_id": item.get("project_id"),
            "status": item.get("status"),
        },
        "uncertainties": uncertainties,
        "recognition_failure": normalize_untrusted_text(item.get("recognition_error")),
        "blocking_candidates": blocking_for_token,
        "blocking_total": len(blocking),
        "overflow": len(blocking) > display_limit,
    }
    digest = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    token = f"review-v1.{digest}"
    # The review surface is primarily a blocking-candidate acknowledgement
    # surface.  Always expose the first `display_limit` blocking rows before
    # using any remaining room for non-blocking references; otherwise a high
    # confidence/reference sort can hide a blocking row while still returning
    # its ID in `blocking_duplicate_ids`.
    displayed_blocking = blocking[:display_limit]
    displayed_ids = {candidate["id"] for candidate in displayed_blocking}
    displayed_candidates = [*displayed_blocking]
    displayed_candidates.extend(
        candidate
        for candidate in candidates
        if candidate["id"] not in displayed_ids
    )
    displayed_candidates = displayed_candidates[:display_limit]
    return {
        "token": token,
        "digest": digest,
        "uncertainties": uncertainties,
        "recognition_failure": snapshot["recognition_failure"] or None,
        "duplicate_candidates": displayed_candidates,
        "blocking_duplicate_ids": [candidate["id"] for candidate in displayed_blocking],
        "blocking_total": len(blocking),
        "duplicate_review_overflow": len(blocking) > display_limit,
        "_all_candidates": candidates,
        "_all_blocking_candidates": blocking,
    }


def require_current_review(db, item_id: int, expected_version, supplied_token: str | None) -> dict:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise AppError("必须提供当前条目版本。", 400, "version_required")
    row = db.execute("SELECT row_version FROM expense_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise AppError("条目不存在。", 404, "item_not_found")
    if int(row["row_version"]) != expected_version:
        raise AppError("条目已被其他操作修改，请刷新后重新核对。", 409, "stale_version")
    if not isinstance(supplied_token, str) or not supplied_token:
        raise AppError("确认或合并前必须读取并提交 review token。", 400, "review_token_required")
    current = build_review(db, item_id)
    if not hmac.compare_digest(current["token"], supplied_token):
        raise AppError("核对内容或重复候选已变化，请重新读取条目。", 409, "review_changed")
    return current


def _strict_id_list(value, field: str) -> list[int]:
    if not isinstance(value, list):
        raise AppError(f"{field} 必须是编号数组。", 400, "invalid_review_acknowledgement")
    result = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
            raise AppError(f"{field} 包含无效编号。", 400, "invalid_review_acknowledgement")
        result.append(entry)
    if len(result) != len(set(result)):
        raise AppError(f"{field} 不能包含重复编号。", 400, "invalid_review_acknowledgement")
    return result


def _strict_string_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(entry, str) and entry for entry in value):
        raise AppError(f"{field} 必须是非空字符串数组。", 400, "invalid_review_acknowledgement")
    if len(value) != len(set(value)):
        raise AppError(f"{field} 不能包含重复值。", 400, "invalid_review_acknowledgement")
    return value


def validate_confirmation(review: dict, payload: dict, *, overflow_authorized: bool = False) -> None:
    uncertainty_ids = [entry["uncertainty_id"] for entry in review["uncertainties"]]
    acknowledged_uncertainties = _strict_string_list(
        payload.get("acknowledged_uncertainty_ids", []), "acknowledged_uncertainty_ids"
    )
    if set(acknowledged_uncertainties) != set(uncertainty_ids):
        raise AppError("必须逐项确认当前全部不确定内容。", 409, "uncertainties_not_acknowledged")

    if review["duplicate_review_overflow"] and not overflow_authorized:
        raise AppError("阻断重复候选过多，必须在浏览器完成完整分页核对。", 409, "duplicate_review_overflow")
    blocking_ids = [candidate["id"] for candidate in review["_all_blocking_candidates"]]
    acknowledged_duplicates = _strict_id_list(
        payload.get("acknowledged_duplicate_ids", []), "acknowledged_duplicate_ids"
    )
    resolution = payload.get("duplicate_resolution")
    if review["duplicate_review_overflow"] and overflow_authorized:
        if resolution != "keep_separate" or acknowledged_duplicates:
            raise AppError(
                "完整分页核对令牌替代重复编号列表，处置方式必须为 keep_separate。",
                400,
                "invalid_duplicate_resolution",
            )
        return
    if not blocking_ids:
        if resolution != "none" or acknowledged_duplicates:
            raise AppError("当前没有阻断重复项，处置方式必须为 none。", 400, "invalid_duplicate_resolution")
        return
    if resolution != "keep_separate" or set(acknowledged_duplicates) != set(blocking_ids):
        raise AppError("必须明确保留为独立报销并确认全部阻断重复项。", 409, "duplicates_not_acknowledged")


def require_merge_target(review: dict, target_id: int) -> dict:
    if target_id <= 0:
        raise AppError("合并目标无效。", 400, "invalid_merge_target")
    candidate = next((entry for entry in review["_all_candidates"] if entry["id"] == target_id), None)
    if candidate is None:
        raise AppError("目标不是本次核对绑定的重复候选。", 409, "merge_target_not_reviewed")
    if not candidate["merge_allowed"] or candidate["historical"]:
        raise AppError("该重复候选不可作为合并目标。", 409, "invalid_merge_target")
    return candidate


def public_review(review: dict) -> dict:
    return {key: value for key, value in review.items() if not key.startswith("_") and key != "digest"}


def _session_cursor(session_id: str, digest: str, last_candidate_id: int, offset: int) -> str:
    payload = {
        "v": 1,
        "session_id": session_id,
        "review_digest": digest,
        "last_candidate_id": last_candidate_id,
        "offset": offset,
    }
    return base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=").decode("ascii")


def _public_session_page(row, candidates: list[dict], *, next_cursor: str | None, token: str | None = None) -> dict:
    result = {
        "session_id": row["session_id"],
        "item_id": int(row["item_id"]),
        "item_version": int(row["item_version"]),
        "blocking_total": int(row["blocking_count"]),
        "expires_at": row["expires_at"],
        "candidates": candidates,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }
    if token is not None:
        result["overflow_review_token"] = token
    return result


def create_review_session(db, item_id: int, expected_version: int, review_token: str) -> dict:
    review = require_current_review(db, item_id, expected_version, review_token)
    if not review["duplicate_review_overflow"]:
        raise AppError("当前重复候选未超过分页核对阈值。", 409, "duplicate_review_not_required")
    candidates = sorted(review["_all_blocking_candidates"], key=lambda entry: entry["id"], reverse=True)
    first_page = candidates[:MAX_DISPLAY_BLOCKING_CANDIDATES]
    session_id = str(uuid.uuid4())
    created_at = utc_now()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=REVIEW_SESSION_TTL_MINUTES)).isoformat(
        timespec="seconds"
    )
    last_id = first_page[-1]["id"]
    allowed_merge_ids = [entry["id"] for entry in candidates if entry["merge_allowed"]]
    db.execute(
        """INSERT INTO duplicate_review_sessions(
               session_id,item_id,item_version,review_digest,blocking_count,next_offset,last_candidate_id,
               expires_at,completed_at,consumed_at,overflow_token_hash,allowed_merge_ids_json,
               created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?)""",
        (
            session_id,
            item_id,
            expected_version,
            review["digest"],
            len(candidates),
            len(first_page),
            last_id,
            expires_at,
            json_dump(allowed_merge_ids),
            created_at,
            created_at,
        ),
    )
    row = db.execute("SELECT * FROM duplicate_review_sessions WHERE session_id=?", (session_id,)).fetchone()
    cursor = _session_cursor(session_id, review["digest"], last_id, len(first_page))
    return _public_session_page(row, first_page, next_cursor=cursor)


def next_review_session_page(db, item_id: int, session_id: str, supplied_cursor: str) -> dict:
    row = db.execute(
        "SELECT * FROM duplicate_review_sessions WHERE session_id=? AND item_id=?",
        (session_id, item_id),
    ).fetchone()
    if not row:
        raise AppError("重复核对会话不存在。", 404, "duplicate_review_session_not_found")
    now = datetime.now(timezone.utc)
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        expires_at = now
    if expires_at <= now:
        raise AppError("重复核对会话已过期，请重新开始。", 409, "duplicate_review_session_expired")
    if row["completed_at"] is not None:
        raise AppError("重复核对会话已完成，不能重复翻页。", 409, "duplicate_review_session_completed")
    expected_cursor = _session_cursor(
        row["session_id"], row["review_digest"], int(row["last_candidate_id"]), int(row["next_offset"])
    )
    if not isinstance(supplied_cursor, str) or not hmac.compare_digest(expected_cursor, supplied_cursor):
        raise AppError("必须使用上一页返回的游标顺序核对。", 409, "invalid_review_cursor")
    current = build_review(db, item_id)
    if int(row["item_version"]) != int(serialize_item(db, item_id)["version"]) or not hmac.compare_digest(
        row["review_digest"], current["digest"]
    ):
        raise AppError("核对期间条目或重复候选已变化。", 409, "review_changed")
    candidates = sorted(current["_all_blocking_candidates"], key=lambda entry: entry["id"], reverse=True)
    page = [entry for entry in candidates if entry["id"] < int(row["last_candidate_id"])][
        :MAX_DISPLAY_BLOCKING_CANDIDATES
    ]
    if not page:
        raise AppError("重复核对游标已失效。", 409, "invalid_review_cursor")
    emitted = int(row["next_offset"]) + len(page)
    has_more = emitted < int(row["blocking_count"])
    now_text = utc_now()
    raw_token = None
    next_cursor = None
    if has_more:
        last_id = page[-1]["id"]
        db.execute(
            "UPDATE duplicate_review_sessions SET next_offset=?,last_candidate_id=?,updated_at=? WHERE session_id=?",
            (emitted, last_id, now_text, session_id),
        )
        next_cursor = _session_cursor(session_id, row["review_digest"], last_id, emitted)
    else:
        raw_token = f"overflow-v1.{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        db.execute(
            """UPDATE duplicate_review_sessions SET next_offset=?,last_candidate_id=?,completed_at=?,
                      overflow_token_hash=?,updated_at=? WHERE session_id=?""",
            (emitted, page[-1]["id"], now_text, token_hash, now_text, session_id),
        )
    updated = db.execute("SELECT * FROM duplicate_review_sessions WHERE session_id=?", (session_id,)).fetchone()
    return _public_session_page(updated, page, next_cursor=next_cursor, token=raw_token)


def consume_overflow_token(db, item_id: int, expected_version: int, review: dict, raw_token: str | None) -> dict:
    if not review["duplicate_review_overflow"]:
        if raw_token not in (None, ""):
            raise AppError("当前核对不需要 overflow token。", 400, "invalid_overflow_review_token")
        return {"authorized": False, "allowed_merge_ids": []}
    if not isinstance(raw_token, str) or not raw_token.startswith("overflow-v1."):
        raise AppError("必须先完成全部重复候选分页核对。", 409, "duplicate_review_overflow")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = db.execute(
        """SELECT * FROM duplicate_review_sessions
           WHERE item_id=? AND item_version=? AND review_digest=? AND overflow_token_hash=?
             AND completed_at IS NOT NULL AND consumed_at IS NULL""",
        (item_id, expected_version, review["digest"], token_hash),
    ).fetchone()
    if not row:
        raise AppError("完整分页核对令牌无效或已使用。", 409, "invalid_overflow_review_token")
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise AppError("完整分页核对令牌已过期。", 409, "invalid_overflow_review_token")
    changed = db.execute(
        "UPDATE duplicate_review_sessions SET consumed_at=?,updated_at=? WHERE session_id=? AND consumed_at IS NULL",
        (utc_now(), utc_now(), row["session_id"]),
    )
    if changed.rowcount != 1:
        raise AppError("完整分页核对令牌已使用。", 409, "invalid_overflow_review_token")
    return {
        "authorized": True,
        "allowed_merge_ids": json_load(row["allowed_merge_ids_json"], []),
    }
