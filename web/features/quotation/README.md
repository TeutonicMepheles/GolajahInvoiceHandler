# Quotation frontend feature

## Responsibility

Owns the quotation calculator UI, editable item/child distribution, optional description column, clipboard output, and Word export interaction.

## Public surface

- `renderQuotation(pageVersion)` from `index.js` mounts and initializes the complete quotation page.

All other functions are feature-private. The application Shell must not call quotation rendering helpers or mutate quotation DOM directly.

## Dependencies

- `core/state.js` for the quotation state slice and navigation version guard.
- `shared/api.js` for JSON calculation requests.
- `shared/dom.js`, `shared/forms.js`, and `shared/ui.js` for domain-independent browser primitives.
- Backend contracts `POST /api/quotations/calculate` and `POST /api/quotations/export`.

This module contains no framework or build-tool dependency. A future component/Harness split requires its own Plan; do not create a second quotation calculation algorithm in the frontend.
