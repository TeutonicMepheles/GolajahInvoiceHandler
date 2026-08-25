# Local Agent MCP

This Feature exposes a Windows-local STDIO MCP surface for the invoice assistant. It is a protocol adapter only: runtime tools call the fixed public HTTP service at `http://127.0.0.1:8765` and never import database, storage, export, recognition, or another Feature's private route code.

## Public surface

- `contracts.py`: the single 17-tool manifest, strict JSON Schemas, annotations, and 15-tool no-file subset.
- `server.py`: STDIO discovery and tool dispatch; `python -m invoice_assistant.features.agent_mcp.server` is the stable module entry.
- `client.py`: fixed loopback HTTP transport, health classification, response limits, timeouts, error mapping, and registration lease checks.
- `dto.py`: safe field allowlists and integer-money output conversion.
- `file_access.py`: explicit allowed-root parsing and same-handle bounded file reads.
- `routes.py`: HTTP adapter for safe persisted Agent-operation status; MCP runtime does not import it.
- `registration.py`: Windows client configuration and managed registration state; MCP runtime does not import it.

## Security boundary

File tools are absent from discovery unless registration supplies valid fixed-disk allowed roots. The MCP never enumerates those roots or returns local paths. File reads reject UNC/device/ADS paths, reparse points, hard links, non-regular files, path escape, identity changes, unsupported content, empty files, and files over 20 MiB.

Every business call rechecks `/health`, ignores proxy environment variables, refuses redirects and non-JSON responses, and caps responses at 4 MiB. Writes use a UUID v4 `Idempotency-Key`, an exact tool-name header, and entity versions; mutable entities are read again through get tools after a write. Import and payment-record attachment tools publish the fixed DeepSeek disclosure and require its versioned acknowledgement before any file read.

Registration, removal, and allowed-root changes rotate or revoke a generation lease. Candidate config and the rotated lease are installed fail-closed; real client parsing and independent 15-second MCP discovery complete before `managed-state.json` is written last to activate the generation. An already-running stale process exposes only `get_service_status=registration_stale` and refuses new business calls. Committed transaction cleanup is retryable and never rolls back an already committed registration.

Client capability checks are pinned to the Windows builds accepted by the owning Plan. Codex must echo every machine-verifiable frozen transport, allowlist, and timeout field; its non-echoed writes policy is checked in TOML and still requires the Plan's separate real-session approval evidence. Hermes additionally requires the inspected fail-closed per-call trust-gate source beside the validated executable; this source check is not represented as a fresh UI approval observation.

## Required-material workflow

The existing tools mirror the browser's required-material slots; no separate tool
or CLI command is needed:

1. Call `list_invoice_drafts` or `list_invoice_items` to find entries whose
   `material.complete` is false, then call `get_invoice_item` for the chosen item.
2. Treat `material.missing` as authoritative. Map `foreign_payment_rmb` and
   `payment_record` to the `payment_record` attachment category, and map
   `purchase_list` to `purchase_list`. Do not guess a file or target item.
3. Call `add_invoice_attachment` with a fresh UUID v4 `operation_id`, the absolute
   file path under a registered allowed root, the current item `version`, the
   current `batch_ref.batch_version` when present, and the mapped `category`.
   Payment records also require the published external-processing acknowledgement.
4. Call `get_invoice_item` again after every successful write. Use only the new
   versions and new `material.missing` result for the next upload; payment
   recognition may update the reimbursable RMB amount and immediately reveal a
   purchase-list requirement.

This recipe preserves the 17-tool manifest and all existing HTTP contracts. A
write result contains resource references, so it is not a substitute for the
authoritative post-write read.

## Dependencies

Runtime dependencies are the MCP Python SDK, `requests`, and the public invoice-assistant HTTP API. Registration additionally uses `tomlkit` and `ruamel.yaml`. No frontend module depends on this package.
