# Document Intake Association and Preview

Status: Implemented

Last verified: 2026-08-24

## Goal

Make the intake workflow reflect the reimbursement domain: Chinese VAT invoices,
foreign Invoices, and foreign Receipts are the primary documents that own an
expense record, while purchase lists, payment records, screenshots, and
unclassified files are supporting attachments that must be associated with a
primary document before they can proceed.

## In scope

- Keep the existing managed-file and carrier-draft storage model while presenting
  primary documents and unassociated supporting files as different UI levels.
- Add a version-protected browser API that atomically associates a supporting
  carrier draft with a selected primary-document draft without copying the file.
- Support dragging a supporting card onto a primary card, with an explicit
  select/button fallback for keyboard and touch users.
- In both table and card views, show a bounded thumbnail for every primary
  document, unassociated supporting attachment, and already-associated
  attachment; PDFs use their first page.
- Make every thumbnail itself a preview control: images open in a large-image
  reader and PDFs open through the inline browser document response.
- Keep the reader docked on the right side of the intake workspace so users can
  inspect a file while retaining the primary/supporting context and association
  controls; collapse it into a bounded overlay on narrow screens.
- Advertise the association/inline-preview capability in bootstrap data. A new
  frontend served by a still-running old backend must disable association with
  a localized restart explanation instead of issuing a known-absent route.
- Permit only the validated same-origin attachment preview endpoint to be framed
  by the application; all other responses retain the global anti-framing policy.
- Preserve category correction so an `unknown` file can be promoted to a primary
  document or kept as an attachment.
- Move the intake rendering and interaction orchestration out of `web/app.js`
  into an owning frontend Feature.
- Add regression tests and update user/Feature documentation.

## Out of scope

- Automatically associating files based only on upload order or recognition
  confidence.
- Changing recognition categories or prompts.
- A database migration to nullable/unowned attachments.
- Relaxing the duplicate-review token rules on the existing merge endpoint.
- Reorganizing already submitted or reimbursed records.
- Introducing a frontend framework, bundler, or package-manager dependency.
- Adding PDF annotation/editing, a third-party PDF renderer, or cross-origin
  document embedding.

## Acceptance gates

- `invoice`, `foreign_invoice`, and `receipt` are derived from bootstrap material
  metadata as the only primary-file categories; all other uploaded files render
  in a separate “待关联附件” area and have no direct confirm action.
- Table and card views both preserve the hierarchy. In each view, every primary
  document, unassociated supporting attachment, and already-associated
  attachment displays a bounded image or first-page PDF thumbnail; a table cell
  may not degrade to only a filename or attachment count.
- Every thumbnail is a button. Images and PDFs open in one right-side docked
  reader within the current page; PDF content uses a same-origin inline frame
  and neither file type triggers a download or new tab. The dock exposes the
  filename, type, close action, loading state, selected-thumbnail state, and a
  visible error fallback without removing the file or hiding its role.
- On narrow screens the reader remains inside the web application as a bounded
  overlay with an explicit close action and no page-level horizontal overflow.
- A supporting card can be dragged onto a primary card. The same operation is
  available through an explicit target selector and button.
- Association rechecks both item versions and roles in one transaction, moves no
  physical file, marks the empty carrier draft as merged, refreshes normalized
  names and foreign-payment data, increments both versions, and writes audits.
- Self-association, primary-as-attachment, non-primary targets, stale versions,
  and locked records fail without a partial move.
- Existing `/api/imports`, duplicate merge, attachment upload/download, Agent,
  export, and material-completeness contracts remain compatible; all new HTTP
  fields and routes are additive.
- Attachment preview responses use `inline`, `nosniff`, and a narrowly scoped
  `SAMEORIGIN`/`frame-ancestors 'self'` exception. Non-preview application and
  API responses remain `DENY`/`frame-ancestors 'none'`.
- If bootstrap lacks the document-intake association capability, target selects,
  drag handles, and association buttons are disabled with a clear restart notice;
  a route-level 404 is also translated into that localized recovery message.
- The frontend Feature depends only on application state and shared modules;
  invoice-review collaboration is injected by the application shell.
- Full pytest, `node --check` for every JavaScript file under `web/`, and a real
  browser exercise of grouping, drag association, refresh persistence, table and
  card thumbnails for all three file roles, docked image/PDF online preview,
  responsive close behavior, and Console output all pass. The restarted formal
  local service is also checked so new frontend assets cannot be paired with its
  previous in-memory route map.

## Evidence

- `2026-08-24`: `.\.venv\Scripts\python.exe -m pytest -q` completed
  with `159 passed in 30.60s`. The suite includes the shared-renderer contract
  requiring clickable thumbnails for main files, unassociated attachments, and
  associated attachments in both table and card views.
- `2026-08-24`: `node --check` passed for all 9 JavaScript files under
  `web/` after the expanded table rendering.
- `2026-08-24`: an isolated real-browser exercise rendered three clickable
  thumbnails in the default table view: one PDF primary document, one already-
  associated PNG, and one unassociated PNG. Switching to card view retained all
  three thumbnails in the correct hierarchy.
- `2026-08-24`: both synthetic PNG thumbnails opened their original-resolution
  image previews; clicking the PDF thumbnail produced
  a successful `GET /api/attachments/1/preview` response. A 390 px viewport had
  no page-level horizontal overflow, while wide tables remained contained in
  their own scroll regions. The browser Console recorded zero warnings or
  errors.
- `2026-08-24`: an isolated real-browser exercise imported one PDF primary
  document and one PNG supporting file, rendered separate primary/supporting
  groups and both thumbnails, associated the support by pointer drag, and after
  refresh retained `1 个主条目 · 0 个待关联附件` with the PNG nested under
  `已关联附件`.
- `2026-08-24`: the final full regression completed with `171 passed in
  32.32s`; `node --check` also passed for all 10 JavaScript files under `web/`.
- `2026-08-24`: a fresh isolated browser run imported one PDF primary receipt
  and two PNG supporting files. Table and card modes each rendered all three
  thumbnail buttons; selector association nested both PNGs under the receipt,
  persisted across a full reload, and retained an open attachment preview while
  ownership refreshed. This complements the earlier pointer-drag evidence above.
- `2026-08-24`: the same browser session opened the PDF in a native same-origin
  iframe reader and a synthetic image in the same right dock without opening
  another tab. Close and Escape returned focus to the source thumbnail. At a
  390 px viewport the dock fit the content width with zero page-level horizontal
  overflow. Console warnings and errors remained empty.
- `2026-08-24`: before the formal service restart, its new static frontend paired
  with the old in-memory backend showed the localized restart notice, disabled
  association, and rendered the preview recovery message in the dock. After the
  scheduled task restarted, `/api/document-intake/associations` returned 405 to
  a GET probe instead of 404, bootstrap advertised both capabilities, preview
  HEAD returned 200 with `SAMEORIGIN`/`frame-ancestors 'self'`, and ordinary
  bootstrap remained `DENY`/`frame-ancestors 'none'`.
- `2026-08-24`: read-only browser validation against the restarted formal data
  opened its PDF and image in the dock, kept the tab count
  unchanged, enabled the association controls, and recorded no Console warnings
  or errors. Isolated browser fixtures were moved to the Windows Recycle Bin.
