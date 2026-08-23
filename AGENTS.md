# InvoicesHandler engineering guidance

## Sources of truth

- Product behavior is defined by the current code, tests, `README.md`, and the matching active or implemented Plan under `docs/plans/`.
- `docs/architecture.md` owns cross-module dependency rules. Feature READMEs own only their local responsibility, public surface, and dependencies.
- Never put real API keys in source, browser code, logs, fixtures, or documentation. Recognition credentials remain backend environment variables.

## Feature workflow

1. Read `docs/README.md` and `docs/plans/README.md` before implementing a feature.
2. Create or update one owning Plan with observable acceptance gates and explicit out-of-scope items.
3. Prefer a vertical feature slice over adding behavior to the application shell or a shared utility module.
4. Update the owning feature README and Plan evidence when implementation or validation changes.

## Architecture boundaries

- Frontend dependency direction is `app -> features -> shared`. `shared` must not import feature or app modules. Features must not import another feature's private files.
- `web/app.js` is the composition shell. It owns navigation and temporary legacy screens, not new feature business logic.
- A frontend feature owns its rendering, interaction orchestration, and feature-local state. Reusable domain-independent helpers belong in `web/shared/` only after their boundary is stable.
- Backend HTTP parsing and response construction belong in feature Blueprints. Business calculations, storage, export, and persistence remain callable without an HTTP request context.
- Preserve existing `/api` contracts unless the owning Plan explicitly includes a contract migration.
- Do not introduce a frontend framework, bundler, package manager dependency, or global state library without an explicit Plan and migration/rollback boundary.

## Validation

- Backend and API regression: `.\.venv\Scripts\python.exe -m pytest -q`
- JavaScript syntax: run `node --check` for every `.js` file under `web/`.
- A frontend behavior change is not considered browser-validated until the affected flow is exercised in a real browser with no Console errors.
- Report only checks actually run. A static check is not browser or end-to-end evidence.
