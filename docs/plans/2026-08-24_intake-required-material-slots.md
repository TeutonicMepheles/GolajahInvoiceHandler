# Intake Required Material Slots

Status: Implemented

Last verified: 2026-08-26

## Goal

Make every primary-document draft show the supporting materials it still needs,
and let the user fill each empty material slot directly from a file drop, file
picker, or clipboard paste without first creating and manually associating a
separate supporting draft.

## In scope

- Derive visible slots from each draft's authoritative `material.requirements`
  response and the currently associated attachment categories.
- Show empty required-material slots in both card and table intake views.
- Map the foreign-currency RMB payment requirement to a `payment_record` upload
  slot while preserving its more precise completeness status.
- Upload one dropped, picked, or pasted image/PDF directly to the owning primary
  draft through the existing version-protected attachment endpoint.
- Refresh after every upload so payment recognition and the resulting RMB amount
  can reveal a higher-tier purchase-list slot immediately.
- Add focused regression coverage and update the owning Feature and user docs.
- Make every attachment thumbnail prioritize its recognized/material category
  over technical file size, in both intake views and the preview dock.
- Separate slot selection for clipboard paste from the explicit file-picker
  action, and surface recognized RMB payment results after the list refreshes.
- Publish the equivalent Agent workflow through the existing MCP discovery
  descriptions: read `material.missing`, map the requirement to an attachment
  category, upload with the current entity versions, and re-read the item after
  every write because payment recognition can reveal another requirement.

## Out of scope

- Changing the configurable material rules, their 1,000 CNY boundary, or
  material category metadata.
- Guessing an attachment's target when several primary drafts or several slots
  are visible.
- Replacing the existing global import, supporting-draft association, category
  correction, or payment-recognition retry flows.
- Accepting clipboard text/HTML or file types outside the existing image/PDF
  intake allowlist.
- Changing any existing `/api` request or response contract.
- Adding another MCP tool or a parallel CLI command when the existing
  `get_invoice_item` and `add_invoice_attachment` tools already cover the flow.

## Acceptance gates

- A foreign Invoice/Receipt with no payment record displays a clearly labelled
  payment-record empty slot in both card and table modes.
- Dropping, choosing, or pasting a supported file into a slot uploads it directly
  to that main draft with the slot category and current item version.
- Clicking a slot selects it as the explicit clipboard target and shows a
  category-specific paste prompt without opening the file manager. Keyboard
  Enter/Space provides the same selection action.
- Only the slot's dedicated `选择文件` button opens the file manager; paste is
  accepted only for the currently selected visible slot unless the user is
  editing text.
- A payment-record upload completes recognition before the UI refreshes, then
  the refreshed table/card and feedback identify the RMB amount when available
  or clearly state that manual confirmation remains necessary.
- After a payment record confirms an RMB reimbursement amount of at least
  1,000 CNY, refresh replaces the filled payment slot with any newly missing
  purchase-list slot required by the configured rule.
- An attached payment record whose RMB amount still needs manual confirmation is
  not misrepresented as another empty payment-file slot.
- Existing supporting-draft drag association remains functional and external
  file drops on a slot are not interpreted as draft association.
- Thumbnail overlays and metadata identify the concrete category (`发票`,
  `Invoice`, `Receipt`, `支付记录`, `购入清单`, or `未知材料`) and do not show byte
  size; the preview dock pairs that category with the human-readable file format.
- MCP discovery explicitly documents `foreign_payment_rmb -> payment_record`,
  `payment_record -> payment_record`, and `purchase_list -> purchase_list`, and
  requires an authoritative `get_invoice_item` read after each attachment write.
- MCP user documentation includes a version-safe required-material recipe while
  preserving the exact 17-tool manifest and existing HTTP contracts.
- Full pytest, `node --check` for every JavaScript file under `web/`, and a real
  browser exercise of both views, file drop/paste, dynamic slot refresh, and
  Console output pass.

## Evidence

- `2026-08-26`: MCP discovery now publishes the existing version-safe material
  workflow without adding a tool or changing HTTP contracts. It maps
  `foreign_payment_rmb` and `payment_record` to the `payment_record` category,
  maps `purchase_list` directly, and requires `get_invoice_item` after every
  attachment write so the next call uses refreshed versions and requirements.
- `2026-08-26`: the Agent, document-intake, and architecture suites passed
  together (`34 passed`), and every JavaScript file under `web/` passed
  `node --check`. In the sanitized publication candidate, the full suite passed
  with `173 passed, 1 skipped`; the skipped real-client registration contract
  was also exercised under the normal system path and correctly failed closed
  because installed Codex `0.149.1` is outside the frozen validated `0.149.0`
  gate. No client-version allowlist was widened.
- `2026-08-24`: slot-body selection and file picking are separate actions.
  Click or Enter/Space selects one visible category-specific paste target and
  changes its inline prompt; only its dedicated `选择文件` button opens the file
  manager. Page paste without a selected slot is rejected with guidance.
- `2026-08-24`: payment upload feedback now waits until the authoritative draft
  list has refreshed. When recognition succeeds it names the exact RMB amount
  and confirms that list/material requirements have updated; failed amount
  recognition retains the manual-confirmation guidance.
- `2026-08-24`: `node --check` passed for every JavaScript file under `web/`;
  `\.venv\Scripts\python.exe -m pytest -q` completed with
  `173 passed in 34.38s`.
- `2026-08-24`: an isolated real-browser run selected the payment slot in both
  table and card modes without opening the file manager and showed
  `已选中，可按 Ctrl+V 粘贴支付记录`. Its dedicated picker uploaded a synthetic
  payment record; the row refreshed to `1200.00 CNY（原 175.00 USD）`, the
  payment slot became a purchase-list slot, and feedback showed the exact
  recognized amount. Selecting that new slot and pasting a clipboard image
  completed the materials. Browser Console warnings and errors remained empty.
  The service was stopped and fixtures were moved to the Windows Recycle Bin.

- `2026-08-24`: attachment information hierarchy was revised so thumbnail
  badges and metadata show the concrete material category, while byte size is
  absent from thumbnails and the preview dock. The dock now shows category and
  file format.
- `2026-08-24`: `node --check` passed for every JavaScript file under `web/`;
  `\.venv\Scripts\python.exe -m pytest -q` completed with
  `173 passed in 46.75s`.
- `2026-08-24`: an isolated real-browser run rendered a synthetic Receipt and
  payment record in table and card modes. Both category badges and both
  `材料类型：…` metadata labels were visible; opening the Receipt showed
  `Receipt · 图片` in the dock. No rendered `KB`/`MB` value and no Console warning
  or error were present. The service was stopped and fixtures were moved to the
  Windows Recycle Bin.

- `2026-08-24`: focused required-slot regressions passed, including the direct
  payment attachment transition from missing `foreign_payment_rmb` to missing
  `purchase_list` at 1,200 CNY and the shared card/table frontend contract.
- `2026-08-24`: `node --check` passed for every JavaScript file under `web/`.
- `2026-08-24`: `\.venv\Scripts\python.exe -m pytest -q` completed with
  `173 passed in 32.77s`.
- `2026-08-24`: an isolated real-browser run imported a foreign Invoice and
  displayed one payment-record empty slot in table mode. Pasting an image into
  the focused slot attached it directly, recognized 1,200 CNY, and refreshed
  the same row to one purchase-list empty slot. Card mode showed that same slot;
  its click/file-picker flow filled the purchase list, after which card and
  table modes both showed `当前所需附件已齐全`. Browser Console warnings and errors
  remained empty. The isolated service was stopped and its fixtures were moved
  to the Windows Recycle Bin.
