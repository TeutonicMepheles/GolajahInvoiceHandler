from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .file_access import VerifiedFile
from .contracts import TOOL_MANIFEST


BASE_URL = "http://127.0.0.1:8765"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT = 2
DEFAULT_TIMEOUT = 30
LONG_TIMEOUT = 330
WRITE_TOOLS = frozenset(
    entry.name for entry in TOOL_MANIFEST if not entry.annotations.read_only_hint
)


@dataclass(frozen=True, slots=True)
class HttpResult:
    payload: dict[str, Any]
    request_id: str
    replayed: bool = False


class ClientError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 0,
        retryable: bool = False,
        outcome: str = "not_applied",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.outcome = outcome if outcome in {"not_applied", "unknown"} else "unknown"


class LeaseGuard:
    def __init__(self, path: str | None = None, generation: str | None = None) -> None:
        self.path = path if path is not None else os.environ.get("INVOICE_MCP_REGISTRATION_LEASE_PATH")
        self.generation = generation if generation is not None else os.environ.get("INVOICE_MCP_REGISTRATION_GENERATION")

    def stale(self) -> bool:
        if self.path is None and self.generation is None:
            return False
        if not self.path or not self.generation:
            return True
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(4097)
        except OSError:
            return True
        if len(raw) > 4096:
            return True
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return True
        if text == self.generation:
            return False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return True
        if (
            not isinstance(payload, dict)
            or payload.get("generation") != self.generation
            or payload.get("active") is not True
        ):
            return True

        # Leases produced by the transactional registrar carry a managed-state
        # activation gate.  The registrar writes the candidate config and the
        # rotated lease first, validates the real client, and writes managed
        # state last.  Until that final write, both the old and candidate MCP
        # processes therefore fail closed.  Legacy bare leases remain readable
        # so a directly launched development server is not broken.
        state_path = payload.get("managed_state_path")
        client = payload.get("client")
        if state_path is None:
            return False
        if not isinstance(state_path, str) or not state_path or not isinstance(client, str):
            return True
        try:
            with open(state_path, "rb") as handle:
                state_raw = handle.read(65537)
        except OSError:
            return True
        if len(state_raw) > 65536:
            return True
        try:
            state = json.loads(state_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(state, dict) or not isinstance(state.get("targets"), dict):
            return True
        target = state["targets"].get(client)
        if not isinstance(target, dict) or target.get("generation") != self.generation:
            return True
        recorded_lease = target.get("lease_path")
        if not isinstance(recorded_lease, str):
            return True
        try:
            return os.path.normcase(str(Path(recorded_lease).resolve(strict=False))) != os.path.normcase(
                str(Path(self.path).resolve(strict=False))
            )
        except (OSError, RuntimeError):
            return True


class LocalInvoiceClient:
    def __init__(self, *, lease_guard: LeaseGuard | None = None) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Accept": "application/json", "User-Agent": "invoice-assistant-mcp/1"})
        self.lease_guard = lease_guard or LeaseGuard()

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _set_stream_timeout(response: requests.Response, seconds: float) -> None:
        """Best-effort tightening of the live urllib3 socket deadline."""
        current: Any = response.raw
        for attribute in ("_fp", "fp", "raw", "_sock"):
            current = getattr(current, attribute, None)
            if current is None:
                return
        setter = getattr(current, "settimeout", None)
        if setter:
            setter(max(0.001, seconds))

    def _read_json_response(
        self,
        response: requests.Response,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if response.is_redirect or 300 <= response.status_code < 400:
            raise ClientError("redirect_rejected", "The local service returned a redirect, so the request was refused.", http_status=response.status_code)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/json", "application/problem+json"}:
            raise ClientError("invalid_service_response", "The local service did not return JSON.", http_status=response.status_code)
        chunks: list[bytes] = []
        size = 0
        iterator = response.iter_content(chunk_size=64 * 1024)
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.ReadTimeout("response deadline exceeded")
                self._set_stream_timeout(response, remaining)
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ClientError("response_too_large", "The local service response exceeded the 4 MiB safety limit.", http_status=response.status_code)
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ClientError("invalid_service_response", "The local service returned malformed JSON.", http_status=response.status_code)
        if not isinstance(payload, dict):
            raise ClientError("invalid_service_response", "The local service returned an unexpected JSON shape.", http_status=response.status_code)
        return payload

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        write: bool = False,
        deadline: float | None = None,
    ) -> tuple[requests.Response, dict[str, Any]]:
        deadline = deadline or (time.monotonic() + timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError(
                "service_timeout",
                "The local service did not respond before the deadline.",
                retryable=True,
                outcome="unknown" if write else "not_applied",
            )
        try:
            response = self.session.request(
                method,
                BASE_URL + path,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers=headers,
                timeout=(min(CONNECT_TIMEOUT, max(0.001, remaining)), max(0.001, remaining)),
                allow_redirects=False,
                stream=True,
            )
        except requests.ConnectTimeout:
            raise ClientError("service_timeout", "The local service connection timed out.", retryable=True, outcome="not_applied")
        except requests.ReadTimeout:
            raise ClientError("service_timeout", "The local service did not respond before the deadline.", retryable=True, outcome="unknown" if write else "not_applied")
        except requests.ConnectionError:
            # Requests uses ConnectionError both for a refused connection and for
            # a connection that disappears after request bytes may have been sent.
            # A write therefore cannot safely be reported as not applied; callers
            # must query the durable operation ID before retrying.
            raise ClientError(
                "service_connection_lost" if write else "service_stopped",
                "The local invoice-assistant service connection was unavailable or ended unexpectedly.",
                retryable=True,
                outcome="unknown" if write else "not_applied",
            )
        except requests.RequestException:
            raise ClientError("service_request_failed", "The local service request failed safely.", retryable=True, outcome="unknown" if write else "not_applied")
        try:
            try:
                payload = self._read_json_response(response, deadline=deadline)
            except ClientError as exc:
                if write:
                    raise ClientError(
                        exc.code,
                        exc.message,
                        http_status=exc.http_status,
                        retryable=exc.retryable,
                        outcome="unknown",
                    ) from exc
                raise
            except requests.ReadTimeout:
                raise ClientError(
                    "service_timeout",
                    "The local service did not respond before the deadline.",
                    retryable=True,
                    outcome="unknown" if write else "not_applied",
                )
            except requests.ConnectionError:
                raise ClientError(
                    "service_connection_lost",
                    "The local service connection ended before a verified response was received.",
                    retryable=True,
                    outcome="unknown" if write else "not_applied",
                )
            return response, payload
        finally:
            response.close()

    def service_status(self, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        if self.lease_guard.stale():
            return {
                "status": "registration_stale",
                "service": None,
                "platform_hint": "Close or refresh this Agent session, then use the current registered configuration.",
            }
        try:
            response, payload = self._send("GET", "/health", timeout=timeout)
        except ClientError as exc:
            if exc.code == "service_timeout":
                status = "timeout"
            elif exc.code == "service_stopped":
                status = "stopped"
            else:
                status = "wrong_service"
            return {
                "status": status,
                "service": None,
                "platform_hint": "Start the invoice assistant separately; MCP never starts it automatically.",
            }
        service = payload.get("service")
        if service != "invoice-assistant" or response.status_code not in {200, 503}:
            return {"status": "wrong_service", "service": None if service is None else str(service), "platform_hint": None}
        status = "running" if response.status_code == 200 and payload.get("ok") is True else "degraded"
        return {"status": status, "service": "invoice-assistant", "platform_hint": None}

    def _require_running(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        status = self.service_status(timeout=timeout)["status"]
        if status == "running":
            return
        messages = {
            "registration_stale": "This MCP process uses a stale registration lease. Refresh the Agent session.",
            "degraded": "The local invoice assistant is degraded, so business tools are disabled.",
            "stopped": "The local invoice assistant is stopped; MCP will not start it.",
            "timeout": "The local invoice assistant health check timed out.",
            "wrong_service": "Port 8765 is not serving the expected local invoice assistant.",
        }
        raise ClientError(status, messages.get(status, "The local invoice assistant is unavailable."), retryable=status in {"stopped", "timeout", "degraded"})

    @staticmethod
    def _request_id(response: requests.Response) -> str:
        value = response.headers.get("X-Request-ID")
        return str(value)[:128] if value else str(uuid.uuid4())

    @staticmethod
    def _error_from_response(response: requests.Response, payload: dict[str, Any], *, write: bool) -> ClientError:
        source = payload.get("error")
        if isinstance(source, dict):
            code = str(source.get("code") or "service_error")
            retryable = bool(source.get("retryable"))
            outcome = str(source.get("outcome") or "not_applied")
        else:
            code = str(source or "service_error")
            retryable = response.status_code >= 500
            outcome = str(payload.get("outcome") or ("unknown" if write and response.status_code >= 500 else "not_applied"))
        if not re.fullmatch(r"[a-z0-9_]{1,120}", code):
            code = "service_error"
        message = f"The local invoice assistant rejected the request ({code})."
        return ClientError(code, message, http_status=response.status_code, retryable=retryable, outcome=outcome)

    def _business(
        self,
        method: str,
        path: str,
        *,
        tool_name: str,
        operation_id: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResult:
        deadline = time.monotonic() + timeout
        self._require_running(
            timeout=min(DEFAULT_TIMEOUT, max(0.001, deadline - time.monotonic()))
        )
        headers: dict[str, str] = {}
        if operation_id is not None:
            headers["Idempotency-Key"] = operation_id
            headers["X-Invoice-Agent-Tool"] = tool_name
        response, payload = self._send(
            method,
            path,
            params=params,
            json_body=json_body,
            data=data,
            files=files,
            headers=headers,
            timeout=max(0.001, deadline - time.monotonic()),
            write=operation_id is not None,
            deadline=deadline,
        )
        if response.status_code >= 400 or payload.get("ok") is False:
            raise self._error_from_response(response, payload, write=operation_id is not None)
        replayed = bool(payload.get("replayed") or (isinstance(payload.get("meta"), dict) and payload["meta"].get("replayed")))
        return HttpResult(payload=payload, request_id=self._request_id(response), replayed=replayed)

    def invoke(self, tool_name: str, arguments: dict[str, Any], verified_file: VerifiedFile | None = None) -> HttpResult:
        args = dict(arguments)
        operation_id = args.pop("operation_id", None) if tool_name in WRITE_TOOLS else None
        if tool_name == "get_invoice_reference_data":
            return self._business("GET", "/api/bootstrap", tool_name=tool_name)
        if tool_name == "get_dashboard_summary":
            return self._business("GET", "/api/dashboard", tool_name=tool_name)
        if tool_name == "get_agent_operation":
            return self._business("GET", f"/api/agent-operations/{args['operation_id']}", tool_name=tool_name)
        if tool_name == "list_invoice_drafts":
            args.setdefault("limit", 50)
            return self._business("GET", "/api/drafts", tool_name=tool_name, params=args)
        if tool_name == "get_invoice_item":
            return self._business("GET", f"/api/items/{args['item_id']}", tool_name=tool_name)
        if tool_name == "list_invoice_items":
            args.setdefault("limit", 50)
            return self._business("GET", "/api/items", tool_name=tool_name, params=args)
        if tool_name == "list_reimbursement_batches":
            args.setdefault("limit", 50)
            return self._business("GET", "/api/batches", tool_name=tool_name, params=args)
        if tool_name == "get_reimbursement_batch":
            return self._business("GET", f"/api/batches/{args['batch_id']}", tool_name=tool_name)
        if tool_name == "create_manual_invoice_draft":
            return self._business("POST", "/api/items/manual", tool_name=tool_name, operation_id=operation_id, json_body=args)
        if tool_name == "update_invoice_item":
            item_id = args.pop("item_id")
            return self._business("PATCH", f"/api/items/{item_id}", tool_name=tool_name, operation_id=operation_id, json_body=args)
        if tool_name == "confirm_invoice_item":
            item_id = args.pop("item_id")
            return self._business("POST", f"/api/items/{item_id}/confirm", tool_name=tool_name, operation_id=operation_id, json_body=args)
        if tool_name == "merge_invoice_draft":
            source_id = args.pop("source_id")
            target_id = args.pop("target_id")
            return self._business("POST", f"/api/items/{source_id}/merge/{target_id}", tool_name=tool_name, operation_id=operation_id, json_body=args)
        if tool_name == "create_reimbursement_batch":
            return self._business("POST", "/api/batches", tool_name=tool_name, operation_id=operation_id, json_body=args)
        if tool_name == "export_reimbursement_batch":
            batch_id = args.pop("batch_id")
            return self._business(
                "POST",
                f"/api/batches/{batch_id}/export",
                tool_name=tool_name,
                operation_id=operation_id,
                json_body=args,
                timeout=LONG_TIMEOUT,
            )
        if tool_name in {"import_invoice_file", "add_invoice_attachment"}:
            if verified_file is None:
                raise ClientError("file_not_verified", "The file was not verified before upload.")
            args.pop("file_path", None)
            files = {"file": (verified_file.display_basename, verified_file.content, verified_file.mime_type)}
            form = {key: ("true" if value is True else "false" if value is False else str(value)) for key, value in args.items() if value is not None}
            if tool_name == "import_invoice_file":
                path = "/api/imports"
            else:
                item_id = form.pop("item_id")
                path = f"/api/items/{item_id}/attachments"
            request_timeout = (
                LONG_TIMEOUT
                if tool_name == "import_invoice_file" or form.get("category") == "payment_record"
                else DEFAULT_TIMEOUT
            )
            return self._business(
                "POST",
                path,
                tool_name=tool_name,
                operation_id=operation_id,
                data=form,
                files=files,
                timeout=request_timeout,
            )
        raise ClientError("unknown_tool", "The requested tool is not part of this MCP manifest.")
