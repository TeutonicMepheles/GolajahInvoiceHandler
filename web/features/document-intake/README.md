# Document Intake frontend Feature

## Responsibility

This Feature owns the complete browser UI for importing reimbursement files,
separating primary documents from supporting material, previewing managed files,
and associating a supporting draft with a primary-document draft. It also owns
the intake table/card switch and the intake-local selection state wiring.

Every uploaded image or PDF is rendered through the same thumbnail button in
both table and card views. This includes primary documents, unassociated
supporting files, and supporting files already nested under a primary document.
Every button opens one same-page reader docked on the right: images use an
inline image surface and PDFs use the browser's same-origin inline frame. The
reader becomes a right-side overlay at constrained widths and never downloads
the file or opens a new browser tab. Table thumbnails omit the category selector
because that control is rendered in the table's dedicated material-type column.

Primary-document status is derived from bootstrap category metadata where
`material === "primary_receipt"`; the Feature does not hard-code the three
category codes. A draft with no attachment remains a main draft because it is a
valid manually created reimbursement entry.

## Public surface

- `renderDocumentIntake(options)` renders and wires `#page-content`.

The required integration options are:

- `items`: the authoritative current `/api/drafts` item array.
- `reloadBootstrap()`: refreshes navigation and bootstrap reference data.
- `refresh()`: re-reads drafts and renders the current intake page.
- `onManual()`: opens the shell-owned manual-entry flow.
- `onEdit(item)`: opens the shell-owned item editor.
- `onDelete(items)`: opens the shell-owned single or bulk deletion flow.
- `onReview({ item, changes, initialMergeTargetId })`: delegates confirmation
  or duplicate merge to the invoice-review Feature through the application
  shell.
- `onMutationFailure(error, { refresh })`: applies the shell-owned concurrency
  error policy.

## HTTP assumptions

- Existing `POST /api/imports` accepts multipart `files` and returns `results`.
- `/api/bootstrap` advertises both `document_intake_association` and
  `inline_attachment_preview` under `capabilities`. A missing flag means that an
  older backend is still serving the new static frontend; the Feature disables
  that action and shows a localized restart-and-refresh message instead of
  sending a request to a route that may not exist.
- Attachment DTOs expose same-origin `thumbnail_url` and `preview_url` fields.
- `POST /api/document-intake/associations` accepts JSON
  `{ source_item_id, target_item_id, source_version, target_version }`.
- Existing `PATCH /api/attachments/:id` accepts `category` plus the owning
  item's version precondition.

After every successful association the Feature reloads bootstrap data and then
calls `refresh()`; it does not predict versions or mutate item ownership in the
browser.

## Dependencies and boundaries

- Depends only on `web/core/state.js` and modules under `web/shared/`.
- Does not import invoice-review or any other Feature. Cross-Feature actions are
  callbacks supplied by `web/app.js`.
- The active reader selection, loading race guard, close/focus restoration, and
  responsive dock orchestration remain Feature-local. The reader does not fall
  back to an attachment download URL or require a PDF viewer dependency.
- Internal drag data uses
  `application/x-invoice-assistant-supporting-draft`; arbitrary text and external
  file drags are never interpreted as an association.
