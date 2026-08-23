# Recognition Feature

## Responsibility

This Feature owns AI-assisted extraction of reimbursement fields from supported images and PDFs. It uses DeepSeek as the only remote provider, converts PDF pages to images locally, validates structured output, and exposes a credential-safe last-attempt status.

## Public surface

- `recognize_file(path_value, mime_type, original_name)`: returns the existing recognition result dictionary.
- `explain_recognition_failure(exc)`: converts provider and local preprocessing errors into user-facing fallback messages.
- `recognition_status()`: reports provider, model, configuration presence, and last-attempt availability without returning credentials.

## Dependencies

- Flask application configuration and the application `AppError` type.
- The OpenAI Python SDK only as an OpenAI-compatible protocol client pointed at `https://api.deepseek.com`.
- `pypdfium2` and Pillow for in-memory PDF page rendering.

The Feature does not own HTTP routes, imported-file persistence, draft creation, or reimbursement calculations. Existing legacy API routes call this public surface to preserve their contracts.
