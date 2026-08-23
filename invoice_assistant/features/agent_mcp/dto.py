from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .contracts import MAX_MONEY_CENTS


SUMMARY_LIMITS = {"merchant": 200, "purpose": 500, "name": 200, "project_name": 200}


def clean_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    return parsed


def _nullable_integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _integer(value)


def _cents_from_amount(value: Any) -> int:
    try:
        result = int((Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) * 100)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0
    return min(max(result, 0), MAX_MONEY_CENTS)


def _summary_text(value: Any, field: str, truncated: list[str]) -> str:
    text = clean_text(value)
    limit = SUMMARY_LIMITS.get(field)
    if limit is not None and len(text) > limit:
        truncated.append(field)
        return text[:limit]
    return text


def material(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    missing = []
    for entry in source.get("missing") or []:
        if isinstance(entry, dict):
            missing.append({"code": clean_text(entry.get("code")), "label": clean_text(entry.get("label"))})
    return {
        "complete": bool(source.get("complete")),
        "percent": min(max(_integer(source.get("percent")), 0), 100),
        "missing": missing,
    }


def attachment(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    display_name = clean_text(
        source.get("display_name") or source.get("normalized_name") or source.get("original_name")
    )
    display_name = re.split(r"[\\/]", display_name)[-1]
    display_name = re.sub(r"^[A-Za-z]:", "", display_name)
    return {
        "id": max(_integer(source.get("id")), 1),
        "category": clean_text(source.get("category") or "unknown"),
        "display_name": display_name,
        "mime_type": clean_text(source.get("mime_type") or "application/octet-stream"),
        "size_bytes": max(_integer(source.get("size_bytes")), 0),
        "recognition_error": None if source.get("recognition_error") in (None, "") else clean_text(source.get("recognition_error")),
    }


def _batch_ref(source: dict[str, Any]) -> dict[str, int] | None:
    ref = source.get("batch_ref")
    if isinstance(ref, dict) and ref.get("batch_id"):
        return {"batch_id": _integer(ref.get("batch_id")), "batch_version": max(_integer(ref.get("batch_version")), 0)}
    if source.get("batch_id"):
        return {"batch_id": _integer(source.get("batch_id")), "batch_version": max(_integer(source.get("batch_version")), 0)}
    return None


def item_summary(value: Any, *, truncate: bool = True) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    truncated: list[str] = []
    text = _summary_text if truncate else (lambda value, _field, _truncated: clean_text(value))
    amount_cents = source.get("amount_cents")
    converted_cents = source.get("converted_amount_cents")
    return {
        "id": max(_integer(source.get("id")), 1),
        "version": max(_integer(source.get("version", source.get("row_version", 0))), 0),
        "merchant": text(source.get("merchant"), "merchant", truncated),
        "expense_date": clean_text(source.get("expense_date")),
        "amount_cents": _cents_from_amount(source.get("amount")) if amount_cents is None else max(_integer(amount_cents), 0),
        "currency": clean_text(source.get("currency") or "CNY").upper(),
        "converted_amount_cents": (
            _nullable_integer(converted_cents)
            if converted_cents is not None
            else (None if source.get("converted_amount") in (None, "") else _cents_from_amount(source.get("converted_amount")))
        ),
        "purpose": text(source.get("purpose"), "purpose", truncated),
        "project_id": _nullable_integer(source.get("project_id")),
        "project_name": None if source.get("project_name") is None else text(source.get("project_name"), "project_name", truncated),
        "status": clean_text(source.get("status") or "unknown"),
        "material": material(source.get("material")),
        "batch_ref": _batch_ref(source),
        "created_at": clean_text(source.get("created_at") or "1970-01-01T00:00:00Z"),
        "updated_at": clean_text(source.get("updated_at") or source.get("created_at") or "1970-01-01T00:00:00Z"),
        "truncated_fields": sorted(set(truncated)),
    }


def duplicate_candidate(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    nested = source.get("item") if isinstance(source.get("item"), dict) else source
    confidence = source.get("confidence_milli")
    if confidence is None:
        try:
            confidence = int(Decimal(str(source.get("confidence", 0))) * 1000)
        except (InvalidOperation, TypeError, ValueError):
            confidence = 0
    reason = source.get("reason")
    if isinstance(reason, (list, tuple, set)):
        reason = ",".join(
            sorted(
                {
                    cleaned
                    for entry in reason
                    if (cleaned := clean_text(entry).strip())
                }
            )
        )
    if not reason:
        flags = []
        if source.get("exact_file"):
            flags.append("exact_file")
        if source.get("high_confidence"):
            flags.append("high_confidence")
        if source.get("historical"):
            flags.append("historical")
        reason = ",".join(flags)
    return {
        "id": max(_integer(nested.get("id")), 1),
        "version": max(_integer(nested.get("version", nested.get("row_version", 0))), 0),
        "merchant": clean_text(nested.get("merchant")),
        "expense_date": clean_text(nested.get("expense_date")),
        "amount_cents": max(_integer(nested.get("amount_cents", _cents_from_amount(nested.get("amount")))), 0),
        "currency": clean_text(nested.get("currency") or "CNY").upper(),
        "status": clean_text(nested.get("status") or "unknown"),
        "confidence_milli": min(max(_integer(confidence), 0), 1000),
        "exact_file": bool(source.get("exact_file")),
        "high_confidence": bool(source.get("high_confidence")),
        "historical": bool(source.get("historical")),
        "merge_allowed": bool(source.get("merge_allowed", not source.get("historical"))),
        "reason": clean_text(reason),
    }


def review(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value.get("token"):
        return None
    uncertainties = []
    for index, entry in enumerate(value.get("uncertainties") or []):
        if isinstance(entry, dict):
            message = clean_text(entry.get("message"))
            uncertainty_id = clean_text(entry.get("uncertainty_id"))
        else:
            message = clean_text(entry)
            uncertainty_id = ""
        if not uncertainty_id:
            uncertainty_id = hashlib.sha256(f"{index}:{message}".encode("utf-8")).hexdigest()[:24]
        uncertainties.append({"uncertainty_id": uncertainty_id, "message": message})
    candidates = [duplicate_candidate(entry) for entry in (value.get("duplicate_candidates") or [])[:100]]
    blocking_ids = [_integer(entry) for entry in (value.get("blocking_duplicate_ids") or [])[:100] if _integer(entry) > 0]
    return {
        "token": clean_text(value.get("token")),
        "uncertainties": uncertainties,
        "recognition_error": (
            None
            if value.get("recognition_failure", value.get("recognition_error")) in (None, "")
            else clean_text(value.get("recognition_failure", value.get("recognition_error")))
        ),
        "duplicate_candidates": candidates,
        "blocking_duplicate_ids": blocking_ids,
        "blocking_total": max(_integer(value.get("blocking_total", len(blocking_ids))), 0),
        "duplicate_review_overflow": bool(value.get("duplicate_review_overflow")),
    }


def item_detail(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = item_summary(source, truncate=False)
    result["attachments"] = [attachment(entry) for entry in source.get("attachments") or []]
    result["review"] = review(source.get("review"))
    result["requirements_version"] = max(_integer(source.get("requirements_version")), 0)
    return result


def batch_summary(value: Any, *, truncate: bool = True) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    truncated: list[str] = []
    text = _summary_text if truncate else (lambda value, _field, _truncated: clean_text(value))
    completeness = source.get("completeness") if isinstance(source.get("completeness"), dict) else source
    total_cents = source.get("total_amount_cents")
    return {
        "id": max(_integer(source.get("id")), 1),
        "version": max(_integer(source.get("version", source.get("row_version", 0))), 0),
        "requirements_version": max(_integer(source.get("requirements_version")), 0),
        "name": text(source.get("name") or "Unnamed batch", "name", truncated),
        "project_id": _nullable_integer(source.get("project_id")),
        "project_name": None if source.get("project_name") is None else text(source.get("project_name"), "project_name", truncated),
        "purpose": text(source.get("purpose"), "purpose", truncated),
        "status": clean_text(source.get("status") or "unknown"),
        "total_amount_cents": _cents_from_amount(source.get("total_amount")) if total_cents is None else max(_integer(total_cents), 0),
        "complete": bool(completeness.get("complete")),
        "missing_item_count": max(_integer(completeness.get("missing_item_count")), 0),
        "artifact_available": bool(source.get("artifact_available") or source.get("pdf_path")),
        "created_at": clean_text(source.get("created_at") or "1970-01-01T00:00:00Z"),
        "updated_at": clean_text(source.get("updated_at") or source.get("created_at") or "1970-01-01T00:00:00Z"),
        "truncated_fields": sorted(set(truncated)),
    }


def batch_detail(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = batch_summary(source, truncate=False)
    result["notes"] = clean_text(source.get("notes"))
    result["items"] = [item_summary(entry) for entry in (source.get("items") or [])[:200]]
    return result


def reference_data(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    projects = [
        {
            "id": max(_integer(entry.get("id")), 1),
            "name": clean_text(entry.get("name") or "Unnamed project"),
            "code": clean_text(entry.get("code")),
            "enabled": bool(entry.get("enabled")),
        }
        for entry in source.get("projects") or []
        if isinstance(entry, dict)
    ]
    categories = [
        {"code": clean_text(entry.get("code")), "label": clean_text(entry.get("label"))}
        for entry in source.get("categories") or []
        if isinstance(entry, dict)
    ]
    materials = [
        {"code": clean_text(entry.get("code")), "label": clean_text(entry.get("label"))}
        for entry in source.get("materials") or []
        if isinstance(entry, dict)
    ]
    rules = []
    for entry in source.get("rules") or []:
        if not isinstance(entry, dict):
            continue
        min_cents = entry.get("min_amount_cents")
        max_cents = entry.get("max_amount_cents")
        rules.append(
            {
                "id": max(_integer(entry.get("id")), 1),
                "label": clean_text(entry.get("label")),
                "min_amount_cents": _cents_from_amount(entry.get("min_amount")) if min_cents is None else max(_integer(min_cents), 0),
                "max_amount_cents": (
                    None
                    if max_cents is None and entry.get("max_amount") is None
                    else (_cents_from_amount(entry.get("max_amount")) if max_cents is None else max(_integer(max_cents), 0))
                ),
                "required": [clean_text(code) for code in entry.get("required") or []],
            }
        )
    return {
        "projects": projects,
        "categories": categories,
        "materials": materials,
        "rules": rules,
        "requirements_version": max(_integer(source.get("requirements_version")), 0),
    }


def dashboard(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    counts = source.get("counts") if isinstance(source.get("counts"), dict) else {}
    keys = ("pending_confirmation", "pending_reimbursement", "submitted_unreimbursed", "missing_materials", "data_anomalies")
    return {
        "counts": {key: max(_integer(counts.get(key)), 0) for key in keys},
        "recent_batches": [batch_summary(entry) for entry in (source.get("recent_batches") or [])[:5]],
    }


def _resource_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    identifier = _integer(value.get("id"))
    if identifier <= 0:
        return None
    return {
        "resource_type": clean_text(value.get("resource_type") or value.get("type") or "resource"),
        "id": identifier,
        "version": _nullable_integer(value.get("version")),
    }


def write_data(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    if isinstance(source.get("operation_result"), dict):
        source = source["operation_result"]
    refs = []
    for entry in source.get("resource_refs") or []:
        ref = _resource_ref(entry)
        if ref:
            refs.append(ref)
    return {
        "resource_refs": refs,
        "artifact_available": bool(source.get("artifact_available")),
        "warning_codes": [clean_text(code) for code in source.get("warning_codes") or []],
    }


def operation(value: Any) -> dict[str, Any]:
    source = value.get("operation") if isinstance(value, dict) and isinstance(value.get("operation"), dict) else value
    source = source if isinstance(source, dict) else {}
    refs = []
    result = source.get("operation_result") if isinstance(source.get("operation_result"), dict) else source
    for entry in result.get("resource_refs") or []:
        ref = _resource_ref(entry)
        if ref:
            refs.append(ref)
    return {
        "operation_id": clean_text(source.get("operation_id") or source.get("id")),
        "operation_name": clean_text(source.get("operation_name") or "unknown"),
        "status": clean_text(source.get("status") or "unknown"),
        "outcome": source.get("outcome") if source.get("outcome") in ("applied", "not_applied", "unknown") else None,
        "resource_refs": refs,
        "artifact_available": bool(result.get("artifact_available")),
        "warning_codes": [clean_text(code) for code in result.get("warning_codes") or []],
        "created_at": clean_text(source.get("created_at") or "1970-01-01T00:00:00Z"),
        "updated_at": clean_text(source.get("updated_at") or source.get("created_at") or "1970-01-01T00:00:00Z"),
    }


def warning_list(value: Any) -> list[dict[str, str]]:
    warnings = []
    for entry in value or []:
        if isinstance(entry, dict):
            code = clean_text(entry.get("code"))
            message = clean_text(entry.get("message"))
        else:
            code = clean_text(entry)
            message = clean_text(entry)
        if code:
            warnings.append({"code": code, "message": message or code})
    return warnings
