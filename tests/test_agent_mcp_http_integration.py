from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from PIL import Image, ImageDraw
from flask import request
from werkzeug.serving import make_server

from invoice_assistant import create_app
from invoice_assistant.features.agent_mcp import client as client_module
from invoice_assistant.features.agent_mcp.client import LeaseGuard, LocalInvoiceClient
from invoice_assistant.features.agent_mcp.contracts import TOOL_MANIFEST
from invoice_assistant.features.agent_mcp.server import create_runtime, create_server
from invoice_assistant.migrate import migrate_database


CONTRACTS = {entry.name: entry for entry in TOOL_MANIFEST}


@dataclass(slots=True)
class LoopbackHarness:
    mcp_server: Any
    http_client: LocalInvoiceClient
    allowed_root: Path
    outside_root: Path
    requests: list[dict[str, Any]]
    recognition_calls: list[dict[str, Any]]
    data_dir: Path


def _operation_id() -> str:
    return str(uuid.uuid4())


def _write_png(path: Path, label: str) -> None:
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 40), label, fill="black")
    draw.text((40, 100), "Amount: 234.56 CNY", fill="black")
    image.save(path, format="PNG")
    image.close()


@pytest.fixture()
def loopback_harness(tmp_path, monkeypatch):
    # Keep the fixture short enough to exercise the application rather than the
    # legacy Win32 MAX_PATH boundary in deeply nested per-test temp directories.
    # The unique directory remains under pytest's session-owned temporary root.
    data_dir = tmp_path.parent / f"m-{uuid.uuid4().hex[:8]}"
    database = data_dir / "invoice-assistant.sqlite3"
    migrate_database(
        data_dir,
        database_path=database,
        default_archive_dir=data_dir / "archives",
        expect_no_database=True,
    )

    recognition_calls: list[dict[str, Any]] = []

    def recognition_stub(path_value: str, mime_type: str, display_name: str) -> dict[str, Any]:
        managed_path = Path(path_value)
        recognition_calls.append(
            {
                "mime_type": mime_type,
                "display_name": display_name,
                "sha256": hashlib.sha256(managed_path.read_bytes()).hexdigest(),
            }
        )
        return {
            "merchant": "MCP Loopback Import Merchant",
            "expense_date": "2026-08-19",
            "amount": 234.56,
            "currency": "CNY",
            "converted_amount": None,
            "purpose": "Loopback import fixture",
            "document_type": "invoice",
            "uncertainties": [],
            "confidence": 0.99,
        }

    app = create_app(
        {
            "TESTING": True,
            "AUTO_BACKUP": False,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(database),
            "IMPORT_DIR": str(data_dir / "imports"),
            "DEFAULT_ARCHIVE_DIR": str(data_dir / "archives"),
            "TEMP_DIR": str(data_dir / "tmp"),
            "RECOGNIZER": recognition_stub,
        }
    )

    captured_requests: list[dict[str, Any]] = []

    @app.before_request
    def capture_mcp_http_request():
        entry: dict[str, Any] = {
            "method": request.method,
            "path": request.path,
            "content_type": request.content_type,
            "operation_id": request.headers.get("Idempotency-Key"),
            "agent_tool": request.headers.get("X-Invoice-Agent-Tool"),
        }
        if request.is_json:
            entry["json"] = request.get_json(silent=True)
        if request.mimetype == "multipart/form-data":
            entry["form"] = {key: request.form.getlist(key) for key in request.form}
            files = []
            for key in request.files:
                for upload in request.files.getlist(key):
                    position = upload.stream.tell()
                    content = upload.stream.read()
                    upload.stream.seek(position)
                    files.append(
                        {
                            "field": key,
                            "filename": upload.filename,
                            "mime_type": upload.mimetype,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
            entry["files"] = files
        captured_requests.append(entry)

    http_server = make_server("127.0.0.1", 0, app, threaded=True)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()

    # Production remains frozen to 127.0.0.1:8765.  Only this in-process test
    # replaces the module constant with the random loopback listener.
    monkeypatch.setattr(client_module, "BASE_URL", f"http://127.0.0.1:{http_server.server_port}")

    allowed_root = tmp_path / "允许文件"
    allowed_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    http_client = LocalInvoiceClient(lease_guard=LeaseGuard(None, None))
    runtime = create_runtime(
        allowed_roots_json=json.dumps([str(allowed_root)], ensure_ascii=False),
        client=http_client,
    )
    harness = LoopbackHarness(
        mcp_server=create_server(runtime),
        http_client=http_client,
        allowed_root=allowed_root,
        outside_root=outside_root,
        requests=captured_requests,
        recognition_calls=recognition_calls,
        data_dir=data_dir,
    )
    try:
        yield harness
    finally:
        http_client.close()
        http_server.shutdown()
        http_thread.join(timeout=5)
        http_server.server_close()


def _structured(result) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    assert isinstance(value, dict)
    return value


async def _call_tool(
    client: Client,
    harness: LoopbackHarness,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    expected_error: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contract = CONTRACTS[tool_name]
    Draft202012Validator(contract.input_schema).validate(arguments)
    before = len(harness.requests)
    result = await client.call_tool(tool_name, arguments)
    structured = _structured(result)
    Draft202012Validator(contract.output_schema).validate(structured)
    if expected_error is None:
        assert result.is_error is False, structured
        assert structured["ok"] is True
    else:
        assert result.is_error is True, structured
        assert structured["ok"] is False
        assert structured["error"]["code"] == expected_error
    return structured, harness.requests[before:]


def _business_request(records: list[dict[str, Any]], path: str) -> dict[str, Any]:
    matching = [entry for entry in records if entry["path"] == path]
    assert len(matching) == 1, records
    return matching[0]


def _resource_id(envelope: dict[str, Any], resource_type: str) -> int:
    references = envelope["data"]["resource_refs"]
    matches = [entry for entry in references if entry["resource_type"] == resource_type]
    assert matches, envelope
    return int(matches[0]["id"])


def _confirmation_arguments(operation_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    review = detail["data"]["review"]
    blocking = review["blocking_duplicate_ids"]
    return {
        "operation_id": operation_id,
        "item_id": detail["data"]["id"],
        "expected_version": detail["data"]["version"],
        "review_token": review["token"],
        "duplicate_resolution": "keep_separate" if blocking else "none",
        "acknowledged_uncertainty_ids": [
            entry["uncertainty_id"] for entry in review["uncertainties"]
        ],
        "acknowledged_duplicate_ids": blocking,
    }


@pytest.mark.anyio
async def test_mcp_http_all_tools(loopback_harness):
    harness = loopback_harness
    import_path = harness.allowed_root / "loopback import.png"
    ordinary_path = harness.allowed_root / "ordinary attachment.png"
    payment_path = harness.allowed_root / "payment record.png"
    _write_png(import_path, "import")
    _write_png(ordinary_path, "ordinary")
    _write_png(payment_path, "payment")

    invoked: set[str] = set()
    observed_http: dict[str, tuple[str, str]] = {}

    async def call(name: str, arguments: dict[str, Any]):
        envelope, records = await _call_tool(agent, harness, name, arguments)
        invoked.add(name)
        non_health = [entry for entry in records if entry["path"] != "/health"]
        if name == "get_service_status":
            health = _business_request(records, "/health")
            observed_http[name] = (health["method"], health["path"])
        else:
            assert len(non_health) == 1, records
            observed_http[name] = (non_health[0]["method"], non_health[0]["path"])
        return envelope, records

    async with Client(harness.mcp_server, mode="auto", read_timeout_seconds=30) as agent:
        assert agent.server_capabilities.resources is None
        assert agent.server_capabilities.prompts is None
        listed = await agent.list_tools()
        assert {tool.name for tool in listed.tools} == set(CONTRACTS)
        assert len(listed.tools) == 17

        # Every published tool must return its own strict failure envelope and
        # must reject schema-invalid input before any loopback HTTP request.
        before_invalid = len(harness.requests)
        for tool_name, contract in CONTRACTS.items():
            invalid_arguments: dict[str, Any] = {"unexpected": True}
            if tool_name == "import_invoice_file":
                invalid_arguments.update(
                    {
                        "external_processing_notice_version": "deepseek-v1",
                        "external_processing_ack": True,
                    }
                )
            invalid_result = await agent.call_tool(tool_name, invalid_arguments)
            invalid = _structured(invalid_result)
            Draft202012Validator(contract.output_schema).validate(invalid)
            assert invalid_result.is_error is True
            assert invalid["error"]["code"] == "invalid_arguments"
            assert invalid["error"]["outcome"] == "not_applied"
        assert len(harness.requests) == before_invalid

        status, _ = await call("get_service_status", {})
        assert status["data"]["status"] == "running"

        reference, _ = await call("get_invoice_reference_data", {})
        project_id = reference["data"]["projects"][0]["id"]
        assert reference["data"]["requirements_version"] >= 0

        dashboard, _ = await call("get_dashboard_summary", {})
        assert dashboard["data"]["counts"]["pending_confirmation"] == 0

        import_operation = _operation_id()
        imported, import_records = await call(
            "import_invoice_file",
            {
                "operation_id": import_operation,
                "file_path": str(import_path),
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": True,
            },
        )
        imported_id = _resource_id(imported, "invoice_item")
        import_http = _business_request(import_records, "/api/imports")
        assert import_http["method"] == "POST"
        assert import_http["operation_id"] == import_operation
        assert import_http["agent_tool"] == "import_invoice_file"
        assert import_http["form"] == {
            "external_processing_notice_version": ["deepseek-v1"],
            "external_processing_ack": ["true"],
        }
        assert import_http["files"] == [
            {
                "field": "file",
                "filename": import_path.name,
                "mime_type": "image/png",
                "size": import_path.stat().st_size,
                "sha256": hashlib.sha256(import_path.read_bytes()).hexdigest(),
            }
        ]
        assert "file_path" not in import_http["form"]

        replayed_import, replay_records = await _call_tool(
            agent,
            harness,
            "import_invoice_file",
            {
                "operation_id": import_operation,
                "file_path": str(import_path),
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": True,
            },
        )
        assert replayed_import["data"] == imported["data"]
        assert replayed_import["meta"]["replayed"] is True
        assert _business_request(replay_records, "/api/imports")["operation_id"] == import_operation
        assert len(harness.recognition_calls) == 1

        operation, _ = await call("get_agent_operation", {"operation_id": import_operation})
        assert operation["data"]["status"] == "succeeded"
        assert operation["data"]["operation_id"] == import_operation

        drafts, _ = await call("list_invoice_drafts", {"limit": 10})
        assert imported_id in {entry["id"] for entry in drafts["data"]["items"]}

        imported_detail, _ = await call("get_invoice_item", {"item_id": imported_id})
        confirm_operation = _operation_id()
        confirmed, confirm_records = await call(
            "confirm_invoice_item",
            _confirmation_arguments(confirm_operation, imported_detail),
        )
        confirmed_version = next(
            entry["version"]
            for entry in confirmed["data"]["resource_refs"]
            if entry["resource_type"] == "invoice_item"
        )
        confirm_http = _business_request(confirm_records, f"/api/items/{imported_id}/confirm")
        assert confirm_http["operation_id"] == confirm_operation
        assert "operation_id" not in confirm_http["json"]
        assert confirm_http["json"]["expected_version"] == imported_detail["data"]["version"]

        ordinary_operation = _operation_id()
        ordinary, ordinary_records = await call(
            "add_invoice_attachment",
            {
                "operation_id": ordinary_operation,
                "item_id": imported_id,
                "expected_version": confirmed_version,
                "file_path": str(ordinary_path),
                "category": "invoice",
            },
        )
        ordinary_version = next(
            entry["version"]
            for entry in ordinary["data"]["resource_refs"]
            if entry["resource_type"] == "invoice_item"
        )
        ordinary_http = _business_request(
            ordinary_records, f"/api/items/{imported_id}/attachments"
        )
        assert ordinary_http["form"] == {
            "expected_version": [str(confirmed_version)],
            "category": ["invoice"],
        }
        assert ordinary_http["files"][0]["sha256"] == hashlib.sha256(
            ordinary_path.read_bytes()
        ).hexdigest()
        assert "file_path" not in ordinary_http["form"]

        payment_operation = _operation_id()
        payment, payment_records = await call(
            "add_invoice_attachment",
            {
                "operation_id": payment_operation,
                "item_id": imported_id,
                "expected_version": ordinary_version,
                "file_path": str(payment_path),
                "category": "payment_record",
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": True,
            },
        )
        payment_version = next(
            entry["version"]
            for entry in payment["data"]["resource_refs"]
            if entry["resource_type"] == "invoice_item"
        )
        payment_http = _business_request(
            payment_records, f"/api/items/{imported_id}/attachments"
        )
        assert payment_http["form"]["external_processing_notice_version"] == ["deepseek-v1"]
        assert payment_http["form"]["external_processing_ack"] == ["true"]
        assert len(harness.recognition_calls) == 2

        manual_operation = _operation_id()
        manual_arguments = {
            "operation_id": manual_operation,
            "merchant": "MCP Merge Merchant",
            "expense_date": "2026-08-20",
            "amount_cents": 34567,
            "currency": "CNY",
            "purpose": "Merge fixture",
            "project_id": project_id,
        }
        manual, manual_records = await call("create_manual_invoice_draft", manual_arguments)
        target_id = _resource_id(manual, "invoice_item")
        manual_http = _business_request(manual_records, "/api/items/manual")
        assert manual_http["operation_id"] == manual_operation
        assert manual_http["json"]["amount_cents"] == 34567
        assert "operation_id" not in manual_http["json"]

        replayed_manual, _ = await _call_tool(
            agent, harness, "create_manual_invoice_draft", manual_arguments
        )
        assert replayed_manual["data"] == manual["data"]
        assert replayed_manual["meta"]["replayed"] is True

        update_operation = _operation_id()
        updated, _ = await call(
            "update_invoice_item",
            {
                "operation_id": update_operation,
                "item_id": target_id,
                "expected_version": 0,
                "purpose": "Merge fixture updated",
            },
        )
        target_version = next(
            entry["version"]
            for entry in updated["data"]["resource_refs"]
            if entry["resource_type"] == "invoice_item"
        )

        source, _ = await _call_tool(
            agent,
            harness,
            "create_manual_invoice_draft",
            {
                **manual_arguments,
                "operation_id": _operation_id(),
                "purpose": "Merge fixture updated",
            },
        )
        source_id = _resource_id(source, "invoice_item")
        source_detail, _ = await _call_tool(
            agent, harness, "get_invoice_item", {"item_id": source_id}
        )
        candidates = source_detail["data"]["review"]["duplicate_candidates"]
        assert target_id in {entry["id"] for entry in candidates}
        merge_operation = _operation_id()
        merged, _ = await call(
            "merge_invoice_draft",
            {
                "operation_id": merge_operation,
                "source_id": source_id,
                "target_id": target_id,
                "source_version": source_detail["data"]["version"],
                "target_version": target_version,
                "source_review_token": source_detail["data"]["review"]["token"],
            },
        )
        assert {entry["id"] for entry in merged["data"]["resource_refs"]} >= {
            source_id,
            target_id,
        }

        items, _ = await call(
            "list_invoice_items", {"status": "pending_reimbursement", "limit": 10}
        )
        assert imported_id in {entry["id"] for entry in items["data"]["items"]}

        batch_operation = _operation_id()
        created_batch, _ = await call(
            "create_reimbursement_batch",
            {
                "operation_id": batch_operation,
                "name": "MCP Loopback Batch",
                "project_id": project_id,
                "purpose": "HTTP integration",
                "items": [{"item_id": imported_id, "expected_version": payment_version}],
            },
        )
        batch_id = _resource_id(created_batch, "reimbursement_batch")

        batches, _ = await call("list_reimbursement_batches", {"limit": 10})
        assert batch_id in {entry["id"] for entry in batches["data"]["batches"]}

        batch, _ = await call("get_reimbursement_batch", {"batch_id": batch_id})
        assert batch["data"]["complete"] is True
        assert batch["data"]["name"] == "MCP Loopback Batch"

        export_operation = _operation_id()
        exported, export_records = await call(
            "export_reimbursement_batch",
            {
                "operation_id": export_operation,
                "batch_id": batch_id,
                "expected_version": batch["data"]["version"],
                "expected_requirements_version": batch["data"]["requirements_version"],
                "confirmation_name": batch["data"]["name"],
            },
        )
        assert exported["data"]["artifact_available"] is True
        export_http = _business_request(export_records, f"/api/batches/{batch_id}/export")
        assert export_http["method"] == "POST"
        assert export_http["operation_id"] == export_operation
        assert "operation_id" not in export_http["json"]

        final_dashboard, _ = await call("get_dashboard_summary", {})
        assert final_dashboard["data"]["counts"]["submitted_unreimbursed"] == 1

    assert invoked == set(CONTRACTS)
    expected_http = {
        "get_service_status": ("GET", "/health"),
        "get_invoice_reference_data": ("GET", "/api/bootstrap"),
        "get_dashboard_summary": ("GET", "/api/dashboard"),
        "get_agent_operation": ("GET", f"/api/agent-operations/{import_operation}"),
        "import_invoice_file": ("POST", "/api/imports"),
        "create_manual_invoice_draft": ("POST", "/api/items/manual"),
        "list_invoice_drafts": ("GET", "/api/drafts"),
        "get_invoice_item": ("GET", f"/api/items/{imported_id}"),
        "update_invoice_item": ("PATCH", f"/api/items/{target_id}"),
        "confirm_invoice_item": ("POST", f"/api/items/{imported_id}/confirm"),
        "merge_invoice_draft": ("POST", f"/api/items/{source_id}/merge/{target_id}"),
        "list_invoice_items": ("GET", "/api/items"),
        "add_invoice_attachment": ("POST", f"/api/items/{imported_id}/attachments"),
        "create_reimbursement_batch": ("POST", "/api/batches"),
        "list_reimbursement_batches": ("GET", "/api/batches"),
        "get_reimbursement_batch": ("GET", f"/api/batches/{batch_id}"),
        "export_reimbursement_batch": ("POST", f"/api/batches/{batch_id}/export"),
    }
    assert observed_http == expected_http
    assert list(harness.data_dir.glob("archives/**/*")), "export must publish only inside the isolated data root"


@pytest.mark.anyio
async def test_mcp_http_errors(loopback_harness):
    harness = loopback_harness
    allowed_file = harness.allowed_root / "allowed.png"
    outside_file = harness.outside_root / "outside.png"
    _write_png(allowed_file, "allowed")
    _write_png(outside_file, "outside")

    async with Client(harness.mcp_server, mode="auto", read_timeout_seconds=30) as agent:
        reference, _ = await _call_tool(agent, harness, "get_invoice_reference_data", {})
        project_id = reference["data"]["projects"][0]["id"]

        operation_id = _operation_id()
        base = {
            "operation_id": operation_id,
            "merchant": "Idempotency Merchant",
            "expense_date": "2026-08-21",
            "amount_cents": 12345,
            "currency": "CNY",
            "purpose": "Original",
            "project_id": project_id,
        }
        created, _ = await _call_tool(agent, harness, "create_manual_invoice_draft", base)
        item_id = _resource_id(created, "invoice_item")
        replayed, _ = await _call_tool(agent, harness, "create_manual_invoice_draft", base)
        assert replayed["meta"]["replayed"] is True
        mismatch, _ = await _call_tool(
            agent,
            harness,
            "create_manual_invoice_draft",
            {**base, "purpose": "Changed under same operation ID"},
            expected_error="idempotency_mismatch",
        )
        assert mismatch["error"]["http_status"] == 409
        assert mismatch["error"]["outcome"] == "not_applied"

        updated, _ = await _call_tool(
            agent,
            harness,
            "update_invoice_item",
            {
                "operation_id": _operation_id(),
                "item_id": item_id,
                "expected_version": 0,
                "purpose": "Updated once",
            },
        )
        current_version = next(
            entry["version"]
            for entry in updated["data"]["resource_refs"]
            if entry["resource_type"] == "invoice_item"
        )
        stale_operation = _operation_id()
        stale, _ = await _call_tool(
            agent,
            harness,
            "update_invoice_item",
            {
                "operation_id": stale_operation,
                "item_id": item_id,
                "expected_version": 0,
                "purpose": "Must not apply",
            },
            expected_error="stale_version",
        )
        assert stale["error"]["http_status"] == 409
        failed_operation, _ = await _call_tool(
            agent, harness, "get_agent_operation", {"operation_id": stale_operation}
        )
        assert failed_operation["data"]["status"] == "failed"
        assert failed_operation["data"]["outcome"] == "not_applied"

        original_detail, _ = await _call_tool(
            agent, harness, "get_invoice_item", {"item_id": item_id}
        )
        original_token = original_detail["data"]["review"]["token"]
        twin, _ = await _call_tool(
            agent,
            harness,
            "create_manual_invoice_draft",
            {
                "operation_id": _operation_id(),
                "merchant": "Idempotency Merchant",
                "expense_date": "2026-08-21",
                "amount_cents": 12345,
                "currency": "CNY",
                "purpose": "Updated once",
                "project_id": project_id,
            },
        )
        assert _resource_id(twin, "invoice_item") != item_id
        review_changed, _ = await _call_tool(
            agent,
            harness,
            "confirm_invoice_item",
            {
                "operation_id": _operation_id(),
                "item_id": item_id,
                "expected_version": current_version,
                "review_token": original_token,
                "duplicate_resolution": "none",
                "acknowledged_uncertainty_ids": [],
                "acknowledged_duplicate_ids": [],
            },
            expected_error="review_changed",
        )
        assert review_changed["error"]["http_status"] == 409

        before_http = len(harness.requests)
        before_recognition = len(harness.recognition_calls)
        missing_ack_result = await agent.call_tool(
            "import_invoice_file",
            {
                "operation_id": _operation_id(),
                "file_path": str(harness.allowed_root / "missing-before-ack.png"),
            },
        )
        missing_ack = _structured(missing_ack_result)
        Draft202012Validator(CONTRACTS["import_invoice_file"].output_schema).validate(missing_ack)
        assert missing_ack_result.is_error is True
        assert missing_ack["error"]["code"] == "external_processing_ack_required"
        assert len(harness.requests) == before_http
        assert len(harness.recognition_calls) == before_recognition

        payment_ack_result = await agent.call_tool(
            "add_invoice_attachment",
            {
                "operation_id": _operation_id(),
                "item_id": item_id,
                "expected_version": current_version,
                "file_path": str(harness.allowed_root / "missing-payment.png"),
                "category": "payment_record",
            },
        )
        payment_ack = _structured(payment_ack_result)
        Draft202012Validator(CONTRACTS["add_invoice_attachment"].output_schema).validate(payment_ack)
        assert payment_ack_result.is_error is True
        assert payment_ack["error"]["code"] == "external_processing_ack_required"
        assert len(harness.requests) == before_http
        assert len(harness.recognition_calls) == before_recognition

        outside, records = await _call_tool(
            agent,
            harness,
            "import_invoice_file",
            {
                "operation_id": _operation_id(),
                "file_path": str(outside_file),
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": True,
            },
            expected_error="file_outside_allowed_roots",
        )
        assert outside["error"]["http_status"] == 0
        assert records == []
        assert len(harness.recognition_calls) == before_recognition

        successful_import, _ = await _call_tool(
            agent,
            harness,
            "import_invoice_file",
            {
                "operation_id": _operation_id(),
                "file_path": str(allowed_file),
                "external_processing_notice_version": "deepseek-v1",
                "external_processing_ack": True,
            },
        )
        assert _resource_id(successful_import, "invoice_item") > 0
        assert len(harness.recognition_calls) == before_recognition + 1

    # Neither the source path nor the allowed-root path may cross the HTTP or DTO boundary.
    serialized_http = json.dumps(harness.requests, ensure_ascii=False)
    serialized_result = json.dumps(successful_import, ensure_ascii=False)
    assert str(allowed_file) not in serialized_http
    assert str(outside_file) not in serialized_http
    assert str(harness.allowed_root) not in serialized_result
    assert str(harness.data_dir) not in serialized_result
