# Batch Management frontend Feature

## Responsibility

This Feature owns the new reimbursement-package lifecycle interactions that are
shared by the legacy workspace and history screens:

- returning any submitted or reimbursed package to the editable workspace;
- selecting one or more editable source packages and merging them into the
  currently focused target package;
- presenting the metadata-retention, item-limit, version-confirmation, and old-
  archive recovery consequences before either action.
- preventing a merge from opening while target metadata edits are still
  unsaved, so a post-merge refresh cannot silently discard the form state.

## Public surface

- `renderBatchMergeAction(target, batches)` returns the workspace action markup.
- `wireBatchMergeAction(root, options)` wires the merge modal to that action.
- `openBatchMergeModal(target, batches, options)` performs a versioned merge.
- `openBatchReopenModal(batch, options)` performs the existing versioned reopen.

Navigation, current-page state, bootstrap refresh, and legacy workspace/history
rendering remain owned by `web/app.js` and are supplied through callbacks.

## HTTP assumptions

- `POST /api/batch-management/merge` accepts
  `{ target_batch_id, target_version, sources, confirmation,
  discard_source_archives }`.
- `POST /api/batches/:id/reopen` remains the compatible history reopen route.

## Dependencies and boundaries

- Depends only on modules under `web/shared/`.
- Does not import application state or another Feature.
- Keeps merge selection local to the modal instead of reusing application-level
  item/draft selections.
- Distinguishes retryable archive cleanup warnings from already-missing legacy
  PDF metadata in the user feedback.
