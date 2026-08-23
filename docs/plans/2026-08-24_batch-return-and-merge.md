# Reimbursement Batch Return and Merge

Status: Implemented

Last verified: 2026-08-24

## Goal

Let users bring any submitted or reimbursed package back into the editable
workspace, and consolidate other same-project editable packages into the
package currently open in that workspace so all items can be managed and
exported together.

## In scope

- Expose the existing version-protected history-package reopen operation for
  every submitted and reimbursed package, not only records with a detected
  foreign-payment problem.
- Add an atomic, additive batch-merge API owned by a backend Feature.
- Treat the editable package currently open in the workspace as the fixed merge
  target, and let the user select one or more other editable source packages
  from the same project.
- Keep the target package's name, project, purpose, and notes; append every
  source item in stable package/item order and recalculate the CNY total.
- Delete the now-empty source package records while preserving item records,
  managed attachments, and item audit continuity.
- Create and audit a retained database snapshot before source package records
  are removed, and reject a source whose referenced old archive is currently
  unavailable instead of losing its recovery pointer.
- Require exact package versions and explicit target-name confirmation.
- If a source package was reopened from history, explicitly move its superseded
  archive into the 30-day recovery area; retain the target package's own prior
  archive until the next successful export.
- Add backend/frontend Feature READMEs, regression tests, user documentation,
  and real-browser validation.

## Out of scope

- Merging submitted or reimbursed packages directly; they must first be
  returned to the editable workspace.
- Merging editable packages from different projects.
- Merging a package while it is exporting.
- Combining more than 200 expense items into one package.
- Automatically reconciling differing package names, projects, purposes, or
  notes; the selected target owns the resulting metadata.
- Splitting a package or undoing a completed merge automatically.
- Adding any Agent MCP tool or changing the exact existing 17-tool/15-tool
  manifests, HTTP mappings, or export contracts. Batch return and batch merge
  remain browser/API capabilities and are not exposed through Agent tools.
- A database schema migration.

## Acceptance gates

- Every history card in `submitted` or `reimbursed` state offers “退回编辑”.
  Exact-name confirmation and expected version are required; the original
  archive remains available until the replacement export succeeds.
- When the current workspace target has at least one other same-project draft
  package available, “本次报销” offers a merge action with an immutable current-
  target summary, source checkboxes, totals, archive-disposition warning, and
  exact target-name confirmation. Different-project packages cannot be
  selected and are also rejected by the API. Unsaved target metadata blocks the
  merge action until the user saves it.
- The merge API rejects self/duplicate sources, missing or stale versions,
  non-draft or exporting packages, empty source selection, more than 200 total
  items, and unconfirmed superseded-archive cleanup without a partial move.
- A successful merge moves only `batch_items` ownership, preserves managed
  attachments, keeps all expense items `in_batch`, increments affected item and
  target-package versions, recalculates the target total, removes source batch
  records, and writes target/item/system audits.
- Superseded source archives are queued and processed through the existing
  recoverable cleanup mechanism. Cleanup failure is reported without rolling
  back an already committed database merge. A configured archive that is
  currently unavailable rejects the merge with all package membership and
  recovery references unchanged.
- A retained manual database snapshot is created before the destructive source-
  package grouping change, and its name is recorded in source, target, and
  system merge audits.
- The merged target remains editable and can complete the existing unified
  archive/PDF export flow without changing that contract.
- The application shell imports the batch-management Feature's public frontend
  entry instead of defining reopen or merge modal logic, and the additive merge
  route is owned by the backend batch-management Blueprint. Both Features have
  local READMEs and their static/public entry contracts are architecture-tested.
- Agent discovery remains exactly 17 tools with allowed roots and 15 tools
  without them; no batch-merge or history-reopen tool is added.
- Full pytest, `node --check` for every JavaScript file under `web/`, and a real
  browser exercise of return, merge, editability, unified item count/total,
  export readiness, and Console output all pass.

## Evidence

- Backend and frontend Feature slices are implemented in
  `invoice_assistant/features/batch_management/` and
  `web/features/batch-management/`; the application shell only composes their
  public entry points.
- Focused batch-management and architecture regression:
  `18 passed in 2.76s`.
- Full backend/API/architecture/Agent regression:
  `.\.venv\Scripts\python.exe -m pytest -q` → `170 passed in 31.65s`.
- JavaScript syntax: `node --check` passed for all 10 `.js` files under `web/`.
- Real in-app browser validation against an isolated database completed this
  flow: submitted history package → exact-name keyboard-confirmed return to edit
  → unsaved-metadata merge guard → saved target → two same-project source
  packages selected → summary showed the resulting three synthetic items → merge
  retained target metadata and all three attachments → unified export produced
  a 7-page PDF and returned the package to submitted history. Browser developer
  logs contained zero warnings or errors.
- Filesystem evidence from that isolated run showed one active export with all
  three original materials, the superseded target export under the 30-day trash
  tree, and separate manual database snapshots for return and merge. The
  isolated validation directory was then sent to the Windows Recycle Bin.
