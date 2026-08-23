# Batch Management backend Feature

## Responsibility

This Feature owns the atomic consolidation of editable reimbursement batches.
It preserves the target metadata and managed attachments, moves source item
membership in stable order, and safely schedules superseded source archives for
the existing 30-day recovery flow. The HTTP adapter creates a recoverable
database snapshot before any source package record can be removed.

## Public surface

- Blueprint: `batch_management_api`
- `POST /api/batch-management/merge`
- Callable service: `merge_reimbursement_batches(...)`
- Post-commit cleanup adapter: `complete_merge_cleanup(...)`

The merge request must contain exactly `target_batch_id`, `target_version`,
`sources`, `confirmation`, and `discard_source_archives`. Each source contains
exactly `batch_id` and `expected_version`.

## Boundaries and dependencies

- HTTP parsing and response construction live in `routes.py`.
- Transactional membership, version, audit, and archive-disposition behavior
  lives in `service.py` and remains callable without a request context.
- Batch serialization and total calculation reuse `batch_service.py`; database
  transactions/audits and the persistent cleanup queue remain shared domain
  services.
- The Feature never copies, renames, or deletes attachment files. Source archive
  cleanup is queued in the merge transaction and processed only after commit.
- A source package with a configured but currently unavailable superseded
  archive is rejected without changing membership or clearing that recovery
  reference. It can be merged after the archive path is available again.
- The route creates a retained manual database snapshot before invoking the
  destructive grouping change, and the snapshot name is recorded in merge
  audits for target, source, and system history.
- Existing batch creation, editing, reopen, export, reimbursement, and deletion
  contracts remain unchanged.
