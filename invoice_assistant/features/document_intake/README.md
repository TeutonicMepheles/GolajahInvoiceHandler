# Document Intake backend Feature

## Responsibility

This Feature owns the additive HTTP and domain behavior used to organize newly
imported reimbursement documents. It can associate a supplementary-material
draft with a pending primary-document draft without copying its managed files,
and it provides safe inline previews and bounded first-page thumbnails.

The existing import and duplicate-review contracts remain unchanged. Invoices,
Invoice documents, and Receipt documents still use the duplicate-review merge
flow when they may represent the same reimbursement.

## Public surface

- `POST /api/document-intake/associations` atomically associates all files from
  one supplementary pending draft with one primary pending draft. The request
  contains exactly `source_item_id`, `target_item_id`, `source_version`, and
  `target_version`.
- `GET /api/attachments/{id}/preview` serves a validated managed PDF or image
  inline without exposing its local path or forcing a download.
- `GET /api/attachments/{id}/thumbnail` returns a bounded JPEG. PDF thumbnails
  render only the first page.
- Existing `/api/bootstrap` data advertises
  `document_intake_association` and `inline_attachment_preview` so a newly loaded
  frontend can detect a backend process that still needs to be restarted.

## Dependencies and boundaries

- Routes perform JSON and response adaptation only. Association, path
  validation, and thumbnail rendering are callable without a request context.
- Association accepts only `purchase_list`, `payment_record`, or `unknown`
  source material and requires a target containing `invoice`,
  `foreign_invoice`, or `receipt` material.
- Both items must still be `pending_confirmation` and match the supplied
  versions. The database mutation, name rebinding, derived RMB payment refresh,
  version increments, and audit entries are one transaction.
- Managed files are neither copied nor moved. Preview paths must resolve below
  the configured import root and must still exist.
- Framing permission is narrowed to the inline preview endpoint: it receives
  `X-Frame-Options: SAMEORIGIN` and a same-origin `frame-ancestors` policy so the
  application can embed PDFs. All other responses remain non-frameable with
  `DENY` and `frame-ancestors 'none'`; the application policy permits only
  same-origin frame sources.
