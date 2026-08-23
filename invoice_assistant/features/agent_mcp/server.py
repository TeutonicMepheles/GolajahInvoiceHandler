from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

import anyio
import mcp_types as types
from jsonschema import Draft202012Validator, FormatChecker
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import dto
from .client import ClientError, HttpResult, LocalInvoiceClient
from .contracts import (
    EXTERNAL_NOTICE_VERSION,
    MAX_MONEY_CENTS,
    SCHEMA_VERSION,
    TOOL_MANIFEST,
    ToolContract,
    exposed_contracts,
)
from .file_access import FileAccessError, VerifiedFile, parse_allowed_roots, read_verified_file


logger = logging.getLogger("invoice_assistant.agent_mcp")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s invoice-agent %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.WARNING)
logger.propagate = False


FILE_TOOLS = frozenset(entry.name for entry in TOOL_MANIFEST if entry.file_tool)
WRITE_TOOLS = frozenset(
    entry.name for entry in TOOL_MANIFEST if not entry.annotations.read_only_hint
)
TOOL_DEADLINE_SECONDS = 350


def _meta(
    *,
    request_id: str | None = None,
    operation_id: str | None = None,
    replayed: bool = False,
    pagination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "operation_id": operation_id,
        "replayed": bool(replayed),
        "pagination": pagination,
    }


def success(
    data: dict[str, Any],
    *,
    warnings: list[dict[str, str]] | None = None,
    request_id: str | None = None,
    operation_id: str | None = None,
    replayed: bool = False,
    pagination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "data": data,
        "warnings": warnings or [],
        "meta": _meta(
            request_id=request_id,
            operation_id=operation_id,
            replayed=replayed,
            pagination=pagination,
        ),
    }


def failure(
    code: str,
    message: str,
    *,
    http_status: int = 0,
    retryable: bool = False,
    outcome: str = "not_applied",
    request_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {
            "code": dto.clean_text(code)[:120] or "tool_error",
            "message": dto.clean_text(message)[:1000] or "The tool failed safely.",
            "http_status": min(max(int(http_status or 0), 0), 599),
            "retryable": bool(retryable),
            "outcome": outcome if outcome in {"not_applied", "unknown"} else "unknown",
        },
        "warnings": [],
        "meta": _meta(request_id=request_id, operation_id=operation_id),
    }


def _pagination(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    page = meta.get("pagination") if isinstance(meta.get("pagination"), dict) else payload
    cursor = page.get("next_cursor")
    return {"next_cursor": None if cursor in (None, "") else str(cursor), "has_more": bool(page.get("has_more"))}


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def _invalid_service_response(tool_name: str) -> None:
    raise ClientError(
        "invalid_service_response",
        "The local service returned an unexpected success shape.",
        outcome="unknown" if tool_name in WRITE_TOOLS else "not_applied",
    )


def _is_integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _is_nullable_text(value: Any) -> bool:
    return value is None or _is_text(value)


def _is_money(value: Any) -> bool:
    return _is_integer(value) and value <= MAX_MONEY_CENTS


def _validate_material_source(value: Any, tool_name: str) -> None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("complete"), bool)
        or not _is_integer(value.get("percent"))
        or value["percent"] > 100
        or not isinstance(value.get("missing"), list)
    ):
        _invalid_service_response(tool_name)
    for entry in value["missing"]:
        if (
            not isinstance(entry, dict)
            or not _is_text(entry.get("code"))
            or not entry.get("code")
            or not _is_text(entry.get("label"))
        ):
            _invalid_service_response(tool_name)


def _validate_attachment_source(value: Any, tool_name: str) -> None:
    if not isinstance(value, dict):
        _invalid_service_response(tool_name)
    display_name = value.get("display_name") or value.get("normalized_name") or value.get("original_name")
    if (
        not _is_integer(value.get("id"), minimum=1)
        or not _is_text(value.get("category"))
        or not value.get("category")
        or not _is_text(display_name)
        or not _is_text(value.get("mime_type"))
        or not value.get("mime_type")
        or not _is_integer(value.get("size_bytes"))
        or not _is_nullable_text(value.get("recognition_error"))
    ):
        _invalid_service_response(tool_name)


def _validate_duplicate_source(value: Any, tool_name: str) -> None:
    if not isinstance(value, dict):
        _invalid_service_response(tool_name)
    nested = value.get("item") if isinstance(value.get("item"), dict) else value
    confidence = value.get("confidence_milli")
    if confidence is None:
        confidence = value.get("confidence")
        confidence_valid = (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
        )
    else:
        confidence_valid = _is_integer(confidence) and confidence <= 1000
    reason = value.get("reason")
    if (
        not _is_integer(nested.get("id"), minimum=1)
        or not _is_integer(nested.get("version", nested.get("row_version")))
        or not _is_text(nested.get("merchant"))
        or not _is_text(nested.get("expense_date"))
        or not _is_money(nested.get("amount_cents"))
        or not _is_text(nested.get("currency"))
        or not nested.get("currency")
        or not _is_text(nested.get("status"))
        or not nested.get("status")
        or not confidence_valid
        or any(not isinstance(value.get(key), bool) for key in ("exact_file", "high_confidence", "historical", "merge_allowed"))
        or not (
            _is_text(reason)
            or isinstance(reason, list)
            and all(_is_text(entry) for entry in reason)
        )
    ):
        _invalid_service_response(tool_name)


def _validate_review_source(value: Any, tool_name: str) -> None:
    if not isinstance(value, dict) or not _is_text(value.get("token")) or not value.get("token"):
        _invalid_service_response(tool_name)
    uncertainties = value.get("uncertainties")
    candidates = value.get("duplicate_candidates")
    blocking_ids = value.get("blocking_duplicate_ids")
    if (
        not isinstance(uncertainties, list)
        or not isinstance(candidates, list)
        or len(candidates) > 100
        or not isinstance(blocking_ids, list)
        or len(blocking_ids) > 100
        or not all(_is_integer(identifier, minimum=1) for identifier in blocking_ids)
        or not _is_integer(value.get("blocking_total"))
        or not isinstance(value.get("duplicate_review_overflow"), bool)
        or not _is_nullable_text(value.get("recognition_failure", value.get("recognition_error")))
    ):
        _invalid_service_response(tool_name)
    for entry in uncertainties:
        if (
            not isinstance(entry, dict)
            or not _is_text(entry.get("uncertainty_id"))
            or not entry.get("uncertainty_id")
            or not _is_text(entry.get("message"))
        ):
            _invalid_service_response(tool_name)
    for candidate in candidates:
        _validate_duplicate_source(candidate, tool_name)


def _validate_success_metadata(payload: dict[str, Any], tool_name: str) -> None:
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        _invalid_service_response(tool_name)
    for warning in warnings:
        if _is_text(warning):
            continue
        if (
            not isinstance(warning, dict)
            or not _is_text(warning.get("code"))
            or not warning.get("code")
            or not _is_text(warning.get("message"))
        ):
            _invalid_service_response(tool_name)
    meta = payload.get("meta")
    if meta is not None and (
        not isinstance(meta, dict)
        or not _is_nullable_text(meta.get("request_id"))
        or "replayed" in meta
        and not isinstance(meta.get("replayed"), bool)
    ):
        _invalid_service_response(tool_name)


def _validate_pagination(payload: dict[str, Any], tool_name: str) -> None:
    meta = payload.get("meta")
    page = meta.get("pagination") if isinstance(meta, dict) else None
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("has_more"), bool)
        or page.get("next_cursor") is not None
        and not isinstance(page.get("next_cursor"), str)
    ):
        _invalid_service_response(tool_name)


def _validate_item_source(value: Any, tool_name: str, *, detail: bool = False) -> None:
    if not isinstance(value, dict):
        _invalid_service_response(tool_name)
    required_integers = {"id": 1, "version": 0, "amount_cents": 0, "requirements_version": 0}
    if any(not _is_integer(value.get(key), minimum=minimum) for key, minimum in required_integers.items()):
        _invalid_service_response(tool_name)
    if not _is_money(value.get("amount_cents")):
        _invalid_service_response(tool_name)
    if not all(
        _is_text(value.get(key))
        for key in ("merchant", "expense_date", "currency", "purpose", "status", "created_at", "updated_at")
    ):
        _invalid_service_response(tool_name)
    if any(not value.get(key) for key in ("currency", "status", "created_at", "updated_at")):
        _invalid_service_response(tool_name)
    converted = value.get("converted_amount_cents")
    if converted is not None and not _is_money(converted):
        _invalid_service_response(tool_name)
    project_id = value.get("project_id")
    if project_id is not None and not _is_integer(project_id, minimum=1):
        _invalid_service_response(tool_name)
    if not _is_nullable_text(value.get("project_name")):
        _invalid_service_response(tool_name)
    _validate_material_source(value.get("material"), tool_name)
    batch_ref = value.get("batch_ref")
    if batch_ref is not None and (
        not isinstance(batch_ref, dict)
        or not _is_integer(batch_ref.get("batch_id"), minimum=1)
        or not _is_integer(batch_ref.get("batch_version"))
    ):
        _invalid_service_response(tool_name)
    if detail:
        attachments = value.get("attachments")
        if not isinstance(attachments, list):
            _invalid_service_response(tool_name)
        for attachment in attachments:
            _validate_attachment_source(attachment, tool_name)
        review = value.get("review")
        if review is not None:
            _validate_review_source(review, tool_name)


def _validate_batch_source(value: Any, tool_name: str, *, detail: bool = False) -> None:
    if not isinstance(value, dict):
        _invalid_service_response(tool_name)
    if any(
        not _is_integer(value.get(key), minimum=minimum)
        for key, minimum in {"id": 1, "version": 0, "requirements_version": 0, "total_amount_cents": 0}.items()
    ):
        _invalid_service_response(tool_name)
    if not _is_money(value.get("total_amount_cents")):
        _invalid_service_response(tool_name)
    if not all(
        _is_text(value.get(key))
        for key in ("name", "purpose", "status", "created_at", "updated_at")
    ):
        _invalid_service_response(tool_name)
    if any(not value.get(key) for key in ("name", "status", "created_at", "updated_at")):
        _invalid_service_response(tool_name)
    project_id = value.get("project_id")
    if project_id is not None and not _is_integer(project_id, minimum=1):
        _invalid_service_response(tool_name)
    if not _is_nullable_text(value.get("project_name")):
        _invalid_service_response(tool_name)
    completeness = value.get("completeness")
    if (
        not isinstance(completeness, dict)
        or not isinstance(completeness.get("complete"), bool)
        or not _is_integer(completeness.get("missing_item_count"))
    ):
        _invalid_service_response(tool_name)
    if detail:
        items = value.get("items")
        if not isinstance(items, list):
            _invalid_service_response(tool_name)
        for item in items:
            _validate_item_source(item, tool_name)


def _validate_write_source(source: dict[str, Any], tool_name: str) -> None:
    result = source.get("operation_result")
    if not isinstance(result, dict):
        _invalid_service_response(tool_name)
    refs = result.get("resource_refs")
    if not isinstance(refs, list) or not refs:
        _invalid_service_response(tool_name)
    for reference in refs:
        if (
            not isinstance(reference, dict)
            or not _is_text(reference.get("type") or reference.get("resource_type"))
            or not _is_integer(reference.get("id"), minimum=1)
            or not _is_integer(reference.get("version"))
        ):
            _invalid_service_response(tool_name)
    if not isinstance(result.get("artifact_available"), bool) or not isinstance(result.get("warning_codes"), list):
        _invalid_service_response(tool_name)
    if any(not _is_text(code) for code in result["warning_codes"]):
        _invalid_service_response(tool_name)


def _validate_success_source(tool_name: str, payload: dict[str, Any], source: dict[str, Any]) -> None:
    _validate_success_metadata(payload, tool_name)
    if tool_name == "get_invoice_reference_data":
        if (
            not all(isinstance(source.get(key), list) for key in ("projects", "categories", "materials", "rules"))
            or not _is_integer(source.get("requirements_version"))
        ):
            _invalid_service_response(tool_name)
        for project in source["projects"]:
            if (
                not isinstance(project, dict)
                or not _is_integer(project.get("id"), minimum=1)
                or not _is_text(project.get("name"))
                or not _is_text(project.get("code"))
                or not (
                    isinstance(project.get("enabled"), bool)
                    or isinstance(project.get("enabled"), int)
                    and not isinstance(project.get("enabled"), bool)
                    and project.get("enabled") in {0, 1}
                )
            ):
                _invalid_service_response(tool_name)
        for collection in (source["categories"], source["materials"]):
            for entry in collection:
                if not isinstance(entry, dict) or not _is_text(entry.get("code")) or not _is_text(entry.get("label")):
                    _invalid_service_response(tool_name)
        for rule in source["rules"]:
            if (
                not isinstance(rule, dict)
                or not _is_integer(rule.get("id"), minimum=1)
                or not _is_text(rule.get("label"))
                or isinstance(rule.get("min_amount"), bool)
                or not isinstance(rule.get("min_amount"), (int, float))
                or rule.get("max_amount") is not None
                and (isinstance(rule.get("max_amount"), bool) or not isinstance(rule.get("max_amount"), (int, float)))
                or not isinstance(rule.get("required"), list)
                or any(not _is_text(code) for code in rule.get("required", []))
            ):
                _invalid_service_response(tool_name)
    elif tool_name == "get_dashboard_summary":
        counts = source.get("counts")
        keys = (
            "pending_confirmation",
            "pending_reimbursement",
            "submitted_unreimbursed",
            "missing_materials",
            "data_anomalies",
        )
        if not isinstance(counts, dict) or any(not _is_integer(counts.get(key)) for key in keys):
            _invalid_service_response(tool_name)
        recent = source.get("recent_batches")
        if not isinstance(recent, list):
            _invalid_service_response(tool_name)
        for batch in recent:
            _validate_batch_source(batch, tool_name)
    elif tool_name == "get_agent_operation":
        operation = source.get("operation")
        if (
            not isinstance(operation, dict)
            or not all(_is_text(operation.get(key)) for key in ("operation_id", "operation_name", "status", "created_at", "updated_at"))
        ):
            _invalid_service_response(tool_name)
        if operation["status"] not in {"in_progress", "succeeded", "failed"}:
            _invalid_service_response(tool_name)
        if operation["status"] == "succeeded":
            _validate_write_source({"operation_result": operation.get("operation_result")}, tool_name)
        elif operation.get("operation_result") is not None:
            _invalid_service_response(tool_name)
    elif tool_name in {"list_invoice_drafts", "list_invoice_items"}:
        if not isinstance(source.get("items"), list):
            _invalid_service_response(tool_name)
        for item in source["items"]:
            _validate_item_source(item, tool_name)
        _validate_pagination(payload, tool_name)
    elif tool_name == "get_invoice_item":
        if not isinstance(source.get("item"), dict):
            _invalid_service_response(tool_name)
        _validate_item_source(source["item"], tool_name, detail=True)
    elif tool_name == "list_reimbursement_batches":
        if not isinstance(source.get("batches"), list):
            _invalid_service_response(tool_name)
        for batch in source["batches"]:
            _validate_batch_source(batch, tool_name)
        _validate_pagination(payload, tool_name)
    elif tool_name == "get_reimbursement_batch":
        if not isinstance(source.get("batch"), dict):
            _invalid_service_response(tool_name)
        _validate_batch_source(source["batch"], tool_name, detail=True)
    elif tool_name in WRITE_TOOLS:
        _validate_write_source(source, tool_name)


def _transform(tool_name: str, result: HttpResult, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = result.payload
    source = _payload_data(payload)
    _validate_success_source(tool_name, payload, source)
    pagination = None
    if tool_name == "get_invoice_reference_data":
        data = dto.reference_data(source)
    elif tool_name == "get_dashboard_summary":
        data = dto.dashboard(source)
    elif tool_name == "get_agent_operation":
        data = dto.operation(source)
    elif tool_name in {"list_invoice_drafts", "list_invoice_items"}:
        data = {"items": [dto.item_summary(entry) for entry in source.get("items") or []]}
        pagination = _pagination(payload)
    elif tool_name == "get_invoice_item":
        data = dto.item_detail(source.get("item") if isinstance(source.get("item"), dict) else source)
    elif tool_name == "list_reimbursement_batches":
        data = {"batches": [dto.batch_summary(entry) for entry in source.get("batches") or []]}
        pagination = _pagination(payload)
    elif tool_name == "get_reimbursement_batch":
        data = dto.batch_detail(source.get("batch") if isinstance(source.get("batch"), dict) else source)
    elif tool_name in WRITE_TOOLS:
        data = dto.write_data(source)
    else:
        raise ClientError("unknown_tool", "The requested tool is not part of this MCP manifest.")
    warnings = dto.warning_list(payload.get("warnings"))
    if not warnings and tool_name in WRITE_TOOLS:
        warnings = [{"code": code, "message": code} for code in data.get("warning_codes", [])]
    return success(
        data,
        warnings=warnings,
        request_id=result.request_id,
        operation_id=arguments.get("operation_id") if tool_name in WRITE_TOOLS else None,
        replayed=result.replayed,
        pagination=pagination,
    )


def _validate_operation_id(arguments: dict[str, Any]) -> None:
    value = arguments.get("operation_id")
    if value is None:
        return
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ClientError("invalid_operation_id", "operation_id must be a UUID v4.")
    if parsed.version != 4 or str(parsed) != str(value).lower():
        raise ClientError("invalid_operation_id", "operation_id must be a canonical UUID v4.")


@dataclass(slots=True)
class Runtime:
    contracts: tuple[ToolContract, ...]
    client: LocalInvoiceClient
    allowed_roots: tuple
    _by_name: dict[str, ToolContract] = field(init=False, repr=False)
    _semaphore: anyio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {entry.name: entry for entry in self.contracts}
        self._semaphore = anyio.Semaphore(1)

    async def list_tools(self, _ctx, _params) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name=entry.name,
                description=entry.description,
                inputSchema=entry.input_schema,
                outputSchema=entry.output_schema,
                annotations=entry.annotations,
            )
            for entry in self.contracts
        ]
        return types.ListToolsResult(tools=tools)

    def _invoke_sync(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_service_status":
            return success(self.client.service_status(), request_id=str(uuid.uuid4()))
        verified: VerifiedFile | None = None
        if name in FILE_TOOLS:
            verified = read_verified_file(arguments.get("file_path"), self.allowed_roots)
        result = self.client.invoke(name, arguments, verified)
        return _transform(name, result, arguments)

    async def call_tool(self, _ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments if isinstance(params.arguments, dict) else {}
        contract = self._by_name.get(name)
        operation_id = arguments.get("operation_id") if name in WRITE_TOOLS else None
        if contract is None:
            structured = failure("unknown_tool", "The requested tool is not exposed by this MCP server.", operation_id=operation_id)
            return _call_result(structured)
        acknowledgement_required = (
            name == "import_invoice_file"
            or name == "add_invoice_attachment" and arguments.get("category") == "payment_record"
        )
        acknowledgement_supplied = (
            arguments.get("external_processing_notice_version") == EXTERNAL_NOTICE_VERSION
            and arguments.get("external_processing_ack") is True
        )
        if acknowledgement_required and not acknowledgement_supplied:
            structured = failure(
                "external_processing_ack_required",
                "The current external-processing disclosure must be acknowledged before file access.",
                operation_id=operation_id,
            )
            return _call_result(structured)
        errors = sorted(
            Draft202012Validator(contract.input_schema, format_checker=FormatChecker()).iter_errors(arguments),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            structured = failure("invalid_arguments", "Tool arguments do not match the published schema.", operation_id=operation_id)
            return _call_result(structured)
        try:
            _validate_operation_id(arguments)
            async with self._semaphore:
                with anyio.fail_after(TOOL_DEADLINE_SECONDS):
                    structured = await anyio.to_thread.run_sync(
                        self._invoke_sync,
                        name,
                        arguments,
                        abandon_on_cancel=True,
                    )
        except FileAccessError as exc:
            structured = failure(exc.code, exc.message, operation_id=operation_id)
        except ClientError as exc:
            structured = failure(
                exc.code,
                exc.message,
                http_status=exc.http_status,
                retryable=exc.retryable,
                outcome=exc.outcome,
                operation_id=operation_id,
            )
        except TimeoutError:
            structured = failure(
                "service_timeout",
                "The tool did not finish before its internal deadline.",
                retryable=True,
                outcome="unknown" if name in WRITE_TOOLS else "not_applied",
                operation_id=operation_id,
            )
        except Exception:
            logger.exception("tool failed safely name=%s", name)
            structured = failure("internal_error", "The tool failed safely before a verified result was available.", outcome="unknown" if name in WRITE_TOOLS else "not_applied", operation_id=operation_id)
        output_errors = list(Draft202012Validator(contract.output_schema).iter_errors(structured))
        if output_errors:
            logger.error("output contract violation name=%s", name)
            structured = failure("output_contract_violation", "The local response could not be represented by the safe tool contract.", outcome="unknown" if name in WRITE_TOOLS else "not_applied", operation_id=operation_id)
        return _call_result(structured)


def _call_result(structured: dict[str, Any]) -> types.CallToolResult:
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=not bool(structured.get("ok")),
    )


def create_runtime(
    *,
    allowed_roots_json: str | None = None,
    client: LocalInvoiceClient | None = None,
) -> Runtime:
    raw = allowed_roots_json if allowed_roots_json is not None else os.environ.get("INVOICE_MCP_ALLOWED_ROOTS_JSON")
    roots = parse_allowed_roots(raw)
    if raw not in (None, "[]") and not roots:
        logger.warning("allowed roots were rejected; file tools are disabled")
    return Runtime(exposed_contracts(bool(roots)), client or LocalInvoiceClient(), roots)


def create_server(runtime: Runtime | None = None) -> Server:
    active = runtime or create_runtime()
    return Server(
        "invoice_assistant",
        version="1.0.0",
        title="Invoice Assistant",
        description="Controlled local invoice workflow over the fixed loopback HTTP API.",
        on_list_tools=active.list_tools,
        on_call_tool=active.call_tool,
    )


async def _main() -> None:
    runtime = create_runtime()
    server = create_server(runtime)
    try:
        await anyio.to_thread.run_sync(runtime.client.service_status, 2)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        runtime.client.close()


def main() -> None:
    anyio.run(_main)


if __name__ == "__main__":
    main()
