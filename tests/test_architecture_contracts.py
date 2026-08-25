from __future__ import annotations

import re
from pathlib import Path


def _project_root(app) -> Path:
    return Path(app.config["BASE_DIR"])


def test_frontend_entry_uses_native_modules_and_feature_public_entry(app, client):
    root = _project_root(app)
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    shell = (root / "web" / "app.js").read_text(encoding="utf-8")
    feature = (root / "web" / "features" / "quotation" / "index.js").read_text(encoding="utf-8")
    review_feature = (root / "web" / "features" / "invoice-review" / "index.js").read_text(encoding="utf-8")
    intake_feature = (root / "web" / "features" / "document-intake" / "index.js").read_text(encoding="utf-8")
    batch_feature = (root / "web" / "features" / "batch-management" / "index.js").read_text(encoding="utf-8")
    review_readme = root / "web" / "features" / "invoice-review" / "README.md"
    intake_readme = root / "web" / "features" / "document-intake" / "README.md"
    batch_readme = root / "web" / "features" / "batch-management" / "README.md"

    assert '<script type="module" src="/static/app.js"></script>' in index
    assert 'from "./features/quotation/index.js"' in shell
    assert "function renderQuotation(" not in shell
    assert "export async function renderQuotation(" in feature
    assert 'from "./features/invoice-review/index.js"' in shell
    assert "export async function openInvoiceReview(" in review_feature
    assert 'from "./features/document-intake/index.js"' in shell
    assert re.search(
        r"export\s+(?:async\s+)?function\s+renderDocumentIntake\s*\(",
        intake_feature,
    )
    assert "function renderDraftItems(" not in shell
    assert "function draftCard(" not in shell
    assert "async function importFiles(" not in shell
    assert 'draggable="${canAssociate ? "true" : "false"}"' in intake_feature
    assert 'from "./features/batch-management/index.js"' in shell
    assert "export function renderBatchMergeAction(" in batch_feature
    assert "export function wireBatchMergeAction(" in batch_feature
    assert "export function openBatchReopenModal(" in batch_feature
    assert review_readme.is_file()
    assert intake_readme.is_file()
    assert batch_readme.is_file()
    assert "duplicate-review-sessions" not in shell

    for asset in (
        "/static/app.js",
        "/static/core/state.js",
        "/static/shared/api.js",
        "/static/shared/dom.js",
        "/static/shared/forms.js",
        "/static/shared/ui.js",
        "/static/features/document-intake/index.js",
        "/static/features/invoice-review/index.js",
        "/static/features/batch-management/index.js",
        "/static/features/quotation/index.js",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_batch_management_feature_owns_merge_and_general_history_reopen(app):
    root = _project_root(app)
    shell = (root / "web" / "app.js").read_text(encoding="utf-8")
    feature = (root / "web" / "features" / "batch-management" / "index.js").read_text(
        encoding="utf-8"
    )
    frontend_readme = root / "web" / "features" / "batch-management" / "README.md"
    backend_readme = (
        root / "invoice_assistant" / "features" / "batch_management" / "README.md"
    )
    legacy_api = (root / "invoice_assistant" / "api.py").read_text(encoding="utf-8")
    endpoints = {rule.rule: rule.endpoint for rule in app.url_map.iter_rules()}

    assert frontend_readme.is_file()
    assert backend_readme.is_file()
    assert (
        endpoints["/api/batch-management/merge"]
        == "batch_management_api.merge_reimbursement_batches_route"
    )
    assert '@api.post("/batch-management/merge")' not in legacy_api

    assert 'from "./features/batch-management/index.js"' in shell
    assert "function openReopenBatchModal(" not in shell
    assert "function openBatchReopenModal(" not in shell
    assert "function openBatchMergeModal(" not in shell
    assert 'api("/batch-management/merge"' not in shell
    assert "data-batch-merge-source" not in shell
    assert "batch-merge-archive-warning" not in shell

    history_card = shell.split("function historyCard", 1)[1].split(
        "function openDeleteHistoryModal", 1
    )[0]
    assert (
        '<button class="btn small" data-reopen-batch="${batch.id}">退回编辑</button>'
        in history_card
    )
    assert "foreignNeedsRepair ? `<button" not in history_card

    assert "export function openBatchMergeModal(" in feature
    assert 'api("/batch-management/merge"' in feature
    assert "data-batch-merge-source" in feature
    assert "batch-merge-archive-warning" in feature


def test_document_intake_every_file_role_has_clickable_thumbnails_in_both_views(app):
    root = _project_root(app)
    intake = (root / "web" / "features" / "document-intake" / "index.js").read_text(
        encoding="utf-8"
    )

    thumbnail = intake.split("function attachmentThumbnail", 1)[1].split(
        "function mainDuplicateMatches", 1
    )[0]
    main_card = intake.split("function mainDraftCard", 1)[1].split(
        "function supportingDraftCard", 1
    )[0]
    supporting_card = intake.split("function supportingDraftCard", 1)[1].split(
        "function issueBadge", 1
    )[0]
    main_table = intake.split("function mainDraftTable", 1)[1].split(
        "function supportingDraftTable", 1
    )[0]
    supporting_table = intake.split("function supportingDraftTable", 1)[1].split(
        "function cardLists", 1
    )[0]
    inline_preview = intake.split("async function openAttachmentPreview", 1)[1].split(
        "function wireThumbnailPreviews", 1
    )[0]
    preview_wiring = intake.split("function wireThumbnailPreviews", 1)[1].split(
        "function internalDrag", 1
    )[0]

    assert "attachment.thumbnail_url" in thumbnail
    assert 'attachment.mime_type === "application/pdf"' in thumbnail
    assert '<img src="${esc(thumbnailUrl)}"' in thumbnail
    assert '<button class="document-thumbnail-preview"' in thumbnail
    assert 'data-preview-attachment="${attachment.id}"' in thumbnail
    assert 'aria-controls="document-preview-dock"' in thumbnail
    assert "attachment.category_label" in thumbnail
    assert "材料类型：${esc(categoryLabel)}" in thumbnail
    assert "attachment.size_bytes" not in thumbnail
    assert "fmtFileSize" not in intake
    assert '<a class="document-thumbnail-preview"' not in thumbnail
    assert 'target="_blank"' not in thumbnail

    # Main cards render the primary file and already-associated attachments;
    # supporting cards render every still-unassociated attachment.
    assert main_card.count("attachmentThumbnail(") >= 2
    assert "attachmentThumbnail(" in supporting_card

    # Table mode must render the same attachment collection through the shared
    # thumbnail control; filenames or attachment counts alone are insufficient.
    for table_renderer in (main_table, supporting_table):
        assert "(item.attachments || []).map((attachment) => attachmentThumbnail(" in table_renderer

    assert "attachment.preview_url" in inline_preview
    assert '`${categoryLabel} · ${fileFormat}`' in inline_preview
    assert "attachment.size_bytes" not in inline_preview
    assert "attachment.download_url" not in inline_preview
    assert 'fetch(previewUrl, { method: "HEAD", cache: "no-store" })' in inline_preview
    assert '<iframe class="document-preview-frame"' in inline_preview
    assert '<img class="document-preview-image"' in inline_preview
    assert "data-inline-preview" in inline_preview
    assert "window.open" not in inline_preview
    assert "target=\"_blank\"" not in inline_preview
    assert 'id="document-preview-dock"' in intake
    assert 'documentIntakeCapability("document_intake_association")' in intake
    assert 'error.code === "not_found"' in intake
    assert "$$('[data-preview-attachment]'" in preview_wiring
    assert "openAttachmentPreview(root, attachment)" in preview_wiring


def test_document_intake_required_material_slots_share_dynamic_direct_upload_flow(app):
    intake = (
        _project_root(app) / "web" / "features" / "document-intake" / "index.js"
    ).read_text(encoding="utf-8")
    slot_derivation = intake.split("function missingMaterialSlots", 1)[1].split(
        "function pendingMaterialMessages", 1
    )[0]
    card_renderer = intake.split("function mainDraftCard", 1)[1].split(
        "function supportingDraftCard", 1
    )[0]
    table_renderer = intake.split("function mainDraftTable", 1)[1].split(
        "function supportingDraftTable", 1
    )[0]

    assert "item.material?.requirements" in slot_derivation
    assert 'foreign_payment_rmb: "payment_record"' in intake
    assert "attachedCategories.has(category)" in slot_derivation
    assert "requiredMaterialPanel(item)" in card_renderer
    assert 'requiredMaterialPanel(item, "table")' in table_renderer
    assert 'data-material-slot-category="${esc(slot.category)}"' in intake
    assert 'data-material-slot-picker' in intake
    assert '>选择文件</button>' in intake
    assert "function selectMaterialSlot(" in intake
    assert 'slot.classList.toggle("selected", selected)' in intake
    assert "已选中，可按 Ctrl+V 粘贴" in intake
    assert 'form.append("category", category)' in intake
    assert "api(`/items/${item.id}/attachments`" in intake
    assert "function recognizedPaymentRmb(" in intake
    assert "列表和材料要求已更新" in intake
    assert "clipboardData?.items" in intake
    assert 'slot.addEventListener("paste"' in intake
    assert 'document.addEventListener("paste"' in intake
    assert 'state.page !== "intake"' in intake
    assert "visibleSlots.find(" in intake
    assert "请先点击要粘贴的材料空槽" in intake
    assert "isTextEditingTarget(event.target)" in intake

    upload_flow = intake.split("const uploadMaterialFile", 1)[1].split(
        "const materialSlots", 1
    )[0]
    assert upload_flow.index("await refresh()") < upload_flow.index(
        "列表和材料要求已更新"
    )


def test_shared_frontend_modules_do_not_depend_on_app_or_features(app):
    shared = _project_root(app) / "web" / "shared"
    for module in shared.glob("*.js"):
        source = module.read_text(encoding="utf-8")
        assert "features/" not in source, module.name
        assert "app.js" not in source, module.name


def test_invoice_review_frontend_owns_tokens_and_versioned_mutations(app):
    root = _project_root(app)
    shell = (root / "web" / "app.js").read_text(encoding="utf-8")
    state = (root / "web" / "core" / "state.js").read_text(encoding="utf-8")
    review = (root / "web" / "features" / "invoice-review" / "index.js").read_text(encoding="utf-8")

    assert "state.matches" not in shell
    assert "review_token" not in state
    assert "overflow_review_token" not in state
    assert "/duplicate-review-sessions" in review
    assert "source_review_token" in review
    assert "overflow_review_token" in review
    assert '"review_required"' in review
    assert "data-review-selected-target" in review
    assert "当前合并目标" in review
    assert "expected_item_version" in shell
    assert "expected_requirements_version" in shell
    assert "confirmation_name" in shell
    assert "item_id: item.id, expected_version: item.version" in shell
    assert 'api("/drafts")' in shell
    assert 'api("/batches?status=draft")' in shell

    patch_position = review.index('method: "PATCH"')
    fresh_detail_position = review.index("detail = await loadCurrentItem")
    assert patch_position < fresh_detail_position
    confirm_handler = review.split("confirmButton.onclick", 1)[1].split("mergeButton.onclick", 1)[0]
    for financial_field in (
        "merchant:",
        "expense_date:",
        "amount:",
        "currency:",
        "converted_amount:",
        "project_id:",
        "purpose:",
    ):
        assert financial_field not in confirm_handler


def test_quotation_http_contract_is_owned_by_feature_blueprint(app):
    endpoints = {rule.rule: rule.endpoint for rule in app.url_map.iter_rules()}

    assert endpoints["/api/quotations/calculate"] == "quotation_api.quotation_calculate"
    assert endpoints["/api/quotations/export"] == "quotation_api.quotation_export"


def test_document_intake_http_contract_is_owned_by_feature_blueprint(app):
    root = _project_root(app)
    readme = root / "invoice_assistant" / "features" / "document_intake" / "README.md"
    endpoints = {rule.rule: rule.endpoint for rule in app.url_map.iter_rules()}

    assert readme.is_file()
    assert (
        endpoints["/api/document-intake/associations"]
        == "document_intake_api.create_document_association"
    )
    assert (
        endpoints["/api/attachments/<int:attachment_id>/thumbnail"]
        == "document_intake_api.attachment_thumbnail"
    )
    assert (
        endpoints["/api/attachments/<int:attachment_id>/preview"]
        == "document_intake_api.attachment_preview"
    )

    intake_source = (
        root / "web" / "features" / "document-intake" / "index.js"
    ).read_text(encoding="utf-8")
    assert 'api("/document-intake/associations"' in intake_source
    assert 'api("/api/document-intake/associations"' not in intake_source


def test_recognition_business_logic_is_owned_by_feature(app):
    root = _project_root(app)
    api_source = (root / "invoice_assistant" / "api.py").read_text(encoding="utf-8")
    service = root / "invoice_assistant" / "features" / "recognition" / "service.py"
    readme = service.parent / "README.md"
    shell = (root / "web" / "app.js").read_text(encoding="utf-8")

    assert service.is_file()
    assert readme.is_file()
    assert "from .features.recognition import" in api_source
    assert "OPENAI_API_KEY" not in api_source
    assert "DeepSeek 识别" in shell
    assert "OpenAI 识别" not in shell
