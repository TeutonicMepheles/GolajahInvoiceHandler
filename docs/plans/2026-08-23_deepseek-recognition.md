# DeepSeek Recognition Provider Migration

Status: Implemented

Last verified: 2026-08-23

## Goal

Replace the non-operational OpenAI recognition path with DeepSeek as the only runtime recognition provider while preserving the existing import, manual fallback, and `/api` contracts.

## In scope

- Move recognition business logic into an owning backend Feature.
- Use `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and `DEEPSEEK_MODEL` exclusively at runtime.
- Send supported images to `deepseek-v4-flash-vision-exp` through its OpenAI-compatible Responses API.
- Render PDF pages to in-memory JPEG images before recognition because DeepSeek vision does not accept PDF file inputs.
- Report configured state and the result of the most recent recognition attempt without exposing credentials.
- Update settings UI, environment examples, root documentation, feature documentation, and regression tests.

## Out of scope

- Provisioning or purchasing a DeepSeek API key or balance.
- Keeping OpenAI as a fallback provider.
- Automatically re-recognizing historical attachments.
- Changing existing import, attachment, or reimbursement API contracts.
- Claiming live DeepSeek validation without a user-configured key and balance.

## Acceptance gates

- Runtime and user-facing configuration contain no `OPENAI_API_KEY` or `OPENAI_MODEL` dependency.
- Image requests use only `input_image` parts and the configured DeepSeek vision model.
- Every PDF page is rendered to an image; no PDF `input_file` is sent to DeepSeek.
- Excessive PDF page counts fail safely and preserve the existing manual-draft fallback.
- Recognition status distinguishes unconfigured, unknown, available, and unavailable states and never returns a key.
- Existing backend/API tests pass, all frontend JavaScript passes `node --check`, and the settings/import flow is exercised in a real browser without Console errors.

## Evidence

- `2026-08-23`: `\.venv\Scripts\python.exe -m pytest -q` — 44 passed.
- `2026-08-23`: `node --check` executed for all 7 JavaScript files under `web/` — passed.
- `2026-08-23`: real-browser smoke test against an isolated local Flask instance — settings showed `deepseek-v4-flash-vision-exp`, unconfigured state, and no prior call; the intake page loaded its PDF/image entry; Console contained no errors.
- Focused tests verify DeepSeek client/base URL selection, structured Responses parsing, image data URLs, two-page PDF-to-JPEG conversion, page-limit fallback, missing-key status, and HTTP 402 balance messaging.
- `2026-08-23`: live DeepSeek validation passed with an existing invoice PDF in an isolated application context. The provider returned the expected structured invoice fields with no uncertainties; no production reimbursement record was created. Exact invoice values are intentionally omitted from the repository.
