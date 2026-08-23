# Invoice Review frontend Feature

## Responsibility

This Feature owns the browser-only invoice review interaction used before a
draft is confirmed or merged. It fetches the authoritative item detail,
renders uncertainty and duplicate decisions, keeps review tokens in modal-local
memory, and drives the sequential overflow review-session protocol.

## Public surface

- `openInvoiceReview(options)` opens the review flow for one pending draft.
- `isInvoiceReviewConflict(error)` classifies version, review, and review-session
  conflicts that require a fresh read rather than an automatic retry.

The application shell supplies the latest list snapshot, optional unsaved form
values, and completion/conflict callbacks. This Feature never owns navigation
or another page's state.

## Dependencies and boundaries

- Depends only on `web/shared/api.js`, `web/shared/dom.js`, and
  `web/shared/ui.js`.
- Review, overflow, and one-time authorization tokens remain inside the active
  modal closure. They are not placed in global application state, URLs, logs,
  or persistent browser storage.
- Confirm never carries editable financial fields. If the card has changed,
  the Feature first performs a versioned `PATCH`, then reads a fresh detail and
  review token before allowing confirm or merge.
