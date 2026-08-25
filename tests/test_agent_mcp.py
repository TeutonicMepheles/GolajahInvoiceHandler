from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import anyio
import mcp_types as types
import pytest
import requests
from jsonschema import Draft202012Validator
from mcp import Client, ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from invoice_assistant.features.agent_mcp.client import ClientError, HttpResult, LeaseGuard, LocalInvoiceClient
from invoice_assistant.features.agent_mcp.contracts import (
    EXTERNAL_NOTICE,
    MATERIAL_SLOT_GUIDANCE,
    TOOL_MANIFEST,
    exposed_contracts,
)
from invoice_assistant.features.agent_mcp.file_access import (
    FileAccessError,
    MAX_FILE_SIZE,
    parse_allowed_roots,
    read_verified_file,
)
from invoice_assistant.features.agent_mcp.server import Runtime
from invoice_assistant.features.agent_mcp import dto


FILE_TOOL_NAMES = {"import_invoice_file", "add_invoice_attachment"}


def _walk_object_schemas(value):
    if isinstance(value, dict):
        if value.get("type") == "object":
            yield value
        for child in value.values():
            yield from _walk_object_schemas(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_object_schemas(child)


def test_manifest_is_exact_strict_and_file_tools_fail_closed():
    expected_annotations = {
        "get_service_status": (True, False, True, False),
        "get_invoice_reference_data": (True, False, True, False),
        "get_dashboard_summary": (True, False, True, False),
        "get_agent_operation": (True, False, True, False),
        "import_invoice_file": (False, False, True, True),
        "create_manual_invoice_draft": (False, False, True, False),
        "list_invoice_drafts": (True, False, True, False),
        "get_invoice_item": (True, False, True, False),
        "update_invoice_item": (False, True, True, False),
        "confirm_invoice_item": (False, True, True, False),
        "merge_invoice_draft": (False, True, True, False),
        "list_invoice_items": (True, False, True, False),
        "add_invoice_attachment": (False, True, True, True),
        "create_reimbursement_batch": (False, True, True, False),
        "list_reimbursement_batches": (True, False, True, False),
        "get_reimbursement_batch": (True, False, True, False),
        "export_reimbursement_batch": (False, True, True, False),
    }
    assert len(TOOL_MANIFEST) == 17
    assert len({entry.name for entry in TOOL_MANIFEST}) == 17
    assert {entry.name for entry in TOOL_MANIFEST} == set(expected_annotations)
    assert {entry.name for entry in exposed_contracts(False)} == {entry.name for entry in TOOL_MANIFEST} - FILE_TOOL_NAMES
    assert len(exposed_contracts(False)) == 15
    assert len(exposed_contracts(True)) == 17
    for contract in TOOL_MANIFEST:
        Draft202012Validator.check_schema(contract.input_schema)
        Draft202012Validator.check_schema(contract.output_schema)
        for schema in _walk_object_schemas(contract.input_schema):
            assert schema.get("additionalProperties") is False, contract.name
        for schema in _walk_object_schemas(contract.output_schema):
            assert schema.get("additionalProperties") is False, contract.name
        assert (
            contract.annotations.read_only_hint,
            contract.annotations.destructive_hint,
            contract.annotations.idempotent_hint,
            contract.annotations.open_world_hint,
        ) == expected_annotations[contract.name]

    import_contract = next(entry for entry in TOOL_MANIFEST if entry.name == "import_invoice_file")
    attachment_contract = next(entry for entry in TOOL_MANIFEST if entry.name == "add_invoice_attachment")
    assert EXTERNAL_NOTICE in import_contract.description
    assert EXTERNAL_NOTICE in attachment_contract.description
    assert import_contract.annotations.open_world_hint is True
    assert attachment_contract.annotations.open_world_hint is True
    assert import_contract.annotations.read_only_hint is False

    forbidden_fragments = {
        "delete",
        "reopen",
        "reimbursed",
        "setting",
        "open_directory",
        "quotation",
        "resource",
        "prompt",
    }
    assert not any(fragment in entry.name for entry in TOOL_MANIFEST for fragment in forbidden_fragments)


def test_required_material_slot_workflow_is_published_in_mcp_discovery():
    contracts = {entry.name: entry for entry in TOOL_MANIFEST}
    assert "material.missing" in contracts["list_invoice_drafts"].description
    assert "required-material status" in contracts["get_invoice_item"].description

    attachment_description = contracts["add_invoice_attachment"].description
    assert MATERIAL_SLOT_GUIDANCE in attachment_description
    assert "foreign_payment_rmb or payment_record to category payment_record" in attachment_description
    assert "purchase_list to category purchase_list" in attachment_description
    assert "call get_invoice_item again after every attachment write" in attachment_description


def test_external_processing_ack_is_schema_enforced_before_file_use():
    import_contract = next(entry for entry in TOOL_MANIFEST if entry.name == "import_invoice_file")
    attachment_contract = next(entry for entry in TOOL_MANIFEST if entry.name == "add_invoice_attachment")
    operation_id = str(uuid.uuid4())
    missing_ack = {"operation_id": operation_id, "file_path": r"C:\allowed\invoice.pdf"}
    assert list(Draft202012Validator(import_contract.input_schema).iter_errors(missing_ack))

    ordinary_attachment = {
        "operation_id": operation_id,
        "item_id": 1,
        "expected_version": 0,
        "file_path": r"C:\allowed\invoice.pdf",
        "category": "invoice",
    }
    assert not list(Draft202012Validator(attachment_contract.input_schema).iter_errors(ordinary_attachment))
    payment_attachment = {**ordinary_attachment, "category": "payment_record"}
    assert list(Draft202012Validator(attachment_contract.input_schema).iter_errors(payment_attachment))
    payment_attachment.update(
        {"external_processing_notice_version": "deepseek-v1", "external_processing_ack": True}
    )
    assert not list(Draft202012Validator(attachment_contract.input_schema).iter_errors(payment_attachment))


def _write_png(path: Path, size: int = 32) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8))


def test_allowed_roots_and_same_handle_bounded_read(tmp_path):
    root = tmp_path / "授权 目录"
    root.mkdir()
    path = root / "票据.png"
    _write_png(path)
    roots = parse_allowed_roots(json.dumps([str(root)], ensure_ascii=False))
    assert len(roots) == 1
    verified = read_verified_file(str(path), roots)
    assert verified.content.startswith(b"\x89PNG")
    assert verified.size_bytes == 32
    assert verified.display_basename == "票据.png"
    assert len(verified.sha256) == 64

    outside = tmp_path / "outside.png"
    _write_png(outside)
    with pytest.raises(FileAccessError, match="outside") as exc:
        read_verified_file(str(outside), roots)
    assert exc.value.code == "file_outside_allowed_roots"


def test_file_limits_magic_ads_and_hardlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    roots = parse_allowed_roots(json.dumps([str(root)]))

    exact = root / "exact.png"
    _write_png(exact, MAX_FILE_SIZE)
    assert read_verified_file(str(exact), roots).size_bytes == MAX_FILE_SIZE

    oversized = root / "oversized.png"
    _write_png(oversized, MAX_FILE_SIZE + 1)
    with pytest.raises(FileAccessError) as exc:
        read_verified_file(str(oversized), roots)
    assert exc.value.code == "file_too_large"

    mismatch = root / "mismatch.pdf"
    mismatch.write_bytes(b"not a pdf")
    with pytest.raises(FileAccessError) as exc:
        read_verified_file(str(mismatch), roots)
    assert exc.value.code == "file_type_mismatch"

    with pytest.raises(FileAccessError) as exc:
        read_verified_file(str(exact) + ":stream", roots)
    assert exc.value.code == "invalid_file_path"

    link_source = root / "linked.png"
    _write_png(link_source)
    link_target = root / "second.png"
    os.link(link_source, link_target)
    with pytest.raises(FileAccessError) as exc:
        read_verified_file(str(link_source), roots)
    assert exc.value.code == "non_regular_file"


def test_same_size_in_place_change_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "changing.png"
    _write_png(path, 2 * 1024 * 1024)
    roots = parse_allowed_roots(json.dumps([str(root)]))
    real_read = os.read
    mutated = False

    def change_after_first_chunk(fd, count):
        nonlocal mutated
        chunk = real_read(fd, count)
        if chunk and not mutated:
            mutated = True
            with path.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                stream.write(b"y")
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr(os, "read", change_after_first_chunk)
    with pytest.raises(FileAccessError) as changed:
        read_verified_file(str(path), roots)
    assert changed.value.code == "file_changed"


def test_malformed_or_missing_roots_disable_file_tools(tmp_path):
    assert parse_allowed_roots(None) == ()
    assert parse_allowed_roots("not-json") == ()
    assert parse_allowed_roots("{}") == ()
    assert parse_allowed_roots(json.dumps([123])) == ()
    assert parse_allowed_roots(json.dumps([str(tmp_path / "missing")])) == ()
    with pytest.raises(FileAccessError) as exc:
        read_verified_file(str(tmp_path / "anything.pdf"), ())
    assert exc.value.code == "file_tools_disabled"


def test_registration_lease_staleness(tmp_path):
    generation = str(uuid.uuid4())
    lease = tmp_path / "lease.json"
    lease.write_text(json.dumps({"generation": generation, "active": True}), encoding="utf-8")
    assert LeaseGuard(str(lease), generation).stale() is False
    assert LeaseGuard(str(lease), str(uuid.uuid4())).stale() is True
    lease.unlink()
    assert LeaseGuard(str(lease), generation).stale() is True
    assert LeaseGuard(None, None).stale() is False


def _response(payload, status=200, content_type="application/json"):
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    return response


def test_http_client_ignores_proxies_rejects_redirects_and_caps_response(monkeypatch):
    client = LocalInvoiceClient(lease_guard=LeaseGuard(None, None))
    assert client.session.trust_env is False
    with pytest.raises(Exception) as redirect:
        client._read_json_response(_response({}, 302))
    assert getattr(redirect.value, "code", None) == "redirect_rejected"
    with pytest.raises(Exception) as non_json:
        client._read_json_response(_response({}, 200, "text/html"))
    assert getattr(non_json.value, "code", None) == "invalid_service_response"
    huge = _response({})
    huge._content = b"{" + b" " * (4 * 1024 * 1024) + b"}"
    with pytest.raises(Exception) as too_large:
        client._read_json_response(huge)
    assert getattr(too_large.value, "code", None) == "response_too_large"


def test_http_client_write_connection_loss_is_unknown_and_lists_force_keyset(monkeypatch):
    client = LocalInvoiceClient(lease_guard=LeaseGuard(None, None))
    monkeypatch.setattr(client, "_require_running", lambda **_kwargs: None)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("lost")),
    )
    with pytest.raises(ClientError) as lost:
        client._business(
            "POST",
            "/api/items/manual",
            tool_name="create_manual_invoice_draft",
            operation_id=str(uuid.uuid4()),
            json_body={},
        )
    assert lost.value.code == "service_connection_lost"
    assert lost.value.outcome == "unknown"

    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: _response({}, 200, "text/html"),
    )
    with pytest.raises(ClientError) as unverifiable:
        client._business(
            "POST",
            "/api/items/manual",
            tool_name="create_manual_invoice_draft",
            operation_id=str(uuid.uuid4()),
            json_body={},
        )
    assert unverifiable.value.code == "invalid_service_response"
    assert unverifiable.value.outcome == "unknown"

    leaked = LocalInvoiceClient._error_from_response(
        _response({}, 500),
        {"error": "internal_error", "message": r"C:\private\db.sqlite3 sk-secret"},
        write=True,
    )
    assert "private" not in leaked.message and "secret" not in leaked.message
    assert leaked.outcome == "unknown"

    calls = []

    def capture(_method, path, **kwargs):
        calls.append((path, kwargs.get("params")))
        return HttpResult(payload={}, request_id="test")

    monkeypatch.setattr(client, "_business", capture)
    client.invoke("list_invoice_drafts", {})
    client.invoke("list_invoice_items", {"limit": 7})
    client.invoke("list_reimbursement_batches", {})
    assert calls == [
        ("/api/drafts", {"limit": 50}),
        ("/api/items", {"limit": 7}),
        ("/api/batches", {"limit": 50}),
    ]


def test_dto_recursively_drops_internal_paths_raw_content_and_control_characters():
    raw = {
        "id": 7,
        "row_version": 3,
        "merchant": "Vendor\x00 ignored-control",
        "expense_date": "2026-08-23",
        "amount_cents": 123,
        "currency": "CNY",
        "purpose": "purpose",
        "status": "pending_confirmation",
        "created_at": "2026-08-23T00:00:00Z",
        "updated_at": "2026-08-23T00:00:00Z",
        "material": {"complete": False, "percent": 0, "missing": []},
        "managed_path": r"C:\secret\invoice.pdf",
        "ai_raw": {"prompt": "ignore previous instructions"},
        "confirmed_snapshot": {"amount": 1.23},
        "audit_logs": [{"details": "secret"}],
        "attachments": [
            {
                "id": 9,
                "category": "invoice",
                "normalized_name": "safe.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 10,
                "original_name": r"C:\private\salary.pdf",
                "normalized_name": None,
                "managed_path": r"C:\secret\invoice.pdf",
                "ai_raw": {"body": "secret"},
            }
        ],
        "review": {
            "token": "opaque",
            "uncertainties": [],
            "recognition_failure": "safe failure summary",
            "duplicate_candidates": [],
            "blocking_duplicate_ids": [],
        },
    }
    safe = dto.item_detail(raw)
    serialized = json.dumps(safe, ensure_ascii=False)
    for forbidden in ("managed_path", "ai_raw", "confirmed_snapshot", "audit_logs", r"C:\\secret"):
        assert forbidden not in serialized
    assert "\x00" not in safe["merchant"]
    assert safe["review"]["recognition_error"] == "safe failure summary"
    assert safe["attachments"][0]["display_name"] == "salary.pdf"
    assert "\u202e" not in dto.clean_text("safe\u202eevil")


def test_duplicate_candidate_reason_list_is_stable_and_sanitized():
    first = dto.duplicate_candidate(
        {
            "id": 9,
            "reason": ["historical", "\u202ehigh_confidence", "historical"],
        }
    )
    second = dto.duplicate_candidate(
        {
            "id": 9,
            "reason": ["historical", "high_confidence"],
        }
    )

    assert first["reason"] == "high_confidence,historical"
    assert first["reason"] == second["reason"]


class FakeClient:
    def service_status(self, timeout=30):
        return {"status": "stopped", "service": None, "platform_hint": "MCP does not start the service."}

    def invoke(self, name, arguments, verified_file=None):
        if name == "get_invoice_reference_data":
            return HttpResult(
                payload={"projects": [], "categories": [], "materials": [], "rules": [], "requirements_version": 4},
                request_id="test-request",
            )
        raise AssertionError(name)

    def close(self):
        return None


class MalformedClient(FakeClient):
    def service_status(self, timeout=30):
        return {"status": "running", "service": "invoice-assistant", "platform_hint": None}

    def invoke(self, name, arguments, verified_file=None):
        return HttpResult(payload={}, request_id="malformed")


@pytest.mark.anyio
async def test_in_memory_discovery_and_structured_output():
    runtime = Runtime(exposed_contracts(False), FakeClient(), ())
    listed = await runtime.list_tools(None, None)
    assert len(listed.tools) == 15
    assert FILE_TOOL_NAMES.isdisjoint({tool.name for tool in listed.tools})

    status = await runtime.call_tool(None, types.CallToolRequestParams(name="get_service_status", arguments={}))
    assert status.is_error is False
    assert status.structured_content["data"]["status"] == "stopped"

    reference = await runtime.call_tool(None, types.CallToolRequestParams(name="get_invoice_reference_data", arguments={}))
    assert reference.is_error is False
    assert reference.structured_content["data"]["requirements_version"] == 4

    invalid = await runtime.call_tool(
        None,
        types.CallToolRequestParams(name="create_manual_invoice_draft", arguments={"operation_id": "not-a-uuid"}),
    )
    assert invalid.is_error is True
    assert invalid.structured_content["error"]["code"] == "invalid_arguments"

    malformed = Runtime(exposed_contracts(False), MalformedClient(), ())
    malformed_read = await malformed.call_tool(
        None,
        types.CallToolRequestParams(name="get_invoice_item", arguments={"item_id": 1}),
    )
    assert malformed_read.is_error is True
    assert malformed_read.structured_content["error"]["code"] == "invalid_service_response"
    malformed_write = await malformed.call_tool(
        None,
        types.CallToolRequestParams(
            name="create_manual_invoice_draft",
            arguments={
                "operation_id": str(uuid.uuid4()),
                "merchant": "test",
                "expense_date": "2026-08-23",
                "amount_cents": 100,
                "currency": "CNY",
                "purpose": "test",
                "project_id": 1,
            },
        ),
    )
    assert malformed_write.is_error is True
    assert malformed_write.structured_content["error"]["outcome"] == "unknown"


@pytest.mark.anyio
async def test_nested_malformed_success_cannot_be_normalized_into_valid_data():
    class NestedMalformedClient(FakeClient):
        def service_status(self, timeout=30):
            return {"status": "running", "service": "invoice-assistant", "platform_hint": None}

        def invoke(self, name, arguments, verified_file=None):
            assert name == "get_invoice_item"
            return HttpResult(
                payload={
                    "item": {
                        "id": 1,
                        "version": 0,
                        "requirements_version": 0,
                        "merchant": "merchant",
                        "expense_date": "2026-08-23",
                        "amount_cents": 100,
                        "currency": "CNY",
                        "converted_amount_cents": None,
                        "purpose": "purpose",
                        "project_id": 1,
                        "project_name": "project",
                        "status": "pending_confirmation",
                        "material": {"complete": True, "percent": 100, "missing": []},
                        "batch_ref": None,
                        "created_at": "2026-08-23T00:00:00Z",
                        "updated_at": "2026-08-23T00:00:00Z",
                        "attachments": [],
                        "review": {
                            "token": "review-v1.token",
                            "uncertainties": "not-an-array",
                            "recognition_failure": None,
                            "duplicate_candidates": [],
                            "blocking_duplicate_ids": [],
                            "blocking_total": 0,
                            "duplicate_review_overflow": False,
                        },
                    },
                    "warnings": {"code": "not-an-array"},
                },
                request_id="malformed-nested",
            )

    runtime = Runtime(exposed_contracts(False), NestedMalformedClient(), ())
    result = await runtime.call_tool(
        None,
        types.CallToolRequestParams(name="get_invoice_item", arguments={"item_id": 1}),
    )
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_service_response"


@pytest.mark.anyio
async def test_external_ack_error_precedes_schema_and_file_read(tmp_path):
    runtime = Runtime(exposed_contracts(True), MalformedClient(), (tmp_path,))
    denied = await runtime.call_tool(
        None,
        types.CallToolRequestParams(
            name="import_invoice_file",
            arguments={"operation_id": str(uuid.uuid4()), "file_path": str(tmp_path / "missing.pdf")},
        ),
    )
    assert denied.is_error is True
    assert denied.structured_content["error"]["code"] == "external_processing_ack_required"


@pytest.mark.anyio
async def test_real_stdio_subprocess_discovers_exact_safe_subset(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["INVOICE_MCP_ALLOWED_ROOTS_JSON"] = "[]"
    environment.pop("INVOICE_MCP_REGISTRATION_LEASE_PATH", None)
    environment.pop("INVOICE_MCP_REGISTRATION_GENERATION", None)
    parameters = StdioServerParameters(
        command=str(project_root / ".venv" / "Scripts" / "python.exe"),
        args=["-m", "invoice_assistant.features.agent_mcp.server"],
        env=environment,
        cwd=project_root,
    )
    with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
        with anyio.fail_after(15):
            async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream, read_timeout_seconds=10) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    assert len(result.tools) == 15
                    assert FILE_TOOL_NAMES.isdisjoint({tool.name for tool in result.tools})
                    status = await session.call_tool("get_service_status", {})
                    assert status.is_error is False
                    assert status.structured_content["data"]["status"] in {
                        "stopped",
                        "timeout",
                        "wrong_service",
                        "degraded",
                        "running",
                    }


@pytest.mark.anyio
async def test_modern_stdio_negotiation_discovers_exact_file_enabled_manifest(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    allowed = tmp_path / "授权 目录"
    allowed.mkdir()
    environment = os.environ.copy()
    environment["INVOICE_MCP_ALLOWED_ROOTS_JSON"] = json.dumps([str(allowed)], ensure_ascii=False)
    environment.pop("INVOICE_MCP_REGISTRATION_LEASE_PATH", None)
    environment.pop("INVOICE_MCP_REGISTRATION_GENERATION", None)
    parameters = StdioServerParameters(
        command=str(project_root / ".venv" / "Scripts" / "python.exe"),
        args=["-m", "invoice_assistant.features.agent_mcp.server"],
        env=environment,
        cwd=project_root,
    )
    with anyio.fail_after(15):
        async with Client(stdio_client(parameters), mode="auto", read_timeout_seconds=10) as modern:
            result = await modern.list_tools()
            assert modern.protocol_version == "2026-07-28"
            assert {tool.name for tool in result.tools} == {entry.name for entry in TOOL_MANIFEST}
            assert modern.server_capabilities.resources is None
            assert modern.server_capabilities.prompts is None
