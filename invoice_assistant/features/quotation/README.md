# Quotation backend feature

## Responsibility

Owns the HTTP adaptation for quotation calculation and Word export while delegating business work to the existing request-context-independent modules.

## Public HTTP surface

- `POST /api/quotations/calculate`
- `POST /api/quotations/export`

## Dependencies

- `invoice_assistant.http.json_body` for shared JSON request validation.
- `invoice_assistant.quotation.calculate_quotation` for calculation and validation.
- `invoice_assistant.quotation_export.build_quotation_docx` for export validation and document generation.
- `invoice_assistant/templates/quotation_template.docx` is an anonymous structural template: it must not contain brand assets, contact details, real financial values, embedded images, or personal author metadata.

The Blueprint must not contain a second calculation, document layout implementation, or persistence side effect.
