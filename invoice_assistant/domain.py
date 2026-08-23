from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path

from . import AppError
from .db import json_dump, json_load, utc_now


CATEGORY_META = {
    "invoice": {"label": "发票", "material": "primary_receipt"},
    "foreign_invoice": {"label": "Invoice", "material": "primary_receipt"},
    "receipt": {"label": "Receipt", "material": "primary_receipt"},
    "purchase_list": {"label": "购入清单", "material": "purchase_list"},
    "payment_record": {"label": "支付记录", "material": "payment_record"},
    "unknown": {"label": "未知材料", "material": "unknown"},
}

MATERIAL_META = {
    "primary_receipt": {"label": "主凭据", "categories": ["invoice", "foreign_invoice", "receipt"]},
    "purchase_list": {"label": "购入清单", "categories": ["purchase_list"]},
    "payment_record": {"label": "支付记录", "categories": ["payment_record"]},
}

STATUS_LABELS = {
    "pending_confirmation": "待确认",
    "pending_reimbursement": "待报销",
    "in_batch": "报销包处理中",
    "submitted": "已提交",
    "reimbursed": "已报销",
    "merged": "已合并",
}

MAX_MONEY_CENTS = 100_000_000_000_00
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def normalize_untrusted_text(value, *, strip: bool = True) -> str:
    """Normalize user/recognizer text without changing meaningful Unicode text."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(character for character in text if unicodedata.category(character) not in {"Cc", "Cf"})
    return text.strip() if strip else text


def money_to_cents(value, label: str = "金额", *, reject_json_float: bool = False) -> int:
    if reject_json_float and isinstance(value, float):
        raise AppError(f"{label}不得使用 JSON 浮点数。", 400, "json_float_not_allowed")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(quantized * 100)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise AppError(f"{label}格式不正确。", 400, "invalid_amount")
    if cents > MAX_MONEY_CENTS:
        raise AppError(f"{label}超出支持范围。", 400, "invalid_amount")
    return cents


def parse_amount(value) -> float:
    cents = money_to_cents(value)
    return cents / 100


def cents_to_amount(value: int | None) -> float | None:
    return None if value is None else int(value) / 100


def reimbursement_cents(row) -> int | None:
    currency = str(row["currency"] if not isinstance(row, dict) else row.get("currency") or "CNY").upper()
    if currency == "CNY":
        value = row["amount_cents"] if "amount_cents" in row.keys() else None
        if value is None:
            return money_to_cents(row["amount"])
        return int(value)
    value = row["converted_amount_cents"] if "converted_amount_cents" in row.keys() else None
    if value is None:
        converted = row["converted_amount"] if "converted_amount" in row.keys() else None
        return None if converted is None else money_to_cents(converted, "人民币实付金额")
    return int(value)


def payment_rmb_cents(recognition: dict | None) -> int | None:
    """Return an explicitly recognized RMB payment amount, if present."""
    if not recognition:
        return None
    converted = recognition.get("converted_amount")
    if converted not in (None, ""):
        cents = money_to_cents(converted, "人民币实付金额")
        return cents if cents > 0 else None
    if str(recognition.get("currency") or "").upper() == "CNY" and recognition.get("amount") not in (None, ""):
        cents = money_to_cents(recognition["amount"], "人民币实付金额")
        return cents if cents > 0 else None
    return None


def refresh_foreign_payment_amount(db, item_id: int) -> None:
    """Refresh an item's RMB reimbursement amount from its payment records."""
    item = db.execute(
        "SELECT currency,converted_amount,converted_amount_cents,uncertainties_json FROM expense_items WHERE id=?",
        (item_id,),
    ).fetchone()
    if not item or item["currency"] == "CNY":
        return
    amounts = {
        cents
        for row in db.execute(
            "SELECT ai_raw_json FROM attachments WHERE expense_item_id=? AND category='payment_record'",
            (item_id,),
        ).fetchall()
        if (cents := payment_rmb_cents(json_load(row["ai_raw_json"], None)))
    }
    payment_count = db.execute(
        "SELECT COUNT(*) AS n FROM attachments WHERE expense_item_id=? AND category='payment_record'",
        (item_id,),
    ).fetchone()["n"]
    uncertainties = json_load(item["uncertainties_json"], [])
    marker = "多份支付记录的人民币金额不一致，请手工确认实际付款金额。"
    uncertainties = [entry for entry in uncertainties if entry != marker]
    if not payment_count:
        db.execute(
            "UPDATE expense_items SET converted_amount=NULL,converted_amount_cents=NULL,uncertainties_json=?,updated_at=? WHERE id=?",
            (json_dump(uncertainties), utc_now(), item_id),
        )
    elif len(amounts) == 1:
        cents = next(iter(amounts))
        db.execute(
            "UPDATE expense_items SET converted_amount=?,converted_amount_cents=?,uncertainties_json=?,updated_at=? WHERE id=?",
            (cents / 100, cents, json_dump(uncertainties), utc_now(), item_id),
        )
    elif len(amounts) > 1:
        uncertainties.append(marker)
        db.execute(
            "UPDATE expense_items SET converted_amount=NULL,converted_amount_cents=NULL,uncertainties_json=?,updated_at=? WHERE id=?",
            (json_dump(uncertainties), utc_now(), item_id),
        )


def validate_item_payload(payload: dict, require_complete: bool = False, *, reject_json_float: bool = False) -> dict:
    amount_cents = money_to_cents(payload.get("amount", 0), reject_json_float=reject_json_float)
    merchant = normalize_untrusted_text(payload.get("merchant"))
    purpose = normalize_untrusted_text(payload.get("purpose"))
    currency = normalize_untrusted_text(payload.get("currency") or "CNY").upper()
    if len(merchant) > 200:
        raise AppError("商户/收款方最多 200 个字符。", 400, "field_too_long")
    if len(purpose) > 2000:
        raise AppError("用途最多 2,000 个字符。", 400, "field_too_long")
    if not re.fullmatch(r"[A-Z0-9]{2,8}", currency):
        raise AppError("币种应使用 2 至 8 位大写字母或数字代码。", 400, "invalid_currency")
    result = {
        "merchant": merchant,
        "expense_date": normalize_untrusted_text(payload.get("expense_date")),
        "amount": cents_to_amount(amount_cents),
        "amount_cents": amount_cents,
        "currency": currency,
        "converted_amount": None,
        "converted_amount_cents": None,
        "purpose": purpose,
        "project_id": payload.get("project_id") or None,
    }
    converted = payload.get("converted_amount")
    if converted not in (None, ""):
        result["converted_amount_cents"] = money_to_cents(
            converted, "人民币实付金额", reject_json_float=reject_json_float
        )
        result["converted_amount"] = cents_to_amount(result["converted_amount_cents"])
    if result["expense_date"]:
        try:
            date.fromisoformat(result["expense_date"])
        except ValueError:
            raise AppError("消费日期必须为 YYYY-MM-DD 格式。", 400, "invalid_date")
    if result["project_id"] is not None:
        if isinstance(result["project_id"], bool):
            raise AppError("报销项目无效。", 400, "invalid_project")
        try:
            result["project_id"] = int(result["project_id"])
        except (TypeError, ValueError):
            raise AppError("报销项目无效。", 400, "invalid_project")
    if require_complete:
        missing = []
        if not result["merchant"]:
            missing.append("商户/收款方")
        if not result["expense_date"]:
            missing.append("消费日期")
        if result["amount"] <= 0:
            missing.append("金额")
        if not result["currency"]:
            missing.append("币种")
        if not result["purpose"]:
            missing.append("用途")
        if result["project_id"] is None:
            missing.append("报销项目")
        if missing:
            raise AppError("请补全：" + "、".join(missing), 400, "incomplete_item")
    return result


def get_rules(db) -> list[dict]:
    rows = db.execute("SELECT * FROM material_rules ORDER BY sort_order, min_amount, id").fetchall()
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "min_amount": row["min_amount"],
            "max_amount": row["max_amount"],
            "required": json_load(row["required_json"], []),
            "sort_order": row["sort_order"],
        }
        for row in rows
    ]


def get_requirements_version(db) -> int:
    row = db.execute("SELECT requirements_version FROM requirements_state WHERE id=1").fetchone()
    if not row:
        raise AppError("材料规则版本状态缺失，请先执行数据库迁移。", 503, "migration_required")
    return int(row["requirements_version"])


def get_materials(db) -> list[dict]:
    stored = {
        row["code"]: row["label"]
        for row in db.execute("SELECT code,label FROM material_types").fetchall()
    }
    return [
        {"code": code, "label": stored.get(code, meta["label"]), "categories": meta["categories"]}
        for code, meta in MATERIAL_META.items()
    ]


def rule_for_amount(db, amount: float) -> dict:
    for rule in get_rules(db):
        if amount >= rule["min_amount"] and (rule["max_amount"] is None or amount < rule["max_amount"]):
            return rule
    raise AppError("当前金额没有匹配的材料规则，请先在设置中修正规则。", 409, "material_rule_gap")


def material_status(db, item_id: int, amount: float | None = None) -> dict:
    item = db.execute(
        "SELECT amount,amount_cents,currency,converted_amount,converted_amount_cents FROM expense_items WHERE id=?",
        (item_id,),
    ).fetchone()
    if amount is None:
        if not item:
            raise AppError("条目不存在。", 404, "item_not_found")
        reimbursable = reimbursement_cents(item)
        amount = cents_to_amount(reimbursable) if reimbursable is not None else 0
    rule = rule_for_amount(db, float(amount))
    categories = {
        row["category"]
        for row in db.execute("SELECT category FROM attachments WHERE expense_item_id=?", (item_id,)).fetchall()
    }
    material_meta = {entry["code"]: entry for entry in get_materials(db)}
    requirements = []
    missing = []
    foreign_item = bool(item and item["currency"] != "CNY")
    if foreign_item:
        paid_cents = reimbursement_cents(item)
        payment_satisfied = bool(paid_cents and "payment_record" in categories)
        payment_requirement = {
            "code": "foreign_payment_rmb",
            "label": "人民币实付记录",
            "satisfied": payment_satisfied,
        }
        requirements.append(payment_requirement)
        if not payment_satisfied:
            missing.append({"code": payment_requirement["code"], "label": payment_requirement["label"]})
    for code in rule["required"]:
        if foreign_item and code == "payment_record":
            continue
        meta = material_meta.get(code)
        if not meta:
            continue
        satisfied = any(category in categories for category in meta["categories"])
        requirements.append({"code": code, "label": meta["label"], "satisfied": satisfied})
        if not satisfied:
            missing.append({"code": code, "label": meta["label"]})
    return {
        "rule": rule,
        "requirements": requirements,
        "missing": missing,
        "complete": not missing,
        "percent": 100 if not requirements else round(100 * (len(requirements) - len(missing)) / len(requirements)),
    }


def serialize_attachment(row) -> dict:
    return {
        "id": row["id"],
        "expense_item_id": row["expense_item_id"],
        "category": row["category"],
        "category_label": CATEGORY_META[row["category"]]["label"],
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "ai_raw": json_load(row["ai_raw_json"], None),
        "recognition_error": row["recognition_error"] if "recognition_error" in row.keys() else None,
        "page_order": row["page_order"],
        "name_locked": bool(row["name_locked"]) if "name_locked" in row.keys() else False,
        "created_at": row["created_at"],
        "rename_history": json_load(row["rename_history_json"], []),
        "download_url": f"/api/attachments/{row['id']}/download",
        "preview_url": f"/api/attachments/{row['id']}/preview",
        "thumbnail_url": f"/api/attachments/{row['id']}/thumbnail",
    }


def serialize_item(db, row_or_id, include_audit: bool = False) -> dict:
    if isinstance(row_or_id, int):
        row = db.execute(
            """
            SELECT i.*, p.name AS project_name, p.code AS project_code,
                   bi.batch_id, b.name AS batch_name, b.status AS batch_status,
                   b.export_token AS batch_export_token, b.row_version AS batch_row_version
            FROM expense_items i
            LEFT JOIN projects p ON p.id=i.project_id
            LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
            LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
            WHERE i.id=?
            """,
            (row_or_id,),
        ).fetchone()
    else:
        row = row_or_id
    if not row:
        raise AppError("条目不存在。", 404, "item_not_found")
    attachments = [
        serialize_attachment(a)
        for a in db.execute(
            """SELECT * FROM attachments WHERE expense_item_id=?
               ORDER BY CASE category WHEN 'invoice' THEN 0 WHEN 'foreign_invoice' THEN 0 WHEN 'receipt' THEN 0
                        WHEN 'purchase_list' THEN 1 WHEN 'payment_record' THEN 2 ELSE 3 END,
                        page_order, id""",
            (row["id"],),
        ).fetchall()
    ]
    reimbursable_cents = reimbursement_cents(row)
    material = material_status(db, row["id"], cents_to_amount(reimbursable_cents) if reimbursable_cents is not None else 0)
    item = {
        "id": row["id"],
        "version": int(row["row_version"]) if "row_version" in row.keys() else 0,
        "merchant": row["merchant"],
        "expense_date": row["expense_date"],
        "amount": row["amount"],
        "amount_cents": row["amount_cents"] if "amount_cents" in row.keys() else money_to_cents(row["amount"]),
        "currency": row["currency"],
        "converted_amount": row["converted_amount"],
        "converted_amount_cents": row["converted_amount_cents"] if "converted_amount_cents" in row.keys() else (
            None if row["converted_amount"] is None else money_to_cents(row["converted_amount"], "人民币实付金额")
        ),
        "reimbursement_amount": cents_to_amount(reimbursable_cents),
        "reimbursement_amount_cents": reimbursable_cents,
        "reimbursement_currency": "CNY",
        "foreign_payment_required": row["currency"] != "CNY",
        "purpose": row["purpose"],
        "project_id": row["project_id"],
        "project_name": row["project_name"] if "project_name" in row.keys() else None,
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "ai_raw": json_load(row["ai_raw_json"], None),
        "confirmed_snapshot": json_load(row["confirmed_json"], None),
        "uncertainties": json_load(row["uncertainties_json"], []),
        "recognition_error": row["recognition_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "confirmed_at": row["confirmed_at"],
        "submitted_at": row["submitted_at"],
        "reimbursed_at": row["reimbursed_at"],
        "batch_id": row["batch_id"] if "batch_id" in row.keys() else None,
        "batch_name": row["batch_name"] if "batch_name" in row.keys() else None,
        "batch_status": row["batch_status"] if "batch_status" in row.keys() else None,
        "batch_exporting": bool(row["batch_export_token"]) if "batch_export_token" in row.keys() else False,
        "batch_ref": (
            {
                "batch_id": row["batch_id"],
                "batch_version": int(row["batch_row_version"]) if "batch_row_version" in row.keys() else 0,
            }
            if "batch_id" in row.keys() and row["batch_id"] is not None
            else None
        ),
        "requirements_version": get_requirements_version(db),
        "attachments": attachments,
        "material": material,
    }
    if include_audit:
        item["audit_logs"] = serialize_audit_logs(db, "item", row["id"])
    return item


def serialize_audit_logs(db, object_type: str, object_id: int) -> list[dict]:
    return [
        {
            "id": row["id"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "action": row["action"],
            "details": json_load(row["details_json"], {}),
            "created_at": row["created_at"],
        }
        for row in db.execute(
            "SELECT * FROM audit_logs WHERE object_type=? AND object_id=? ORDER BY created_at, id",
            (object_type, object_id),
        ).fetchall()
    ]


def sanitize_component(value: str, fallback: str, limit: int = 34) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "").strip())
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    result = (value or fallback)[:limit]
    if result.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        result = f"_{result}"
    return result


def normalized_attachment_name(item: dict, category: str, original_name: str, sequence: int) -> str:
    extension = Path(original_name).suffix.lower()
    if extension not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
        extension = ".bin"
    amount = f"{float(item['amount']):.2f}{sanitize_component(item['currency'], 'CNY', 8)}"
    if item.get("converted_amount") is not None and item.get("currency") != "CNY":
        amount += f"_折合{float(item['converted_amount']):.2f}CNY"
    fields = [
        sanitize_component(item.get("expense_date"), "未知日期", 10),
        sanitize_component(item.get("project_name"), "未分项目"),
        sanitize_component(item.get("merchant"), "未知商户"),
        amount,
        sanitize_component(CATEGORY_META.get(category, CATEGORY_META["unknown"])["label"], "未知材料"),
        sanitize_component(item.get("purpose"), "未填用途"),
        f"{sequence:03d}",
    ]
    return "_".join(fields) + extension


def find_duplicate_candidates(db, draft_item_id: int, limit: int | None = 3) -> list[dict]:
    current = serialize_item(db, draft_item_id)
    current_hashes = {
        row["sha256"]
        for row in db.execute("SELECT sha256 FROM attachments WHERE expense_item_id=?", (draft_item_id,)).fetchall()
    }
    rows = db.execute(
        """
        SELECT i.*, p.name AS project_name, p.code AS project_code,
               bi.batch_id, b.name AS batch_name, b.status AS batch_status,
                b.export_token AS batch_export_token, b.row_version AS batch_row_version
        FROM expense_items i
        LEFT JOIN projects p ON p.id=i.project_id
        LEFT JOIN batch_items bi ON bi.expense_item_id=i.id
        LEFT JOIN reimbursement_batches b ON b.id=bi.batch_id
        WHERE i.id<>? AND i.status<>'merged' AND (
            EXISTS (
                SELECT 1 FROM attachments current_attachment
                JOIN attachments candidate_attachment ON candidate_attachment.sha256=current_attachment.sha256
                WHERE current_attachment.expense_item_id=? AND candidate_attachment.expense_item_id=i.id
            )
            OR (i.currency=? AND ABS(i.amount_cents-?)<=1)
            OR (?<>'' AND i.expense_date=?)
            OR (?<>'' AND i.merchant LIKE ?)
        )
        ORDER BY i.created_at DESC,i.id DESC
        """,
        (
            draft_item_id,
            draft_item_id,
            current["currency"],
            current["amount_cents"],
            current["expense_date"],
            current["expense_date"],
            current["merchant"],
            f"%{current['merchant']}%",
        ),
    ).fetchall()
    scored = []
    for row in rows:
        candidate = serialize_item(db, row)
        candidate_hashes = {attachment["sha256"] for attachment in candidate["attachments"]}
        exact_file = bool(current_hashes & candidate_hashes)
        score = 0.0
        amount_delta = abs(float(current["amount"]) - float(candidate["amount"]))
        score += 0.35 if amount_delta <= 0.01 else max(0, 0.35 - amount_delta / max(float(current["amount"]), 1))
        score += 0.10 if current["currency"] == candidate["currency"] else 0
        score += 0.20 if current["expense_date"] and current["expense_date"] == candidate["expense_date"] else 0
        merchant_ratio = SequenceMatcher(None, current["merchant"].casefold(), candidate["merchant"].casefold()).ratio()
        purpose_ratio = SequenceMatcher(None, current["purpose"].casefold(), candidate["purpose"].casefold()).ratio()
        score += 0.25 * merchant_ratio + 0.10 * purpose_ratio
        if exact_file:
            score = 1.0
        score = round(min(1, score), 3)
        if score >= 0.6:
            reasons = []
            if exact_file:
                reasons.append("exact_file")
            if score >= 0.82:
                reasons.append("high_confidence")
            historical = candidate["status"] in {"submitted", "reimbursed"}
            if historical:
                reasons.append("historical")
            scored.append(
                {
                    "confidence": score,
                    "high_confidence": score >= 0.82,
                    "exact_file": exact_file,
                    "historical": historical,
                    "blocking": bool(reasons),
                    "reason": reasons,
                    "merge_allowed": candidate["status"] in {
                        "pending_confirmation", "pending_reimbursement", "in_batch"
                    },
                    "item": candidate,
                }
            )
    ordered = sorted(scored, key=lambda entry: (-entry["confidence"], -entry["item"]["id"]))
    return ordered if limit is None else ordered[:limit]


def project_exists(db, project_id: int | None) -> bool:
    return bool(project_id and db.execute("SELECT 1 FROM projects WHERE id=? AND enabled=1", (project_id,)).fetchone())


def batch_completeness(db, batch_id: int) -> dict:
    item_rows = db.execute(
        "SELECT i.id, i.amount, i.amount_cents, i.currency, i.converted_amount, i.converted_amount_cents FROM expense_items i JOIN batch_items bi ON bi.expense_item_id=i.id WHERE bi.batch_id=? ORDER BY bi.sort_order, i.id",
        (batch_id,),
    ).fetchall()
    items = []
    for row in item_rows:
        cents = reimbursement_cents(row)
        status = material_status(db, row["id"], cents_to_amount(cents) if cents is not None else 0)
        items.append({"item_id": row["id"], **status})
    missing = [entry for entry in items if not entry["complete"]]
    return {
        "items": items,
        "complete": bool(items) and not missing,
        "missing_item_count": len(missing),
        "percent": 0 if not items else round(sum(entry["percent"] for entry in items) / len(items)),
    }
