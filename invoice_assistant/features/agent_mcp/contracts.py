from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mcp_types as types


SCHEMA_VERSION = "invoice-agent-v1"
EXTERNAL_NOTICE_VERSION = "deepseek-v1"
EXTERNAL_NOTICE = (
    "若本地后端已配置识别凭据，此文件内容将发送至 DeepSeek 进行识别；"
    "未配置时仅保存为手工处理记录。"
)
MAX_MONEY_CENTS = 10_000_000_000_000


def obj(
    properties: dict[str, Any],
    required: tuple[str, ...] | list[str] = (),
    *,
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


def array(
    items: dict[str, Any],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if minimum is not None:
        schema["minItems"] = minimum
    if maximum is not None:
        schema["maxItems"] = maximum
    return schema


STRING = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
ID = {"type": "integer", "minimum": 1}
VERSION = {"type": "integer", "minimum": 0}
OPERATION_ID = {"type": "string", "format": "uuid"}
CURSOR = {"type": "string", "minLength": 1, "maxLength": 8192}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
AMOUNT_CENTS = {"type": "integer", "minimum": 0, "maximum": MAX_MONEY_CENTS}


WARNING_SCHEMA = obj(
    {"code": NONEMPTY, "message": NONEMPTY},
    ("code", "message"),
)
PAGINATION_SCHEMA = obj(
    {"next_cursor": {"type": ["string", "null"]}, "has_more": {"type": "boolean"}},
    ("next_cursor", "has_more"),
)
META_SCHEMA = obj(
    {
        "request_id": {"type": ["string", "null"]},
        "operation_id": {"type": ["string", "null"]},
        "replayed": {"type": "boolean"},
        "pagination": {"oneOf": [PAGINATION_SCHEMA, {"type": "null"}]},
    },
    ("request_id", "operation_id", "replayed", "pagination"),
)
ERROR_SCHEMA = obj(
    {
        "code": NONEMPTY,
        "message": NONEMPTY,
        "http_status": {"type": "integer", "minimum": 0, "maximum": 599},
        "retryable": {"type": "boolean"},
        "outcome": {"type": "string", "enum": ["not_applied", "unknown"]},
    },
    ("code", "message", "http_status", "retryable", "outcome"),
)


def envelope(data_schema: dict[str, Any]) -> dict[str, Any]:
    schema = obj(
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "ok": {"type": "boolean"},
            "data": data_schema,
            "error": ERROR_SCHEMA,
            "warnings": array(WARNING_SCHEMA),
            "meta": META_SCHEMA,
        },
        ("schema_version", "ok", "warnings", "meta"),
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["oneOf"] = [
        {"properties": {"ok": {"const": True}}, "required": ["data"], "not": {"required": ["error"]}},
        {"properties": {"ok": {"const": False}}, "required": ["error"], "not": {"required": ["data"]}},
    ]
    return schema


RESOURCE_REF = obj(
    {"resource_type": NONEMPTY, "id": ID, "version": {"type": ["integer", "null"], "minimum": 0}},
    ("resource_type", "id", "version"),
)
WRITE_DATA = obj(
    {
        "resource_refs": array(RESOURCE_REF, minimum=1),
        "artifact_available": {"type": "boolean"},
        "warning_codes": array(NONEMPTY),
    },
    ("resource_refs", "artifact_available", "warning_codes"),
)

ATTACHMENT = obj(
    {
        "id": ID,
        "category": NONEMPTY,
        "display_name": STRING,
        "mime_type": NONEMPTY,
        "size_bytes": {"type": "integer", "minimum": 0},
        "recognition_error": {"type": ["string", "null"]},
    },
    ("id", "category", "display_name", "mime_type", "size_bytes", "recognition_error"),
)
MATERIAL = obj(
    {
        "complete": {"type": "boolean"},
        "percent": {"type": "integer", "minimum": 0, "maximum": 100},
        "missing": array(obj({"code": NONEMPTY, "label": STRING}, ("code", "label"))),
    },
    ("complete", "percent", "missing"),
)
BATCH_REF = obj(
    {"batch_id": ID, "batch_version": VERSION},
    ("batch_id", "batch_version"),
)
UNCERTAINTY = obj(
    {"uncertainty_id": NONEMPTY, "message": STRING},
    ("uncertainty_id", "message"),
)
DUPLICATE = obj(
    {
        "id": ID,
        "version": VERSION,
        "merchant": STRING,
        "expense_date": STRING,
        "amount_cents": AMOUNT_CENTS,
        "currency": NONEMPTY,
        "status": NONEMPTY,
        "confidence_milli": {"type": "integer", "minimum": 0, "maximum": 1000},
        "exact_file": {"type": "boolean"},
        "high_confidence": {"type": "boolean"},
        "historical": {"type": "boolean"},
        "merge_allowed": {"type": "boolean"},
        "reason": STRING,
    },
    (
        "id",
        "version",
        "merchant",
        "expense_date",
        "amount_cents",
        "currency",
        "status",
        "confidence_milli",
        "exact_file",
        "high_confidence",
        "historical",
        "merge_allowed",
        "reason",
    ),
)
REVIEW = obj(
    {
        "token": NONEMPTY,
        "uncertainties": array(UNCERTAINTY),
        "recognition_error": {"type": ["string", "null"]},
        "duplicate_candidates": array(DUPLICATE, maximum=100),
        "blocking_duplicate_ids": array(ID, maximum=100),
        "blocking_total": {"type": "integer", "minimum": 0},
        "duplicate_review_overflow": {"type": "boolean"},
    },
    (
        "token",
        "uncertainties",
        "recognition_error",
        "duplicate_candidates",
        "blocking_duplicate_ids",
        "blocking_total",
        "duplicate_review_overflow",
    ),
)


ITEM_SUMMARY = obj(
    {
        "id": ID,
        "version": VERSION,
        "merchant": STRING,
        "expense_date": STRING,
        "amount_cents": AMOUNT_CENTS,
        "currency": NONEMPTY,
        "converted_amount_cents": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_MONEY_CENTS},
        "purpose": STRING,
        "project_id": {"type": ["integer", "null"], "minimum": 1},
        "project_name": {"type": ["string", "null"]},
        "status": NONEMPTY,
        "material": MATERIAL,
        "batch_ref": {"oneOf": [BATCH_REF, {"type": "null"}]},
        "created_at": NONEMPTY,
        "updated_at": NONEMPTY,
        "truncated_fields": array(NONEMPTY),
    },
    (
        "id",
        "version",
        "merchant",
        "expense_date",
        "amount_cents",
        "currency",
        "converted_amount_cents",
        "purpose",
        "project_id",
        "project_name",
        "status",
        "material",
        "batch_ref",
        "created_at",
        "updated_at",
        "truncated_fields",
    ),
)
ITEM_DETAIL = obj(
    {
        **ITEM_SUMMARY["properties"],
        "attachments": array(ATTACHMENT),
        "review": {"oneOf": [REVIEW, {"type": "null"}]},
        "requirements_version": VERSION,
    },
    tuple(ITEM_SUMMARY["required"]) + ("attachments", "review", "requirements_version"),
)

BATCH_SUMMARY = obj(
    {
        "id": ID,
        "version": VERSION,
        "requirements_version": VERSION,
        "name": NONEMPTY,
        "project_id": {"type": ["integer", "null"], "minimum": 1},
        "project_name": {"type": ["string", "null"]},
        "purpose": STRING,
        "status": NONEMPTY,
        "total_amount_cents": AMOUNT_CENTS,
        "complete": {"type": "boolean"},
        "missing_item_count": {"type": "integer", "minimum": 0},
        "artifact_available": {"type": "boolean"},
        "created_at": NONEMPTY,
        "updated_at": NONEMPTY,
        "truncated_fields": array(NONEMPTY),
    },
    (
        "id",
        "version",
        "requirements_version",
        "name",
        "project_id",
        "project_name",
        "purpose",
        "status",
        "total_amount_cents",
        "complete",
        "missing_item_count",
        "artifact_available",
        "created_at",
        "updated_at",
        "truncated_fields",
    ),
)
BATCH_DETAIL = obj(
    {**BATCH_SUMMARY["properties"], "notes": STRING, "items": array(ITEM_SUMMARY, maximum=200)},
    tuple(BATCH_SUMMARY["required"]) + ("notes", "items"),
)

PROJECT = obj(
    {"id": ID, "name": NONEMPTY, "code": STRING, "enabled": {"type": "boolean"}},
    ("id", "name", "code", "enabled"),
)
REFERENCE_DATA = obj(
    {
        "projects": array(PROJECT),
        "categories": array(obj({"code": NONEMPTY, "label": STRING}, ("code", "label"))),
        "materials": array(obj({"code": NONEMPTY, "label": STRING}, ("code", "label"))),
        "rules": array(
            obj(
                {
                    "id": ID,
                    "label": STRING,
                    "min_amount_cents": AMOUNT_CENTS,
                    "max_amount_cents": {"type": ["integer", "null"], "minimum": 0},
                    "required": array(NONEMPTY),
                },
                ("id", "label", "min_amount_cents", "max_amount_cents", "required"),
            )
        ),
        "requirements_version": VERSION,
    },
    ("projects", "categories", "materials", "rules", "requirements_version"),
)
SERVICE_DATA = obj(
    {
        "status": {
            "type": "string",
            "enum": ["running", "degraded", "stopped", "timeout", "wrong_service", "registration_stale"],
        },
        "service": {"type": ["string", "null"]},
        "platform_hint": {"type": ["string", "null"]},
    },
    ("status", "service", "platform_hint"),
)
DASHBOARD_DATA = obj(
    {
        "counts": obj(
            {
                "pending_confirmation": {"type": "integer", "minimum": 0},
                "pending_reimbursement": {"type": "integer", "minimum": 0},
                "submitted_unreimbursed": {"type": "integer", "minimum": 0},
                "missing_materials": {"type": "integer", "minimum": 0},
                "data_anomalies": {"type": "integer", "minimum": 0},
            },
            (
                "pending_confirmation",
                "pending_reimbursement",
                "submitted_unreimbursed",
                "missing_materials",
                "data_anomalies",
            ),
        ),
        "recent_batches": array(BATCH_SUMMARY, maximum=5),
    },
    ("counts", "recent_batches"),
)
OPERATION_DATA = obj(
    {
        "operation_id": NONEMPTY,
        "operation_name": NONEMPTY,
        "status": NONEMPTY,
        "outcome": {"type": ["string", "null"], "enum": ["not_applied", "unknown", "applied", None]},
        "resource_refs": array(RESOURCE_REF),
        "artifact_available": {"type": "boolean"},
        "warning_codes": array(NONEMPTY),
        "created_at": NONEMPTY,
        "updated_at": NONEMPTY,
    },
    (
        "operation_id",
        "operation_name",
        "status",
        "outcome",
        "resource_refs",
        "artifact_available",
        "warning_codes",
        "created_at",
        "updated_at",
    ),
)


def input_schema(properties: dict[str, Any], required: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj(properties, required)}


ITEM_FIELDS = {
    "merchant": {"type": "string", "maxLength": 200},
    "expense_date": {"type": "string", "maxLength": 10},
    "amount_cents": AMOUNT_CENTS,
    "currency": {"type": "string", "pattern": "^[A-Z0-9]{2,8}$"},
    "converted_amount_cents": {"type": ["integer", "null"], "minimum": 0, "maximum": MAX_MONEY_CENTS},
    "purpose": {"type": "string", "maxLength": 2000},
    "project_id": {"type": ["integer", "null"], "minimum": 1},
}


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    annotations: types.ToolAnnotations
    file_tool: bool = False


def hints(read_only: bool, destructive: bool, idempotent: bool, open_world: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def contract(
    name: str,
    description: str,
    input_: dict[str, Any],
    output: dict[str, Any],
    r: bool,
    d: bool,
    i: bool,
    o: bool,
    *,
    file_tool: bool = False,
) -> ToolContract:
    return ToolContract(name, description, input_, envelope(output), hints(r, d, i, o), file_tool)


PAGINATION_INPUT = {"limit": LIMIT, "cursor": CURSOR}
OP = {"operation_id": OPERATION_ID}

TOOL_MANIFEST: tuple[ToolContract, ...] = (
    contract("get_service_status", "Check the fixed local invoice-assistant service without starting it.", input_schema({}), SERVICE_DATA, True, False, True, False),
    contract("get_invoice_reference_data", "Read projects, material rules, labels, and the current requirements version.", input_schema({}), REFERENCE_DATA, True, False, True, False),
    contract("get_dashboard_summary", "Read non-sensitive invoice dashboard counts and recent reimbursement batches.", input_schema({}), DASHBOARD_DATA, True, False, True, False),
    contract("get_agent_operation", "Read the safe persisted status of one Agent write operation.", input_schema({"operation_id": OPERATION_ID}, ("operation_id",)), OPERATION_DATA, True, False, True, False),
    contract(
        "import_invoice_file",
        "Import one invoice file. " + EXTERNAL_NOTICE,
        input_schema(
            {
                **OP,
                "file_path": NONEMPTY,
                "external_processing_notice_version": {"const": EXTERNAL_NOTICE_VERSION},
                "external_processing_ack": {"const": True},
            },
            ("operation_id", "file_path", "external_processing_notice_version", "external_processing_ack"),
        ),
        WRITE_DATA,
        False,
        False,
        True,
        True,
        file_tool=True,
    ),
    contract(
        "create_manual_invoice_draft",
        "Create a local invoice draft without reading a file or contacting an external service.",
        input_schema({**OP, **ITEM_FIELDS}, ("operation_id", "merchant", "expense_date", "amount_cents", "currency", "purpose", "project_id")),
        WRITE_DATA,
        False,
        False,
        True,
        False,
    ),
    contract("list_invoice_drafts", "List pending-confirmation drafts using stable keyset pagination.", input_schema(PAGINATION_INPUT), obj({"items": array(ITEM_SUMMARY)}, ("items",)), True, False, True, False),
    contract("get_invoice_item", "Read one invoice item, including its current review token when it is a draft.", input_schema({"item_id": ID}, ("item_id",)), ITEM_DETAIL, True, False, True, False),
    contract(
        "update_invoice_item",
        "Update explicit invoice fields. Read the item again before confirming it.",
        input_schema({**OP, "item_id": ID, "expected_version": VERSION, "expected_batch_version": VERSION, **ITEM_FIELDS}, ("operation_id", "item_id", "expected_version")),
        WRITE_DATA,
        False,
        True,
        True,
        False,
    ),
    contract(
        "confirm_invoice_item",
        "Confirm the already-saved current invoice snapshot after acknowledging every uncertainty and blocking duplicate.",
        input_schema(
            {
                **OP,
                "item_id": ID,
                "expected_version": VERSION,
                "review_token": NONEMPTY,
                "duplicate_resolution": {"type": "string", "enum": ["none", "keep_separate"]},
                "acknowledged_uncertainty_ids": array(NONEMPTY, maximum=1000),
                "acknowledged_duplicate_ids": array(ID, maximum=100),
            },
            ("operation_id", "item_id", "expected_version", "review_token", "duplicate_resolution", "acknowledged_uncertainty_ids", "acknowledged_duplicate_ids"),
        ),
        WRITE_DATA,
        False,
        True,
        True,
        False,
    ),
    contract(
        "merge_invoice_draft",
        "Merge a reviewed draft only into a currently reviewed mutable duplicate target.",
        input_schema(
            {
                **OP,
                "source_id": ID,
                "target_id": ID,
                "source_version": VERSION,
                "target_version": VERSION,
                "source_review_token": NONEMPTY,
                "expected_batch_version": VERSION,
            },
            ("operation_id", "source_id", "target_id", "source_version", "target_version", "source_review_token"),
        ),
        WRITE_DATA,
        False,
        True,
        True,
        False,
    ),
    contract(
        "list_invoice_items",
        "List invoice items using stable keyset pagination and explicit filters.",
        input_schema(
            {
                **PAGINATION_INPUT,
                "status": {"type": "string", "enum": ["pending_confirmation", "pending_reimbursement", "in_batch", "submitted", "reimbursed"]},
                "project_id": ID,
                "date_from": STRING,
                "date_to": STRING,
                "amount_min_cents": AMOUNT_CENTS,
                "amount_max_cents": AMOUNT_CENTS,
                "material": {"type": "string", "enum": ["complete", "missing"]},
                "search": {"type": "string", "maxLength": 200},
            }
        ),
        obj({"items": array(ITEM_SUMMARY)}, ("items",)),
        True,
        False,
        True,
        False,
    ),
    contract(
        "add_invoice_attachment",
        "Add one local file to a mutable invoice. For payment records: " + EXTERNAL_NOTICE,
        {
            **input_schema(
                {
                    **OP,
                    "item_id": ID,
                    "expected_version": VERSION,
                    "expected_batch_version": VERSION,
                    "file_path": NONEMPTY,
                    "category": {"type": "string", "enum": ["invoice", "foreign_invoice", "receipt", "purchase_list", "payment_record", "unknown"]},
                    "external_processing_notice_version": {"type": "string"},
                    "external_processing_ack": {"type": "boolean"},
                },
                ("operation_id", "item_id", "expected_version", "file_path", "category"),
            ),
            "allOf": [
                {
                    "if": {"properties": {"category": {"const": "payment_record"}}, "required": ["category"]},
                    "then": {
                        "properties": {
                            "external_processing_notice_version": {"const": EXTERNAL_NOTICE_VERSION},
                            "external_processing_ack": {"const": True},
                        },
                        "required": ["external_processing_notice_version", "external_processing_ack"],
                    },
                }
            ],
        },
        WRITE_DATA,
        False,
        True,
        True,
        True,
        file_tool=True,
    ),
    contract(
        "create_reimbursement_batch",
        "Create one reimbursement batch from explicitly versioned pending items.",
        input_schema(
            {
                **OP,
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "project_id": ID,
                "purpose": {"type": "string", "maxLength": 2000},
                "notes": {"type": "string", "maxLength": 4000},
                "items": array(obj({"item_id": ID, "expected_version": VERSION}, ("item_id", "expected_version")), maximum=200),
            },
            ("operation_id", "name", "project_id", "items"),
        ),
        WRITE_DATA,
        False,
        True,
        True,
        False,
    ),
    contract(
        "list_reimbursement_batches",
        "List reimbursement batches using stable keyset pagination.",
        input_schema({**PAGINATION_INPUT, "status": {"type": "string", "enum": ["draft", "submitted", "reimbursed"]}, "project_id": ID}),
        obj({"batches": array(BATCH_SUMMARY)}, ("batches",)),
        True,
        False,
        True,
        False,
    ),
    contract("get_reimbursement_batch", "Read one reimbursement batch and its current concurrency versions.", input_schema({"batch_id": ID}, ("batch_id",)), BATCH_DETAIL, True, False, True, False),
    contract(
        "export_reimbursement_batch",
        "Generate the local archive only when every material requirement and exact batch-name confirmation still matches.",
        input_schema(
            {
                **OP,
                "batch_id": ID,
                "expected_version": VERSION,
                "expected_requirements_version": VERSION,
                "confirmation_name": NONEMPTY,
            },
            ("operation_id", "batch_id", "expected_version", "expected_requirements_version", "confirmation_name"),
        ),
        WRITE_DATA,
        False,
        True,
        True,
        False,
    ),
)

if len(TOOL_MANIFEST) != 17 or len({entry.name for entry in TOOL_MANIFEST}) != 17:
    raise RuntimeError("The invoice Agent manifest must contain exactly 17 uniquely named tools.")


def exposed_contracts(has_allowed_roots: bool) -> tuple[ToolContract, ...]:
    contracts = tuple(entry for entry in TOOL_MANIFEST if has_allowed_roots or not entry.file_tool)
    expected = 17 if has_allowed_roots else 15
    if len(contracts) != expected:
        raise RuntimeError("Unexpected invoice Agent exposed tool count.")
    return contracts


def tool_names(has_allowed_roots: bool) -> list[str]:
    return [entry.name for entry in exposed_contracts(has_allowed_roots)]
