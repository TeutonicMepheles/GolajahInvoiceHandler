import { state } from "../../core/state.js";
import { api } from "../../shared/api.js";
import { $, $$, esc } from "../../shared/dom.js";
import { busy, emptyState, toast } from "../../shared/ui.js";

const INTERNAL_DND_MIME = "application/x-invoice-assistant-supporting-draft";
const SERVICE_RESTART_MESSAGE = "本地服务仍在运行旧版本，请重启本地应用后刷新页面再关联附件。";
const MATERIAL_SLOT_CATEGORY = {
  foreign_payment_rmb: "payment_record",
  payment_record: "payment_record",
  purchase_list: "purchase_list",
};
const SUPPORTED_UPLOAD_EXTENSIONS = /\.(pdf|png|jpe?g|webp)$/i;
const CLIPBOARD_EXTENSION = {
  "application/pdf": "pdf",
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
};
let activePreviewAttachmentId = null;
let previewRequestSequence = 0;
let activeMaterialPasteHandler = null;
let activeMaterialSlotKey = null;

document.addEventListener("paste", (event) => activeMaterialPasteHandler?.(event));

const fmtDate = (value) => value
  ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" })
    .format(new Date(`${String(value).slice(0, 10)}T00:00:00`))
  : "—";

const fmtTime = (value) => value
  ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    .format(new Date(value))
  : "—";

const fmtMoney = (amount, currency = "CNY") => `${Number(amount || 0).toFixed(2)} ${esc(currency || "CNY")}`;

function fmtItemAmount(item) {
  if (item.currency === "CNY") return fmtMoney(item.amount, "CNY");
  const reimbursed = item.reimbursement_amount == null
    ? "人民币实付待识别"
    : fmtMoney(item.reimbursement_amount, "CNY");
  return `${reimbursed}（原 ${fmtMoney(item.amount, item.currency)}）`;
}

function primaryCategoryCodes() {
  return new Set(
    (state.bootstrap?.categories || [])
      .filter((category) => category.material === "primary_receipt")
      .map((category) => category.code),
  );
}

function isPrimaryAttachment(attachment, primaryCodes) {
  return primaryCodes.has(attachment?.category);
}

function isMainDraft(item, primaryCodes) {
  const attachments = item.attachments || [];
  return attachments.length === 0 || attachments.some((attachment) => isPrimaryAttachment(attachment, primaryCodes));
}

function isPrimaryTarget(item, primaryCodes) {
  return (item.attachments || []).some((attachment) => isPrimaryAttachment(attachment, primaryCodes));
}

function projectOptions(selected) {
  return (state.bootstrap?.projects || [])
    .filter((project) => project.enabled || project.id === Number(selected))
    .map((project) => `<option value="${project.id}" ${project.id === Number(selected) ? "selected" : ""}>${esc(project.name)}${project.code ? ` · ${esc(project.code)}` : ""}${project.enabled ? "" : "（已停用）"}</option>`)
    .join("");
}

function categoryOptions(selected) {
  const categories = state.bootstrap?.categories || [];
  const primary = categories.filter((category) => category.material === "primary_receipt");
  const supporting = categories.filter((category) => category.material !== "primary_receipt");
  const options = (entries) => entries
    .map((category) => `<option value="${esc(category.code)}" ${selected === category.code ? "selected" : ""}>${esc(category.label)}</option>`)
    .join("");
  return `<optgroup label="主凭据">${options(primary)}</optgroup><optgroup label="附件材料">${options(supporting)}</optgroup>`;
}

function convertedAmountField(item = {}) {
  return `<div class="field"><label>人民币实际付款金额${item.currency === "CNY" ? "（外币时填写）" : ""}</label><input name="converted_amount" type="number" min="0" max="100000000000" step="0.01" value="${item.converted_amount ?? ""}" placeholder="优先从支付记录自动识别"></div>`;
}

function safeAttachmentUrl(value) {
  return typeof value === "string" && value.startsWith("/api/attachments/") ? value : "";
}

function documentIntakeCapability(name) {
  return state.bootstrap?.capabilities?.[name] === true;
}

function attachmentThumbnail(attachment, primaryCodes, compact = false, surface = "card") {
  const thumbnailUrl = safeAttachmentUrl(attachment.thumbnail_url);
  const isPdf = attachment.mime_type === "application/pdf";
  const isTable = surface === "table";
  const categoryLabel = attachment.category_label || "未知材料";
  const isActive = Number(activePreviewAttachmentId) === Number(attachment.id);
  return `<div class="document-thumbnail ${compact ? "compact" : ""} ${isTable ? "table" : ""} ${isActive ? "is-preview-active" : ""}" data-attachment-tile="${attachment.id}">
    <button class="document-thumbnail-preview" type="button" data-preview-attachment="${attachment.id}" aria-label="在右侧预览${esc(categoryLabel)} ${esc(attachment.original_name)}" aria-controls="document-preview-dock" aria-expanded="${isActive ? "true" : "false"}">
      ${thumbnailUrl ? `<img src="${esc(thumbnailUrl)}" alt="${esc(attachment.original_name)} 的缩略图" loading="lazy" data-thumbnail-image="${attachment.id}">` : ""}
      <span class="document-thumbnail-fallback" ${thumbnailUrl ? "hidden" : ""} data-thumbnail-fallback="${attachment.id}"><b>${isPdf ? "PDF" : "文件"}</b><small>点击预览</small></span>
      <span class="document-thumbnail-category ${isPrimaryAttachment(attachment, primaryCodes) ? "primary" : ""}">${esc(categoryLabel)}</span>
    </button>
    <div class="document-thumbnail-meta">
      <strong title="${esc(attachment.original_name)}">${esc(attachment.original_name)}</strong>
      <small class="document-thumbnail-type">材料类型：${esc(categoryLabel)}</small>
      ${isTable ? "" : `<select data-attachment-category="${attachment.id}" aria-label="${esc(attachment.original_name)} 的材料类型">${categoryOptions(attachment.category)}</select>`}
    </div>
  </div>`;
}

function mainDuplicateMatches(item, primaryCodes) {
  return (item.matches || []).filter((match) => isMainDraft(match.item || {}, primaryCodes));
}

function duplicateMatches(item, primaryCodes) {
  const matches = mainDuplicateMatches(item, primaryCodes);
  if (!matches.length) return "";
  return `<div class="match-box"><strong>发现可能的已有条目，请先确认是否重复报销</strong>${matches.map((match) => {
    const candidate = match.item || {};
    const targetId = Number(candidate.id);
    const action = match.historical
      ? '<span class="badge warn">历史记录不可合并</span>'
      : `<button class="btn small" type="button" data-review-merge-source="${item.id}" data-review-target="${targetId}">合并为附件</button>`;
    return `<div class="match-row"><span>${match.exact_file ? "同一文件 · " : ""}${esc(candidate.merchant)} · ${fmtDate(candidate.expense_date)} · ${fmtItemAmount(candidate)} · ${esc(candidate.status_label)} · 匹配 ${Math.round(Number(match.confidence || 0) * 100)}%</span>${action}</div>`;
  }).join("")}</div>`;
}

function associationTargetOptions(targets) {
  return targets.map((target) => `<option value="${target.id}">#${target.id} · ${esc(target.merchant || "未命名主凭据")} · ${fmtDate(target.expense_date)}</option>`).join("");
}

function missingMaterialSlots(item) {
  const attachedCategories = new Set((item.attachments || []).map((attachment) => attachment.category));
  return (item.material?.requirements || []).flatMap((requirement) => {
    if (requirement.satisfied) return [];
    const category = MATERIAL_SLOT_CATEGORY[requirement.code];
    if (!category || attachedCategories.has(category)) return [];
    const label = requirement.code === "foreign_payment_rmb" ? "支付记录" : requirement.label;
    return [{ code: requirement.code, category, label }];
  });
}

function pendingMaterialMessages(item) {
  const attachedCategories = new Set((item.attachments || []).map((attachment) => attachment.category));
  return (item.material?.requirements || []).flatMap((requirement) => {
    if (requirement.satisfied) return [];
    if (requirement.code === "foreign_payment_rmb" && attachedCategories.has("payment_record")) {
      return ["支付记录已附带，人民币实付金额仍待确认"];
    }
    if (!MATERIAL_SLOT_CATEGORY[requirement.code]) return [`${requirement.label}仍待补充`];
    return [];
  });
}

function materialSlot(item, slot, surface = "card") {
  const compact = surface === "table" ? " compact" : "";
  const inputId = `material-slot-file-${surface}-${item.id}-${slot.code}`;
  const slotKey = `${item.id}:${slot.category}`;
  const selected = activeMaterialSlotKey === slotKey;
  return `<div class="material-upload-slot${compact} ${selected ? "selected" : ""}" tabindex="0" role="group" data-material-slot data-material-slot-key="${esc(slotKey)}" data-material-slot-item="${item.id}" data-material-slot-category="${esc(slot.category)}" data-material-slot-label="${esc(slot.label)}" aria-label="${selected ? "已选中" : "选择"}主凭据 #${item.id} 的${esc(slot.label)}粘贴槽">
    <span class="material-slot-icon" aria-hidden="true">${selected ? "✓" : "＋"}</span>
    <span class="material-slot-copy"><strong>${esc(slot.label)}空槽</strong><small data-material-slot-instruction>${selected ? `已选中，可按 Ctrl+V 粘贴${esc(slot.label)}` : "点击槽位选中粘贴目标，也可直接拖入文件"}</small></span>
    <button class="btn small material-slot-picker" type="button" data-material-slot-picker aria-label="为主凭据 #${item.id} 的${esc(slot.label)}选择文件">选择文件</button>
    <input id="${inputId}" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" data-material-slot-input hidden>
  </div>`;
}

function requiredMaterialPanel(item, surface = "card") {
  const slots = missingMaterialSlots(item);
  const pending = pendingMaterialMessages(item);
  const content = slots.length
    ? slots.map((slot) => materialSlot(item, slot, surface)).join("")
    : pending.length
      ? pending.map((message) => `<div class="material-slot-status pending"><span>…</span><small>${esc(message)}</small></div>`).join("")
      : '<div class="material-slot-status complete"><span>✓</span><small>当前所需附件已齐全</small></div>';
  if (surface === "table") return `<div class="table-material-slots">${content}</div>`;
  return `<section class="required-materials"><div class="required-materials-heading"><strong>尚需附带</strong><span>${slots.length ? `${slots.length} 个附件空槽` : pending.length ? "等待金额确认" : "材料已齐"}</span></div><div class="required-material-grid">${content}</div></section>`;
}

function mainDraftCard(item, primaryCodes) {
  const isTarget = isPrimaryTarget(item, primaryCodes);
  const primaryAttachments = (item.attachments || []).filter((attachment) => isPrimaryAttachment(attachment, primaryCodes));
  const supportingAttachments = (item.attachments || []).filter((attachment) => !isPrimaryAttachment(attachment, primaryCodes));
  const recognitionError = item.recognition_error ? `<div class="notice error">${esc(item.recognition_error)}</div>` : "";
  const uncertainty = item.uncertainties?.length
    ? `<div class="notice warn">待核对：${item.uncertainties.map(esc).join("；")}</div>`
    : "";
  const primaryMedia = primaryAttachments.length
    ? `<div class="document-media-gallery">${primaryAttachments.map((attachment) => attachmentThumbnail(attachment, primaryCodes)).join("")}</div>`
    : '<div class="document-manual-placeholder"><span>✎</span><strong>手工录入条目</strong><small>没有原始主凭据，可继续补全字段并核对</small></div>';
  const associatedMedia = supportingAttachments.length
    ? `<section class="document-associated-materials"><div class="document-associated-heading"><strong>已关联附件</strong><span>${supportingAttachments.length} 份补充材料</span></div><div class="document-associated-grid">${supportingAttachments.map((attachment) => attachmentThumbnail(attachment, primaryCodes, true)).join("")}</div></section>`
    : "";
  return `<article class="card draft-card document-main-card ${isTarget ? "is-association-target" : "is-manual"}" data-main-draft-card="${item.id}" ${isTarget ? `data-association-target="${item.id}"` : ""}>
    ${isTarget ? '<div class="association-drop-overlay" aria-hidden="true"><strong>放开以关联附件</strong><small>附件将归入这份主凭据</small></div>' : ""}
    <div class="draft-head"><div class="draft-select-title"><input type="checkbox" data-select-draft="${item.id}" ${state.selectedDrafts.has(item.id) ? "checked" : ""} aria-label="选择草稿 #${item.id}"><div><strong>${isTarget ? "主凭据" : "手工录入"} #${item.id}</strong><small>创建于 ${fmtTime(item.created_at)}</small></div></div><span class="badge ${isTarget ? "info" : "muted"}">${isTarget ? "待核对" : "无文件"}</span></div>
    <div class="draft-body">${recognitionError}${uncertainty}${duplicateMatches(item, primaryCodes)}${primaryMedia}${associatedMedia}${requiredMaterialPanel(item)}
      <div class="form-grid document-fields">
        <div class="field"><label>商户 / 收款方</label><input name="merchant" maxlength="200" value="${esc(item.merchant)}"></div>
        <div class="field"><label>消费日期</label><input name="expense_date" type="date" value="${esc(item.expense_date)}"></div>
        <div class="field"><label>金额</label><input name="amount" type="number" min="0" step="0.01" value="${Number(item.amount || 0)}"></div>
        <div class="field"><label>币种</label><input name="currency" maxlength="8" value="${esc(item.currency || "CNY")}"></div>
        ${convertedAmountField(item)}
        <div class="field full"><label>报销项目</label><select name="project_id">${projectOptions(item.project_id)}</select></div>
        <div class="field full"><label>购买内容 / 用途摘要</label><textarea name="purpose" maxlength="2000">${esc(item.purpose)}</textarea></div>
      </div>
      <div class="card-actions"><button class="btn" type="button" data-edit-main="${item.id}">详细编辑</button><button class="btn danger" type="button" data-delete-draft="${item.id}">删除草稿</button><button class="btn primary" type="button" data-review-draft="${item.id}">核对并确认</button></div>
    </div>
  </article>`;
}

function supportingDraftCard(item, primaryCodes, targets, associationAvailable) {
  const canAssociate = associationAvailable && targets.length > 0;
  const fileCount = (item.attachments || []).length;
  const help = !associationAvailable
    ? SERVICE_RESTART_MESSAGE
    : canAssociate
      ? "拖到上方主凭据卡，或从下拉框选择归属。"
      : "请先导入主凭据，或将这份材料的类型改为发票、Invoice 或 Receipt。";
  return `<article class="card supporting-draft-card" data-supporting-draft="${item.id}" draggable="${canAssociate ? "true" : "false"}">
    <div class="supporting-draft-head"><label class="draft-select-title"><input type="checkbox" data-select-draft="${item.id}" ${state.selectedDrafts.has(item.id) ? "checked" : ""} aria-label="选择附件草稿 #${item.id}"><span><strong>待关联附件 #${item.id}</strong><small>${fileCount} 份文件 · ${fmtTime(item.created_at)}</small></span></label><span class="badge warn">非主凭据</span></div>
    <div class="supporting-media-grid">${(item.attachments || []).map((attachment) => attachmentThumbnail(attachment, primaryCodes, true)).join("")}</div>
    ${item.recognition_error ? `<div class="notice error">${esc(item.recognition_error)}</div>` : ""}
    <p class="association-help ${associationAvailable ? "" : "service-restart-required"}">${help}</p>
    <div class="association-controls"><select data-association-select="${item.id}" ${canAssociate ? "" : "disabled"} aria-label="选择要关联的主凭据"><option value="">选择主凭据…</option>${associationTargetOptions(targets)}</select><button class="btn primary small" type="button" data-associate-source="${item.id}" ${canAssociate ? "" : "disabled"}>关联</button><button class="btn danger small" type="button" data-delete-draft="${item.id}">删除</button></div>
  </article>`;
}

function issueBadge(item, primaryCodes) {
  if (item.recognition_error) return '<span class="badge danger">识别失败</span>';
  if (item.uncertainties?.length) return `<span class="badge warn">${item.uncertainties.length} 项待核对</span>`;
  if (mainDuplicateMatches(item, primaryCodes).length) return '<span class="badge info">可能重复</span>';
  return '<span class="badge ok">字段已预填</span>';
}

function mainDraftTable(items, primaryCodes) {
  if (!items.length) return emptyState("票", "尚无主凭据", "导入发票、Invoice 或 Receipt；识别不准确时也可在附件区修正类型。");
  return `<div class="card table-card"><div class="table-scroll"><table class="data draft-table document-main-table"><thead><tr><th>选择</th><th>主条目</th><th>创建时间</th><th>商户 / 用途</th><th>日期</th><th>金额</th><th>项目</th><th>全部文件预览</th><th>尚需附带</th><th>核对状态</th><th>操作</th></tr></thead><tbody>${items.map((item) => {
    const primaryCount = (item.attachments || []).filter((attachment) => isPrimaryAttachment(attachment, primaryCodes)).length;
    const supportingCount = (item.attachments || []).length - primaryCount;
    const target = isPrimaryTarget(item, primaryCodes);
    const files = (item.attachments || []).map((attachment) => attachmentThumbnail(attachment, primaryCodes, true, "table")).join("");
    const filePreviews = files
      ? `<div class="table-thumbnail-list" aria-label="草稿 #${item.id} 的全部文件">${files}</div>`
      : '<span class="table-sub">手工条目，无原始文件</span>';
    return `<tr class="document-main-row ${target ? "is-association-target" : ""}" ${target ? `data-association-target="${item.id}"` : ""}><td><input type="checkbox" data-select-draft="${item.id}" ${state.selectedDrafts.has(item.id) ? "checked" : ""} aria-label="选择草稿 #${item.id}"></td><td><span class="table-main">#${item.id} · ${target ? "主凭据" : "手工条目"}</span><span class="table-sub">${primaryCount} 份主文件${supportingCount ? ` · ${supportingCount} 份附件` : ""}</span></td><td>${fmtTime(item.created_at)}</td><td><span class="table-main">${esc(item.merchant || "待填写")}</span><span class="table-sub">${esc(item.purpose || "未填写用途")}</span></td><td>${fmtDate(item.expense_date)}</td><td><span class="table-main">${fmtItemAmount(item)}</span></td><td>${esc(item.project_name || "—")}</td><td class="table-preview-cell">${filePreviews}</td><td>${requiredMaterialPanel(item, "table")}</td><td>${issueBadge(item, primaryCodes)}</td><td><div class="table-actions"><button class="btn small" type="button" data-edit-main="${item.id}">编辑</button><button class="btn primary small" type="button" data-review-draft="${item.id}">确认</button><button class="btn danger small" type="button" data-delete-draft="${item.id}">删除</button></div></td></tr>`;
  }).join("")}</tbody></table></div></div>`;
}

function supportingDraftTable(items, primaryCodes, targets, associationAvailable) {
  if (!items.length) return '<div class="notice success document-supporting-empty">当前没有待关联附件。</div>';
  return `<div class="card table-card"><div class="table-scroll"><table class="data document-supporting-table"><thead><tr><th>选择</th><th>附件预览</th><th>材料类型</th><th>导入时间</th><th>关联到主凭据</th><th>操作</th></tr></thead><tbody>${items.map((item) => {
    const files = (item.attachments || []).map((attachment) => attachmentThumbnail(attachment, primaryCodes, true, "table")).join("");
    const categories = (item.attachments || []).map((attachment) => `<select data-attachment-category="${attachment.id}" aria-label="${esc(attachment.original_name)} 的材料类型">${categoryOptions(attachment.category)}</select>`).join("");
    const canAssociate = associationAvailable && targets.length > 0;
    return `<tr class="document-supporting-row" data-supporting-draft="${item.id}" draggable="${canAssociate ? "true" : "false"}"><td><input type="checkbox" data-select-draft="${item.id}" ${state.selectedDrafts.has(item.id) ? "checked" : ""} aria-label="选择附件草稿 #${item.id}"></td><td class="table-preview-cell"><span class="table-main">附件 #${item.id}</span><div class="table-thumbnail-list">${files}</div></td><td><div class="table-category-list">${categories}</div></td><td>${fmtTime(item.created_at)}</td><td><select data-association-select="${item.id}" ${canAssociate ? "" : "disabled"}><option value="">选择主凭据…</option>${associationTargetOptions(targets)}</select></td><td><div class="table-actions"><button class="btn primary small" type="button" data-associate-source="${item.id}" ${canAssociate ? "" : "disabled"}>关联</button><button class="btn danger small" type="button" data-delete-draft="${item.id}">删除</button></div></td></tr>`;
  }).join("")}</tbody></table></div></div>`;
}

function cardLists(mainItems, supportingItems, primaryCodes, targets, associationAvailable) {
  const main = mainItems.length
    ? `<div class="document-main-grid">${mainItems.map((item) => mainDraftCard(item, primaryCodes)).join("")}</div>`
    : emptyState("票", "尚无主凭据", "导入发票、Invoice 或 Receipt；识别不准确时也可在附件区修正类型。");
  const supporting = supportingItems.length
    ? `<div class="document-supporting-grid">${supportingItems.map((item) => supportingDraftCard(item, primaryCodes, targets, associationAvailable)).join("")}</div>`
    : '<div class="notice success document-supporting-empty">当前没有待关联附件。</div>';
  return { main, supporting };
}

function draftChanges(itemId, root) {
  const card = $(`[data-main-draft-card="${itemId}"]`, root);
  if (!card) return null;
  return {
    merchant: $('[name="merchant"]', card)?.value || "",
    expense_date: $('[name="expense_date"]', card)?.value || "",
    amount: $('[name="amount"]', card)?.value || "",
    currency: $('[name="currency"]', card)?.value || "CNY",
    converted_amount: $('[name="converted_amount"]', card)?.value || null,
    project_id: $('[name="project_id"]', card)?.value || null,
    purpose: $('[name="purpose"]', card)?.value || "",
  };
}

function itemPreconditions(item) {
  const payload = { expected_version: item.version };
  if (item.batch_ref) payload.expected_batch_version = item.batch_ref.batch_version;
  return payload;
}

async function reportMutationFailure(callback, error, refresh) {
  if (typeof callback !== "function") {
    toast(error.message, "error");
    return;
  }
  try {
    await callback(error, { refresh });
  } catch (callbackError) {
    toast(callbackError.message, "error");
  }
}

function updateSelectionUi(root, items) {
  const selected = items.filter((item) => state.selectedDrafts.has(item.id)).length;
  const count = $("#draft-selection-count", root);
  const remove = $("#delete-selected-drafts", root);
  const selectAll = $("#select-all-drafts", root);
  if (count) count.textContent = String(selected);
  if (remove) remove.disabled = selected === 0;
  if (selectAll) {
    selectAll.checked = items.length > 0 && selected === items.length;
    selectAll.indeterminate = selected > 0 && selected < items.length;
  }
}

function updatePreviewSelection(root, attachmentId) {
  $$('[data-preview-attachment]', root).forEach((button) => {
    const selected = Number(button.dataset.previewAttachment) === Number(attachmentId);
    button.setAttribute("aria-expanded", selected ? "true" : "false");
    button.closest("[data-attachment-tile]")?.classList.toggle("is-preview-active", selected);
  });
}

function renderPreviewError(root, message) {
  const body = $("[data-document-preview-body]", root);
  if (!body) return;
  body.innerHTML = `<div class="document-preview-message error" role="alert"><span>!</span><strong>无法在线预览</strong><p>${esc(message)}</p></div>`;
}

function closeAttachmentPreview(root, { restoreFocus = true } = {}) {
  const closingId = activePreviewAttachmentId;
  activePreviewAttachmentId = null;
  previewRequestSequence += 1;
  const layout = $(".document-intake-layout", root);
  const dock = $("#document-preview-dock", root);
  layout?.classList.remove("preview-open");
  if (dock) dock.hidden = true;
  updatePreviewSelection(root, null);
  if (restoreFocus && closingId != null) {
    requestAnimationFrame(() => {
      $(`[data-preview-attachment="${closingId}"]`, root)?.focus();
    });
  }
}

async function openAttachmentPreview(root, attachment) {
  const previewUrl = safeAttachmentUrl(attachment.preview_url);
  const dock = $("#document-preview-dock", root);
  const layout = $(".document-intake-layout", root);
  const body = $("[data-document-preview-body]", root);
  if (!dock || !layout || !body) {
    toast("该文件暂时没有可用预览。", "error");
    return;
  }

  activePreviewAttachmentId = Number(attachment.id);
  const requestSequence = ++previewRequestSequence;
  layout.classList.add("preview-open");
  dock.hidden = false;
  $("[data-document-preview-name]", dock).textContent = attachment.original_name || "文件预览";
  const categoryLabel = attachment.category_label || "未知材料";
  const fileFormat = attachment.mime_type === "application/pdf" ? "PDF" : "图片";
  $("[data-document-preview-meta]", dock).textContent = `${categoryLabel} · ${fileFormat}`;
  body.innerHTML = '<div class="document-preview-message loading" role="status"><span class="document-preview-spinner" aria-hidden="true"></span><strong>正在加载在线预览…</strong><p>文件不会被下载到本地。</p></div>';
  updatePreviewSelection(root, attachment.id);

  if (!documentIntakeCapability("inline_attachment_preview")) {
    renderPreviewError(root, "预览接口尚未载入，请重启本地应用并刷新页面。文件不会被删除或修改。");
    return;
  }
  if (!previewUrl) {
    renderPreviewError(root, "该文件暂时没有可用的在线预览地址，请刷新页面后重试。");
    return;
  }

  try {
    const response = await fetch(previewUrl, { method: "HEAD", cache: "no-store" });
    if (requestSequence !== previewRequestSequence || Number(activePreviewAttachmentId) !== Number(attachment.id)) return;
    if (!response.ok) {
      const message = response.status === 404
        ? "预览接口或文件尚未载入，请重启本地应用并刷新页面。"
        : `服务返回 ${response.status}，请稍后重试。`;
      renderPreviewError(root, message);
      return;
    }

    const responseMime = (response.headers.get("content-type") || "").split(";", 1)[0];
    const mimeType = responseMime || attachment.mime_type || "";
    const isPdf = mimeType === "application/pdf";
    const isImage = mimeType.startsWith("image/");
    if (!isPdf && !isImage) {
      renderPreviewError(root, "当前文件格式不支持在线阅读。");
      return;
    }

    const media = isPdf
      ? `<iframe class="document-preview-frame" src="${esc(`${previewUrl}#toolbar=1&navpanes=0&view=FitH`)}" title="${esc(attachment.original_name || "PDF 文件")} 在线预览" data-inline-preview></iframe>`
      : `<div class="document-preview-image-scroll"><img class="document-preview-image" src="${esc(previewUrl)}" alt="${esc(attachment.original_name || "图片文件")}" data-inline-preview></div>`;
    body.innerHTML = `<div class="document-preview-stage"><div class="document-preview-message loading" data-preview-loading role="status"><span class="document-preview-spinner" aria-hidden="true"></span><strong>正在打开${isPdf ? " PDF" : "图片"}…</strong></div>${media}</div>`;
    const preview = $("[data-inline-preview]", body);
    const stage = $(".document-preview-stage", body);
    const markReady = () => {
      if (requestSequence !== previewRequestSequence) return;
      stage?.classList.add("is-ready");
      const loading = $("[data-preview-loading]", stage);
      if (loading) loading.hidden = true;
    };
    preview.addEventListener("load", markReady, { once: true });
    if (preview.tagName === "IMG") {
      preview.addEventListener("error", () => {
        if (requestSequence === previewRequestSequence) renderPreviewError(root, "图片内容加载失败，请稍后重试。");
      }, { once: true });
      if (preview.complete && preview.naturalWidth > 0) markReady();
    }
  } catch {
    if (requestSequence === previewRequestSequence) {
      renderPreviewError(root, "无法连接本地预览服务，请确认应用正在运行后重试。");
    }
  }
}

function wireThumbnailPreviews(root, attachments) {
  const byId = new Map(attachments.map((attachment) => [Number(attachment.id), attachment]));
  $$('[data-thumbnail-image]', root).forEach((image) => {
    image.addEventListener("error", () => {
      image.hidden = true;
      const fallback = $(`[data-thumbnail-fallback="${image.dataset.thumbnailImage}"]`, image.closest("[data-attachment-tile]"));
      if (fallback) fallback.hidden = false;
      image.closest("[data-attachment-tile]")?.classList.add("thumbnail-unavailable");
    }, { once: true });
  });
  $$('[data-preview-attachment]', root).forEach((button) => {
    button.onclick = () => {
      const attachment = byId.get(Number(button.dataset.previewAttachment));
      if (attachment) openAttachmentPreview(root, attachment);
    };
  });
}

function internalDrag(event) {
  return [...(event.dataTransfer?.types || [])].includes(INTERNAL_DND_MIME);
}

function externalFileDrag(event) {
  const types = [...(event.dataTransfer?.types || [])];
  return types.includes("Files") && !types.includes(INTERNAL_DND_MIME);
}

function clipboardFiles(event) {
  const itemFiles = [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
  return itemFiles.length ? itemFiles : [...(event.clipboardData?.files || [])];
}

function normalizedClipboardFile(file) {
  if (SUPPORTED_UPLOAD_EXTENSIONS.test(file.name || "")) return file;
  const extension = CLIPBOARD_EXTENSION[String(file.type || "").toLowerCase()];
  if (!extension) return file;
  return new File([file], `剪贴板-${new Date().toISOString().replace(/[:.]/g, "-")}.${extension}`, {
    type: file.type,
    lastModified: file.lastModified || Date.now(),
  });
}

function isSupportedUpload(file) {
  return Boolean(file && (
    SUPPORTED_UPLOAD_EXTENSIONS.test(file.name || "")
    || Object.hasOwn(CLIPBOARD_EXTENSION, String(file.type || "").toLowerCase())
  ));
}

function isTextEditingTarget(target) {
  return Boolean(target?.closest?.("input:not([type=file]), textarea, select, [contenteditable=true]"));
}

function selectMaterialSlot(root, selectedSlot, { announce = true } = {}) {
  activeMaterialSlotKey = selectedSlot.dataset.materialSlotKey;
  $$('[data-material-slot]', root).forEach((slot) => {
    const selected = slot.dataset.materialSlotKey === activeMaterialSlotKey;
    const label = slot.dataset.materialSlotLabel || "附件";
    const itemId = slot.dataset.materialSlotItem;
    slot.classList.toggle("selected", selected);
    slot.setAttribute("aria-label", `${selected ? "已选中" : "选择"}主凭据 #${itemId} 的${label}粘贴槽`);
    const icon = $(".material-slot-icon", slot);
    const instruction = $("[data-material-slot-instruction]", slot);
    if (icon) icon.textContent = selected ? "✓" : "＋";
    if (instruction) instruction.textContent = selected
      ? `已选中，可按 Ctrl+V 粘贴${label}`
      : "点击槽位选中粘贴目标，也可直接拖入文件";
  });
  selectedSlot.focus();
  if (announce) toast(`已选择${selectedSlot.dataset.materialSlotLabel || "附件"}槽，可按 Ctrl+V 粘贴对应材料。`);
}

function recognizedPaymentRmb(item) {
  const payment = [...(item?.attachments || [])]
    .reverse()
    .find((attachment) => attachment.category === "payment_record");
  const recognition = payment?.ai_raw || {};
  const value = recognition.converted_amount ?? (
    String(recognition.currency || "").toUpperCase() === "CNY" ? recognition.amount : null
  );
  const amount = Number(value);
  return Number.isFinite(amount) && amount > 0 ? amount : null;
}

function clearDropFeedback(root) {
  $$(".association-drop-target", root).forEach((node) => node.classList.remove("association-drop-target"));
}

/**
 * Render and wire the complete document intake page.
 *
 * The application shell owns data fetching and cross-feature review/delete
 * flows. This Feature owns import, document-role presentation, preview, and
 * draft-to-draft attachment association.
 */
export function renderDocumentIntake(options) {
  const {
    items = [],
    reloadBootstrap = async () => {},
    refresh = async () => {},
    onManual = () => {},
    onEdit = () => {},
    onDelete = () => {},
    onReview = () => {},
    onMutationFailure = null,
  } = options || {};
  const root = $("#page-content");
  if (!root) throw new Error("Document intake requires #page-content.");

  const currentIds = new Set(items.map((item) => item.id));
  state.selectedDrafts = new Set([...state.selectedDrafts].filter((id) => currentIds.has(id)));
  const primaryCodes = primaryCategoryCodes();
  const mainItems = items.filter((item) => isMainDraft(item, primaryCodes));
  const supportingItems = items.filter((item) => !isMainDraft(item, primaryCodes));
  const targets = mainItems.filter((item) => isPrimaryTarget(item, primaryCodes));
  const associationAvailable = documentIntakeCapability("document_intake_association");
  const attachments = items.flatMap((item) => item.attachments || []);
  const itemById = new Map(items.map((item) => [Number(item.id), item]));
  const supportingById = new Map(supportingItems.map((item) => [Number(item.id), item]));
  const targetById = new Map(targets.map((item) => [Number(item.id), item]));
  const lists = state.draftView === "cards"
    ? cardLists(mainItems, supportingItems, primaryCodes, targets, associationAvailable)
    : {
      main: mainDraftTable(mainItems, primaryCodes),
      supporting: supportingDraftTable(supportingItems, primaryCodes, targets, associationAvailable),
    };

  root.innerHTML = `<div class="intake-layout document-intake-layout">
    <div class="document-intake-main">
      <section class="dropzone" id="dropzone"><div><div class="dropzone-icon">⇧</div><h2>拖入报销主凭据与附件材料</h2><p>发票、Invoice、Receipt 会成为主文件；截图、清单和支付记录会进入待关联附件区</p><button class="btn primary" type="button" id="pick-files">选择文件</button><input id="file-picker" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden></div></section>
      <section class="document-intake-board"><div class="section-head"><div><h2>待确认材料</h2><p>${mainItems.length} 个主条目 · ${supportingItems.length} 个待关联附件</p></div><div class="draft-head-actions"><div class="view-switch" aria-label="草稿视图"><button class="btn small ${state.draftView === "table" ? "active" : ""}" type="button" id="draft-table-view">表格</button><button class="btn small ${state.draftView === "cards" ? "active" : ""}" type="button" id="draft-card-view">卡片</button></div><button class="btn" type="button" id="manual-from-intake">＋ 手工录入</button></div></div>
        ${items.length ? `<div class="selection-bar" id="draft-selection-bar"><label class="selection-check"><input type="checkbox" id="select-all-drafts"> 全选当前材料</label><div class="selection-actions"><strong>已选择 <span id="draft-selection-count">${state.selectedDrafts.size}</span> 条</strong><button class="btn danger small" type="button" id="delete-selected-drafts" ${state.selectedDrafts.size ? "" : "disabled"}>删除所选</button></div></div>` : ""}
        <section class="document-role-section"><div class="document-role-heading"><div><span class="document-role-kicker">PRIMARY DOCUMENTS</span><h3>主文件与报销条目</h3></div><p>只有发票、Invoice、Receipt 与手工条目在此作为报销主体。</p></div>${lists.main}</section>
        <section class="document-role-section supporting"><div class="document-role-heading"><div><span class="document-role-kicker">SUPPORTING FILES</span><h3>待关联附件</h3></div><p>拖到主凭据，或使用下拉框设定归属；附件本身不能直接确认报销。</p></div>${supportingItems.length && !associationAvailable ? `<div class="notice warn document-service-restart" role="status"><strong>附件关联功能尚未载入。</strong> ${SERVICE_RESTART_MESSAGE}</div>` : ""}${lists.supporting}</section>
      </section>
    </div>
    <aside class="card document-preview-dock" id="document-preview-dock" aria-label="文件在线预览" hidden>
      <header class="document-preview-head"><div><span class="document-role-kicker">INLINE PREVIEW</span><strong data-document-preview-name>文件预览</strong><small data-document-preview-meta></small></div><button class="btn small document-preview-close" type="button" id="close-document-preview" aria-label="关闭文件预览">关闭</button></header>
      <div class="document-preview-body" data-document-preview-body aria-live="polite"></div>
    </aside>
  </div>`;

  const invoke = async (callback, ...args) => {
    try { await callback(...args); }
    catch (error) { toast(error.message, "error"); }
  };

  $("#manual-from-intake", root).onclick = () => invoke(onManual);
  $("#draft-table-view", root).onclick = () => {
    state.draftView = "table";
    renderDocumentIntake(options);
  };
  $("#draft-card-view", root).onclick = () => {
    state.draftView = "cards";
    renderDocumentIntake(options);
  };

  const picker = $("#file-picker", root);
  const zone = $("#dropzone", root);
  $("#pick-files", root).onclick = () => picker.click();
  const importFiles = async (files) => {
    if (!files.length) return;
    const done = busy(`正在导入并识别 ${files.length} 个文件…`);
    try {
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      const payload = await api("/imports", { method: "POST", body: form });
      const failed = (payload.results || []).filter((result) => !result.recognition_succeeded).length;
      toast(failed ? `已导入 ${files.length} 个文件，${failed} 个需手工分类或补录` : `已导入并识别 ${files.length} 个文件`);
      await reloadBootstrap();
      await refresh();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      picker.value = "";
      zone.classList.remove("drag");
      done();
    }
  };
  picker.onchange = () => importFiles([...picker.files]);
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
    if (!externalFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    zone.classList.add("drag");
  }));
  zone.addEventListener("dragleave", (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove("drag");
  });
  zone.addEventListener("drop", (event) => {
    if (!externalFileDrag(event)) return;
    event.preventDefault();
    zone.classList.remove("drag");
    importFiles([...event.dataTransfer.files]);
  });

  const uploadMaterialFile = async (slot, sourceFile) => {
    const item = itemById.get(Number(slot.dataset.materialSlotItem));
    if (!item || slot.classList.contains("uploading")) return;
    const file = normalizedClipboardFile(sourceFile);
    if (!isSupportedUpload(file)) {
      toast("仅支持 PDF、PNG、JPG/JPEG 或 WEBP 文件。", "error");
      return;
    }
    slot.classList.add("uploading");
    slot.classList.remove("drag");
    slot.setAttribute("aria-busy", "true");
    const label = slot.dataset.materialSlotLabel || "附件";
    const category = slot.dataset.materialSlotCategory;
    const done = busy(`正在补充${label}…`);
    try {
      const form = new FormData();
      form.append("expected_version", String(item.version));
      if (item.batch_ref) form.append("expected_batch_version", String(item.batch_ref.batch_version));
      form.append("category", category);
      form.append("file", file, file.name);
      const payload = await api(`/items/${item.id}/attachments`, { method: "POST", body: form });
      const recognizedAmount = category === "payment_record" ? recognizedPaymentRmb(payload.item) : null;
      activeMaterialSlotKey = null;
      await refresh();
      if (category === "payment_record") {
        toast(recognizedAmount != null
          ? `已识别人民币实付 ${recognizedAmount.toFixed(2)} CNY，列表和材料要求已更新。`
          : "支付记录已上传，但未识别到人民币实付金额，请手工确认。");
      } else {
        toast(payload.message || `${label}已附带到主凭据 #${item.id}`);
      }
    } catch (error) {
      await reportMutationFailure(onMutationFailure, error, refresh);
    } finally {
      done();
      if (slot.isConnected) {
        slot.classList.remove("uploading", "drag");
        slot.removeAttribute("aria-busy");
        const input = $("[data-material-slot-input]", slot);
        if (input) input.value = "";
      }
    }
  };

  const materialSlots = $$('[data-material-slot]', root);
  if (!materialSlots.some((slot) => slot.dataset.materialSlotKey === activeMaterialSlotKey)) {
    activeMaterialSlotKey = null;
  }
  materialSlots.forEach((slot) => {
    const input = $("[data-material-slot-input]", slot);
    const pickerButton = $("[data-material-slot-picker]", slot);
    slot.addEventListener("click", (event) => {
      if (event.target === input || event.target.closest("[data-material-slot-picker]")) return;
      if (!slot.classList.contains("uploading")) selectMaterialSlot(root, slot);
    });
    slot.addEventListener("keydown", (event) => {
      if (event.target !== slot) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      if (!slot.classList.contains("uploading")) selectMaterialSlot(root, slot);
    });
    if (pickerButton) pickerButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!slot.classList.contains("uploading")) input?.click();
    });
    if (input) input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (file) uploadMaterialFile(slot, file);
    });
    ["dragenter", "dragover"].forEach((name) => slot.addEventListener(name, (event) => {
      if (!externalFileDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "copy";
      slot.classList.add("drag");
    }));
    slot.addEventListener("dragleave", (event) => {
      if (!slot.contains(event.relatedTarget)) slot.classList.remove("drag");
    });
    slot.addEventListener("drop", (event) => {
      if (!externalFileDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      slot.classList.remove("drag");
      const file = event.dataTransfer.files?.[0];
      if (file) uploadMaterialFile(slot, file);
    });
    slot.addEventListener("paste", (event) => {
      const file = clipboardFiles(event)[0];
      if (!file) return;
      event.preventDefault();
      event.stopPropagation();
      if (slot.dataset.materialSlotKey !== activeMaterialSlotKey) {
        selectMaterialSlot(root, slot, { announce: false });
        toast(`已选择${slot.dataset.materialSlotLabel || "附件"}槽，请再次按 Ctrl+V 粘贴。`);
        return;
      }
      uploadMaterialFile(slot, file);
    });
  });
  activeMaterialPasteHandler = (event) => {
    if (state.page !== "intake" || !root.isConnected) return;
    if (isTextEditingTarget(event.target)) return;
    const visibleSlots = $$('[data-material-slot]', root);
    const file = clipboardFiles(event)[0];
    if (!file) return;
    event.preventDefault();
    const selectedSlot = visibleSlots.find((slot) => slot.dataset.materialSlotKey === activeMaterialSlotKey);
    if (!selectedSlot) {
      toast("请先点击要粘贴的材料空槽，再按 Ctrl+V。", "error");
      return;
    }
    uploadMaterialFile(selectedSlot, file);
  };

  wireThumbnailPreviews(root, attachments);
  $("#close-document-preview", root).onclick = () => closeAttachmentPreview(root);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activePreviewAttachmentId != null) closeAttachmentPreview(root);
  });
  if (activePreviewAttachmentId != null) {
    const retainedAttachment = attachments.find((attachment) => Number(attachment.id) === Number(activePreviewAttachmentId));
    if (retainedAttachment) openAttachmentPreview(root, retainedAttachment);
    else {
      activePreviewAttachmentId = null;
      previewRequestSequence += 1;
    }
  }

  $$('[data-select-draft]', root).forEach((checkbox) => {
    checkbox.onchange = () => {
      const itemId = Number(checkbox.dataset.selectDraft);
      checkbox.checked ? state.selectedDrafts.add(itemId) : state.selectedDrafts.delete(itemId);
      updateSelectionUi(root, items);
    };
  });
  const selectAll = $("#select-all-drafts", root);
  if (selectAll) selectAll.onchange = () => {
    items.forEach((item) => selectAll.checked ? state.selectedDrafts.add(item.id) : state.selectedDrafts.delete(item.id));
    $$('[data-select-draft]', root).forEach((checkbox) => { checkbox.checked = selectAll.checked; });
    updateSelectionUi(root, items);
  };
  const deleteSelected = $("#delete-selected-drafts", root);
  if (deleteSelected) deleteSelected.onclick = () => invoke(onDelete, items.filter((item) => state.selectedDrafts.has(item.id)));
  updateSelectionUi(root, items);

  $$('[data-edit-main]', root).forEach((button) => {
    button.onclick = () => {
      const item = itemById.get(Number(button.dataset.editMain));
      if (item) invoke(onEdit, item);
    };
  });
  $$('[data-delete-draft]', root).forEach((button) => {
    button.onclick = () => {
      const item = itemById.get(Number(button.dataset.deleteDraft));
      if (item) invoke(onDelete, [item]);
    };
  });
  $$('[data-review-draft]', root).forEach((button) => {
    button.onclick = () => {
      const item = itemById.get(Number(button.dataset.reviewDraft));
      if (item) invoke(onReview, { item, changes: draftChanges(item.id, root), initialMergeTargetId: null });
    };
  });
  $$('[data-review-merge-source]', root).forEach((button) => {
    button.onclick = () => {
      const item = itemById.get(Number(button.dataset.reviewMergeSource));
      if (item) invoke(onReview, {
        item,
        changes: draftChanges(item.id, root),
        initialMergeTargetId: Number(button.dataset.reviewTarget),
      });
    };
  });

  $$('[data-attachment-category]', root).forEach((select) => {
    select.onchange = async () => {
      const attachmentId = Number(select.dataset.attachmentCategory);
      const item = items.find((entry) => (entry.attachments || []).some((attachment) => attachment.id === attachmentId));
      if (!item) return;
      select.disabled = true;
      try {
        await api(`/attachments/${attachmentId}`, { method: "PATCH", body: { category: select.value, ...itemPreconditions(item) } });
        toast(primaryCodes.has(select.value) ? "已改为主凭据" : "附件类型已更新");
        await refresh();
      } catch (error) {
        await reportMutationFailure(onMutationFailure, error, refresh);
      } finally {
        if (select.isConnected) select.disabled = false;
      }
    };
  });

  const associate = async (source, target) => {
    if (!source || !target || source.id === target.id) return;
    if (!associationAvailable) {
      toast(SERVICE_RESTART_MESSAGE, "error");
      return;
    }
    const done = busy(`正在把附件关联到 #${target.id}…`);
    try {
      await api("/document-intake/associations", {
        method: "POST",
        body: {
          source_item_id: source.id,
          target_item_id: target.id,
          source_version: source.version,
          target_version: target.version,
        },
      });
      state.selectedDrafts.delete(source.id);
      toast(`附件已关联到 #${target.id}`);
      await reloadBootstrap();
      await refresh();
    } catch (error) {
      if (error.code === "not_found") {
        const restartError = new Error(`${SERVICE_RESTART_MESSAGE} 附件和主凭据均未发生变化。`);
        restartError.code = "service_restart_required";
        restartError.status = error.status;
        await reportMutationFailure(onMutationFailure, restartError, refresh);
      } else {
        await reportMutationFailure(onMutationFailure, error, refresh);
      }
    } finally {
      clearDropFeedback(root);
      done();
    }
  };

  $$('[data-associate-source]', root).forEach((button) => {
    button.onclick = () => {
      const source = supportingById.get(Number(button.dataset.associateSource));
      const select = $(`[data-association-select="${source?.id}"]`, root);
      const target = targetById.get(Number(select?.value));
      if (!target) {
        toast("请先选择要关联的主凭据。", "error");
        return;
      }
      associate(source, target);
    };
  });

  $$('[data-supporting-draft]', root).forEach((sourceNode) => {
    let cancelPointerDrag = null;
    sourceNode.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button, select, input, a") || sourceNode.getAttribute("draggable") !== "true") return;
      const source = supportingById.get(Number(sourceNode.dataset.supportingDraft));
      if (!source) return;
      const start = { x: event.clientX, y: event.clientY };
      let active = false;
      let targetNode = null;
      const cleanup = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", finish);
        document.removeEventListener("pointercancel", cancel);
        sourceNode.classList.remove("is-dragging");
        clearDropFeedback(root);
        cancelPointerDrag = null;
      };
      const move = (moveEvent) => {
        if (!active && Math.hypot(moveEvent.clientX - start.x, moveEvent.clientY - start.y) < 8) return;
        active = true;
        moveEvent.preventDefault();
        sourceNode.classList.add("is-dragging");
        targetNode = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY)?.closest("[data-association-target]") || null;
        clearDropFeedback(root);
        targetNode?.classList.add("association-drop-target");
      };
      const finish = (upEvent) => {
        const droppedNode = active
          ? document.elementFromPoint(upEvent.clientX, upEvent.clientY)?.closest("[data-association-target]") || targetNode
          : null;
        const target = droppedNode ? targetById.get(Number(droppedNode.dataset.associationTarget)) : null;
        cleanup();
        if (target) associate(source, target);
      };
      const cancel = () => cleanup();
      cancelPointerDrag = cleanup;
      document.addEventListener("pointermove", move, { passive: false });
      document.addEventListener("pointerup", finish);
      document.addEventListener("pointercancel", cancel);
    });
    sourceNode.addEventListener("dragstart", (event) => {
      if (event.target.closest("button, select, input, a") || sourceNode.getAttribute("draggable") !== "true") {
        event.preventDefault();
        return;
      }
      cancelPointerDrag?.();
      const source = supportingById.get(Number(sourceNode.dataset.supportingDraft));
      if (!source || !event.dataTransfer) {
        event.preventDefault();
        return;
      }
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData(INTERNAL_DND_MIME, JSON.stringify({ source_item_id: source.id, source_version: source.version }));
      sourceNode.classList.add("is-dragging");
    });
    sourceNode.addEventListener("dragend", () => {
      sourceNode.classList.remove("is-dragging");
      clearDropFeedback(root);
    });
  });

  $$('[data-association-target]', root).forEach((targetNode) => {
    ["dragenter", "dragover"].forEach((name) => targetNode.addEventListener(name, (event) => {
      if (!internalDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "move";
      clearDropFeedback(root);
      targetNode.classList.add("association-drop-target");
    }));
    targetNode.addEventListener("dragleave", (event) => {
      if (!targetNode.contains(event.relatedTarget)) targetNode.classList.remove("association-drop-target");
    });
    targetNode.addEventListener("drop", (event) => {
      if (!internalDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      let payload = null;
      try { payload = JSON.parse(event.dataTransfer.getData(INTERNAL_DND_MIME)); }
      catch { payload = null; }
      const source = supportingById.get(Number(payload?.source_item_id));
      const target = targetById.get(Number(targetNode.dataset.associationTarget));
      targetNode.classList.remove("association-drop-target");
      if (!source || !target || Number(payload?.source_version) !== Number(source.version)) {
        toast("拖拽内容已失效，请刷新后重试。", "error");
        return;
      }
      associate(source, target);
    });
  });
}
