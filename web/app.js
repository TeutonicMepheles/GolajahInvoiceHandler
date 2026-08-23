import { state } from "./core/state.js";
import { openBatchReopenModal, renderBatchMergeAction, wireBatchMergeAction } from "./features/batch-management/index.js";
import { renderDocumentIntake } from "./features/document-intake/index.js";
import { isInvoiceReviewConflict, openInvoiceReview } from "./features/invoice-review/index.js";
import { renderQuotation } from "./features/quotation/index.js";
import { api } from "./shared/api.js";
import { $, $$, esc } from "./shared/dom.js";
import { FormDataForm } from "./shared/forms.js";
import { busy, emptyState, showModal, toast } from "./shared/ui.js";

const pageMeta = {
  dashboard: ["REIMBURSEMENT DESK", "仪表盘", "掌握每一笔报销的材料与进度"],
  intake: ["CAPTURE & VERIFY", "录入中心", "导入票据或手工录入，核对后才进入待报销池"],
  pool: ["EXPENSE POOL", "待报销条目", "筛选、核对材料并选择多笔条目创建报销包"],
  workspace: ["ACTIVE BATCHES", "本次报销", "补齐每笔材料，确认无缺失后生成归档材料包"],
  history: ["AUDIT TRAIL", "报销历史", "查找已提交与已报销记录，追溯每一次变更"],
  quotation: ["CLEAN QUOTATION", "报价测算", "反算整洁的开发费用，让合同总额尽量贴近上限"],
  settings: ["LOCAL SETTINGS", "设置", "管理归档目录、报销项目、材料规则与识别配置"],
};

const fmtDate = (value) => value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(`${value.slice(0, 10)}T00:00:00`)) : "—";
const fmtTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";
const fmtMoney = (amount, currency = "CNY") => `${Number(amount || 0).toFixed(2)} ${esc(currency || "CNY")}`;
const fmtBatchTotals = (batch) => fmtMoney(batch.total_amount, "CNY");
const fmtItemAmount = (item) => item.currency === "CNY"
  ? fmtMoney(item.amount, "CNY")
  : `${item.reimbursement_amount == null ? "人民币实付待识别" : fmtMoney(item.reimbursement_amount, "CNY")}（原 ${fmtMoney(item.amount, item.currency)}）`;
const convertedAmountField = (item = {}) => `<div class="field"><label>人民币实际付款金额${item.currency === "CNY" ? "（外币时填写）" : ""}</label><input name="converted_amount" type="number" min="0" max="100000000000" step="0.01" value="${item.converted_amount ?? ""}" placeholder="优先从支付记录自动识别"></div>`;

const CONCURRENCY_ERROR_CODES = new Set([
  "stale_version",
  "stale_requirements_version",
  "batch_exporting",
  "batch_changed",
  "batch_changed_during_export",
  "batch_not_found",
  "batch_locked",
  "batch_item_not_found",
  "invalid_batch_status",
  "items_unavailable",
  "item_not_found",
  "item_delete_locked",
  "item_locked",
  "attachment_not_found",
  "attachment_locked",
]);

function itemPreconditions(item) {
  const payload = { expected_version: item.version };
  if (item.batch_ref) payload.expected_batch_version = item.batch_ref.batch_version;
  return payload;
}

function appendItemPreconditions(form, item) {
  form.append("expected_version", String(item.version));
  if (item.batch_ref) form.append("expected_batch_version", String(item.batch_ref.batch_version));
}

function requirementsVersion(source = {}) {
  return source.requirements_version ?? state.bootstrap?.requirements_version;
}

async function handleMutationFailure(error, { refresh = null, close = null } = {}) {
  if (!CONCURRENCY_ERROR_CODES.has(error?.code) && !isInvoiceReviewConflict(error)) {
    toast(error.message, "error");
    return false;
  }
  close?.();
  toast("数据已被其他窗口或 Agent 更新，已刷新，请重新核对。", "error");
  if (refresh) {
    try { await refresh(); }
    catch (refreshError) { toast(refreshError.message, "error"); }
  }
  return true;
}

function restoreQuery(form, query) {
  const params = new URLSearchParams(query || "");
  params.forEach((value, key) => {
    const field = form.elements.namedItem(key);
    if (field) field.value = value;
  });
}

function projectOptions(selected, includeDisabled = false) {
  return state.bootstrap.projects
    .filter((project) => includeDisabled || project.enabled || project.id === Number(selected))
    .map((project) => `<option value="${project.id}" ${Number(selected) === project.id ? "selected" : ""}>${esc(project.name)}${project.code ? ` · ${esc(project.code)}` : ""}${project.enabled ? "" : "（已停用）"}</option>`)
    .join("");
}

function categoryOptions(selected) {
  return state.bootstrap.categories.map((category) => `<option value="${category.code}" ${selected === category.code ? "selected" : ""}>${esc(category.label)}</option>`).join("");
}

async function reloadBootstrap() {
  state.bootstrap = await api("/bootstrap");
  const counts = state.bootstrap.dashboard.counts;
  $("#nav-draft-count").textContent = counts.pending_confirmation;
  $("#nav-pool-count").textContent = counts.pending_reimbursement;
}

async function navigate(page) {
  const pageVersion = ++state.pageVersion;
  state.page = page;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  const [eyebrow, title, subtitle] = pageMeta[page];
  $("#page-eyebrow").textContent = eyebrow;
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $("#page-content").innerHTML = '<div class="loading-card"><span class="spinner"></span>正在加载…</div>';
  try {
    if (page === "dashboard") await renderDashboard(pageVersion);
    if (page === "intake") await renderIntake(pageVersion);
    if (page === "pool") await renderPool("", pageVersion);
    if (page === "workspace") await renderWorkspace(pageVersion);
    if (page === "history") await renderHistory("", pageVersion);
    if (page === "quotation") await renderQuotation(pageVersion);
    if (page === "settings") await renderSettings(pageVersion);
  } catch (error) {
    if (pageVersion !== state.pageVersion) return;
    $("#page-content").innerHTML = `<div class="card empty"><div class="empty-icon">!</div><h3>页面加载失败</h3><p>${esc(error.message)}</p><button class="btn" id="retry-page">重试</button></div>`;
    $("#retry-page").onclick = () => navigate(page);
  }
}

async function renderDashboard(pageVersion = state.pageVersion) {
  const dashboard = await api("/dashboard");
  if (pageVersion !== state.pageVersion) return;
  state.bootstrap.dashboard = dashboard;
  const counts = dashboard.counts;
  const metrics = [
    ["待整理材料", counts.pending_confirmation, "主文件待核对，附件待设定归属", "✎", "#e7f6f2", "intake"],
    ["待报销条目", counts.pending_reimbursement, "尚未纳入报销包的已确认条目", "☷", "#edf6fa", "pool"],
    ["材料缺失", counts.missing_materials, "主凭据或补充材料尚未齐全", "!", "#fff5db", "pool"],
    ["已提交未到账", counts.submitted_unreimbursed, "等待到账后手工标记为已报销", "✓", "#fdecef", "history"],
    ["数据待修正", counts.data_anomalies, "历史外币实付、合计或状态一致性异常", "!", "#fff0e8", "history"],
  ];
  const recent = dashboard.recent_batches.length ? dashboard.recent_batches.map((batch) => `
    <div class="activity-item"><i class="activity-dot"></i><div><strong>${esc(batch.name)}</strong><small>${esc(batch.project_name || "未分项目")} · ${fmtTime(batch.updated_at)}</small></div><span class="badge ${batch.status === "reimbursed" ? "ok" : batch.status === "submitted" ? "info" : "warn"}">${esc(batch.status_label)}</span></div>`).join("") : `<div class="empty"><p>还没有报销包记录</p></div>`;
  $("#page-content").innerHTML = `
    <div class="metric-grid">${metrics.map(([label, value, hint, icon, tone, page]) => `
      <button class="card metric-card" style="--tone:${tone};text-align:left;border:1px solid var(--line)" data-go="${page}"><span class="metric-label">${label}</span><strong class="metric-value">${value}</strong><p class="metric-hint">${hint}</p><i class="metric-icon">${icon}</i></button>`).join("")}</div>
    <div class="dashboard-grid">
      <section class="card panel"><div class="section-head"><div><h2>近期报销动态</h2><p>最近更新的报销包状态</p></div><button class="btn ghost small" data-go="history">查看全部 →</button></div><div class="activity-list">${recent}</div></section>
      <section class="card panel"><div class="section-head"><div><h2>开始处理</h2><p>从录入到归档的常用入口</p></div></div><div class="quick-actions">
        <button class="quick-action" data-go="intake"><span><strong>导入票据识别</strong><small>截图、图片或 PDF</small></span><b>→</b></button>
        <button class="quick-action" data-quick="manual"><span><strong>手工新建条目</strong><small>不依赖票据识别</small></span><b>＋</b></button>
        <button class="quick-action" data-go="workspace"><span><strong>继续本次报销</strong><small>补齐材料并导出</small></span><b>→</b></button>
      </div></section>
    </div>`;
  $$('[data-go]').forEach((button) => button.onclick = () => navigate(button.dataset.go));
  const quickManual = $('[data-quick="manual"]');
  if (quickManual) quickManual.onclick = openManualModal;
}

async function renderIntake(pageVersion = state.pageVersion) {
  const { items } = await api("/drafts");
  if (pageVersion !== state.pageVersion || state.page !== "intake") return;
  renderDocumentIntake({
    items,
    reloadBootstrap,
    refresh: renderIntake,
    onManual: openManualModal,
    onEdit: (item) => openEditDraftModal(item),
    onDelete: (selectedItems) => openDeleteItemsModal(selectedItems, "intake"),
    onReview: ({ item, changes, initialMergeTargetId }) => openInvoiceReview({
      item,
      changes,
      initialMergeTargetId,
      onComplete: async () => { await reloadBootstrap(); await renderIntake(); },
      onConflict: renderIntake,
    }),
    onMutationFailure: handleMutationFailure,
  });
}

function openEditDraftModal(item, returnPage = "intake", query = "") {
  const modal = showModal(returnPage === "intake" ? "编辑草稿" : "编辑报销条目", `<div class="form-grid" id="draft-edit-form">
    <div class="field"><label>商户 / 收款方</label><input name="merchant" maxlength="200" value="${esc(item.merchant)}" autofocus></div>
    <div class="field"><label>消费日期</label><input name="expense_date" type="date" value="${esc(item.expense_date)}"></div>
    <div class="field"><label>金额</label><input name="amount" type="number" min="0" step="0.01" value="${Number(item.amount || 0)}"></div>
    <div class="field"><label>币种</label><input name="currency" maxlength="8" value="${esc(item.currency || "CNY")}"></div>
    ${convertedAmountField(item)}
    <div class="field full"><label>报销项目</label><select name="project_id">${projectOptions(item.project_id)}</select></div>
    <div class="field full"><label>购买内容 / 用途摘要</label><textarea name="purpose" maxlength="2000">${esc(item.purpose)}</textarea></div>
    <div class="field full"><div class="notice ${item.attachments.length ? "success" : "warn"}">${item.attachments.length ? `已关联 ${item.attachments.length} 份受管附件；如需调整附件类型，请切换到卡片视图。` : "这条草稿尚无附件。"}</div></div>
  </div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="save-draft-edit">保存修改</button>');
  $("#save-draft-edit", modal.root).onclick = async () => {
    try {
      await api(`/items/${item.id}`, {
        method: "PATCH",
        body: { ...Object.fromEntries(FormDataForm($("#draft-edit-form", modal.root))), ...itemPreconditions(item) },
      });
      modal.close(); toast(returnPage === "intake" ? "草稿已更新" : "报销条目已更新");
      if (returnPage === "intake") await renderIntake();
      else if (returnPage === "pool") await renderPool(query);
      else await renderWorkspace();
    } catch (error) {
      const refresh = returnPage === "intake" ? renderIntake : returnPage === "pool" ? () => renderPool(query) : renderWorkspace;
      await handleMutationFailure(error, { refresh, close: modal.close });
    }
  };
}

function openDeleteItemsModal(items, sourcePage, query = "") {
  const itemIds = items.map((item) => item.id);
  const attachmentCount = items.reduce((total, item) => total + (item.attachments?.length || 0), 0);
  const label = items.length === 1
    ? `${items[0].merchant || "未命名记录"}（#${items[0].id}）`
    : `${items.length} 条已选记录`;
  const modal = showModal(
    items.length === 1 ? "删除记录" : "批量删除记录",
    `<div class="notice error"><strong>删除后，这些记录将不再出现在系统中。</strong><br>将删除 ${esc(label)}${attachmentCount ? `，并把 ${attachmentCount} 份受管附件移入 30 天回收区` : ""}。操作前会自动创建数据库快照。</div><p class="modal-hint">处理中、已提交或已报销的记录不能从这里删除。</p>`,
    '<button class="btn" data-modal-close>取消</button><button class="btn danger" id="confirm-delete-items">确认删除</button>',
  );
  $("#confirm-delete-items", modal.root).onclick = async () => {
    const done = busy("正在删除记录…");
    try {
      const result = await api("/items/bulk-delete", {
        method: "POST",
        body: { items: items.map((item) => ({ item_id: item.id, expected_version: item.version })) },
      });
      itemIds.forEach((id) => state.selected.delete(id));
      itemIds.forEach((id) => state.selectedDrafts.delete(id));
      modal.close();
      toast(result.cleanup_warnings?.length ? "记录已删除，部分文件需要手工清理" : `已删除 ${result.deleted_count} 条记录`, result.cleanup_warnings?.length ? "error" : "success");
      await reloadBootstrap();
      if (sourcePage === "intake") await renderIntake();
      else await renderPool(query);
    } catch (error) {
      const refresh = sourcePage === "intake" ? renderIntake : () => renderPool(query);
      await handleMutationFailure(error, { refresh, close: modal.close });
    }
    finally { done(); }
  };
}

function openManualModal() {
  const modal = showModal("手工新建报销条目", `<div class="form-grid" id="manual-form">
    <div class="field"><label>商户 / 收款方</label><input name="merchant" maxlength="200" autofocus></div>
    <div class="field"><label>消费日期</label><input name="expense_date" type="date" value="${new Date().toISOString().slice(0,10)}"></div>
    <div class="field"><label>金额</label><input name="amount" type="number" min="0" step="0.01"></div>
    <div class="field"><label>币种</label><input name="currency" value="CNY" maxlength="8"></div>
    ${convertedAmountField()}
    <div class="field full"><label>报销项目</label><select name="project_id">${projectOptions(state.bootstrap.projects.find((p) => p.enabled)?.id)}</select></div>
    <div class="field full"><label>用途摘要</label><textarea name="purpose" maxlength="2000" placeholder="说明具体购买内容或用途"></textarea></div>
    <div class="field full"><div class="notice success">保存后先进入“待确认”，确认前不会出现在待报销池。</div></div>
  </div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="save-manual">保存草稿</button>');
  $("#save-manual", modal.root).onclick = async () => {
    const form = $("#manual-form", modal.root);
    const payload = Object.fromEntries(FormDataForm(form));
    try {
      await api("/items/manual", { method: "POST", body: payload });
      modal.close(); toast("手工草稿已创建"); await reloadBootstrap(); await navigate("intake");
    } catch (error) { toast(error.message, "error"); }
  };
}

async function renderPool(query = "", pageVersion = state.pageVersion) {
  const { items } = await api(`/items?status=pending_reimbursement${query ? `&${query}` : ""}`);
  if (pageVersion !== state.pageVersion) return;
  state.selected = new Set([...state.selected].filter((id) => items.some((item) => item.id === id)));
  $("#page-content").innerHTML = `<form class="card filters" id="pool-filters">
    <div class="field"><label>商户或用途</label><input name="search" placeholder="输入关键词"></div>
    <div class="field"><label>报销项目</label><select name="project_id"><option value="">全部项目</option>${projectOptions()}</select></div>
    <div class="field"><label>开始日期</label><input name="date_from" type="date"></div>
    <div class="field"><label>结束日期</label><input name="date_to" type="date"></div>
    <div class="field"><label>最低金额</label><input name="amount_min" type="number" min="0" step="0.01"></div>
    <div class="field"><label>最高金额</label><input name="amount_max" type="number" min="0" step="0.01"></div>
    <div class="field"><label>材料状态</label><select name="material"><option value="">全部</option><option value="complete">已齐全</option><option value="missing">有缺失</option></select></div>
    <button class="btn" type="submit">筛选</button>
  </form>
  <div class="selection-bar" id="selection-bar"><strong>已选择 <span id="selection-count">${state.selected.size}</span> 笔</strong><div class="selection-actions"><button class="btn danger small" id="delete-selected" ${state.selected.size ? "" : "disabled"}>删除所选</button><button class="btn primary small" id="create-batch" ${state.selected.size ? "" : "disabled"}>创建本次报销</button></div></div>
  <section class="card table-card">${items.length ? `<div class="table-scroll"><table class="data"><thead><tr><th><input type="checkbox" id="select-all"></th><th>日期</th><th>商户 / 用途</th><th>报销金额</th><th>项目</th><th>材料完整度</th><th>缺失材料</th><th>操作</th></tr></thead><tbody>${items.map(poolRow).join("")}</tbody></table></div>` : `<div class="empty"><div class="empty-icon">✓</div><h3>没有符合条件的待报销条目</h3><p>确认新草稿后，条目会出现在这里。</p><button class="btn primary" data-go-intake>前往录入中心</button></div>`}</section>`;
  restoreQuery($("#pool-filters"), query);
  $("#pool-filters").onsubmit = (event) => {
    event.preventDefault();
    const params = new URLSearchParams();
    FormDataForm(event.currentTarget).forEach(([key, value]) => { if (value) params.set(key, value); });
    renderPool(params.toString());
  };
  $$('[data-select-item]').forEach((box) => box.onchange = () => { box.checked ? state.selected.add(Number(box.dataset.selectItem)) : state.selected.delete(Number(box.dataset.selectItem)); updateSelection(); });
  const selectAll = $("#select-all");
  if (selectAll) selectAll.onchange = () => { $$('[data-select-item]').forEach((box) => { box.checked = selectAll.checked; selectAll.checked ? state.selected.add(Number(box.dataset.selectItem)) : state.selected.delete(Number(box.dataset.selectItem)); }); updateSelection(); };
  $("#create-batch").onclick = () => openCreateBatchModal(items, query);
  $("#delete-selected").onclick = () => openDeleteItemsModal(items.filter((item) => state.selected.has(item.id)), "pool", query);
  $$('[data-delete-pool-item]').forEach((button) => button.onclick = () => {
    const item = items.find((entry) => entry.id === Number(button.dataset.deletePoolItem));
    if (item) openDeleteItemsModal([item], "pool", query);
  });
  $$('[data-edit-pool-item]').forEach((button) => button.onclick = () => {
    const item = items.find((entry) => entry.id === Number(button.dataset.editPoolItem));
    if (item) openEditDraftModal(item, "pool", query);
  });
  const intake = $('[data-go-intake]'); if (intake) intake.onclick = () => navigate("intake");
}

function poolRow(item) {
  const missing = item.material.missing.map((m) => m.label).join("、") || "—";
  return `<tr><td><input type="checkbox" data-select-item="${item.id}" ${state.selected.has(item.id) ? "checked" : ""}></td>
    <td>${fmtDate(item.expense_date)}</td><td><span class="table-main">${esc(item.merchant)}</span><span class="table-sub">${esc(item.purpose)}</span></td>
    <td><span class="table-main">${fmtItemAmount(item)}</span></td><td>${esc(item.project_name || "—")}</td>
    <td><span class="badge ${item.material.complete ? "ok" : "warn"}">${item.material.percent}% · ${item.material.complete ? "齐全" : "待补"}</span></td><td>${esc(missing)}</td><td><div class="table-actions"><button class="btn small" data-edit-pool-item="${item.id}">编辑</button><button class="btn danger small" data-delete-pool-item="${item.id}">删除</button></div></td></tr>`;
}

function updateSelection() {
  $("#selection-count").textContent = state.selected.size;
  $("#create-batch").disabled = !state.selected.size;
  $("#delete-selected").disabled = !state.selected.size;
}

function openCreateBatchModal(items, query = "") {
  if (!state.selected.size) return;
  const selectedItems = items.filter((item) => state.selected.has(item.id));
  if (!selectedItems.length) return;
  const defaultProject = state.bootstrap.projects.find((project) => project.enabled)?.id;
  const modal = showModal("创建本次报销", `<div class="form-grid" id="batch-create-form">
    <div class="field full"><div class="notice success">已选择 ${selectedItems.length} 笔条目。创建后仍可补充材料或移回待报销池。</div></div>
    <div class="field full"><label>报销包名称</label><input name="name" maxlength="120" value="${new Date().toISOString().slice(0,10)} 报销"></div>
    <div class="field full"><label>报销项目</label><select name="project_id">${projectOptions(defaultProject)}</select></div>
    <div class="field full"><label>用途说明</label><textarea name="purpose" maxlength="2000"></textarea></div>
    <div class="field full"><label>备注</label><textarea name="notes" maxlength="4000"></textarea></div>
  </div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="confirm-create-batch">创建报销包</button>');
  $("#confirm-create-batch", modal.root).onclick = async () => {
    const form = $("#batch-create-form", modal.root);
    const payload = Object.fromEntries(FormDataForm(form));
    payload.items = selectedItems.map((item) => ({ item_id: item.id, expected_version: item.version }));
    try {
      const result = await api("/batches", { method: "POST", body: payload });
      state.currentBatchId = result.batch.id; state.selected.clear(); modal.close(); toast("本次报销已创建");
      await reloadBootstrap(); await navigate("workspace");
    } catch (error) { await handleMutationFailure(error, { refresh: () => renderPool(query), close: modal.close }); }
  };
}

async function renderWorkspace(pageVersion = state.pageVersion) {
  const { batches } = await api("/batches?status=draft");
  if (pageVersion !== state.pageVersion) return;
  if (!batches.length) {
    $("#page-content").innerHTML = emptyState("▣", "当前没有处理中的报销包", "先从待报销池勾选条目并创建本次报销。", '<button class="btn primary" id="go-pool">前往待报销条目</button>');
    $("#go-pool").onclick = () => navigate("pool"); return;
  }
  if (!state.currentBatchId || !batches.some((batch) => batch.id === state.currentBatchId)) state.currentBatchId = batches[0].id;
  const selectedBatchId = state.currentBatchId;
  const { batch } = await api(`/batches/${selectedBatchId}`);
  if (pageVersion !== state.pageVersion || selectedBatchId !== state.currentBatchId) return;
  $("#page-content").innerHTML = `<div class="workspace-layout"><aside class="card batch-list"><div class="section-head"><div><h2>处理中</h2><p>${batches.length} 个报销包</p></div></div>${batches.map((entry) => `<button class="${entry.id === batch.id ? "active" : ""}" data-switch-batch="${entry.id}"><strong>${esc(entry.name)}</strong><small>${entry.items.length} 笔 · ${fmtBatchTotals(entry)}</small></button>`).join("")}</aside><div id="batch-workspace">${batchWorkspace(batch, batches)}</div></div>`;
  $$('[data-switch-batch]').forEach((button) => button.onclick = () => { state.currentBatchId = Number(button.dataset.switchBatch); renderWorkspace(); });
  wireBatchWorkspace(batch, batches);
}

function batchWorkspace(batch, batches) {
  const complete = batch.completeness.complete;
  const locked = batch.exporting;
  return `<section class="card batch-editor"><div class="section-head"><div><h2>${esc(batch.name)}</h2><p>创建于 ${fmtTime(batch.created_at)} · 状态：${locked ? "正在生成归档" : "处理中"}</p></div><span class="badge ${locked ? "info" : complete ? "ok" : "warn"}">${locked ? "导出中，请勿重复操作" : complete ? "材料齐全" : `${batch.completeness.missing_item_count} 笔待补`}</span></div>
    <div class="form-grid" id="batch-edit-form"><div class="field"><label>名称</label><input name="name" maxlength="120" value="${esc(batch.name)}" ${locked ? "disabled" : ""}></div><div class="field"><label>项目</label><select name="project_id" ${locked ? "disabled" : ""}>${projectOptions(batch.project_id)}</select></div><div class="field full"><label>用途说明</label><textarea name="purpose" maxlength="2000" ${locked ? "disabled" : ""}>${esc(batch.purpose)}</textarea></div><div class="field full"><label>备注</label><textarea name="notes" maxlength="4000" ${locked ? "disabled" : ""}>${esc(batch.notes)}</textarea></div></div>
    <div class="batch-summary"><div class="summary-cell"><span>条目数量</span><strong>${batch.items.length}</strong></div><div class="summary-cell"><span>报销总额</span><strong>${fmtBatchTotals(batch)}</strong></div><div class="summary-cell"><span>材料完整度</span><strong>${batch.completeness.percent}%</strong></div></div>
    <div class="progress"><i style="width:${batch.completeness.percent}%"></i></div><div class="card-actions">${renderBatchMergeAction(batch, batches)}<button class="btn danger" id="delete-batch" ${locked ? "disabled" : ""}>删除报销包</button><button class="btn" id="save-batch" ${locked ? "disabled" : ""}>保存信息</button><button class="btn primary" id="export-batch" ${complete && !locked ? "" : "disabled"}>生成归档与自适应 A4 PDF</button></div>
  </section><div class="workspace-items">${batch.items.map(workspaceItem).join("")}</div>`;
}

function workspaceItem(item) {
  const missingCode = item.material.missing[0]?.code;
  const suggestedCategory = missingCode === "primary_receipt" ? "invoice" : missingCode === "foreign_payment_rmb" ? "payment_record" : missingCode || "unknown";
  return `<article class="card workspace-item" data-workspace-item="${item.id}"><div class="workspace-item-head"><div><h3>${esc(item.merchant)}</h3><p>${fmtDate(item.expense_date)} · ${fmtItemAmount(item)} · ${esc(item.purpose)}</p></div><div class="table-actions"><button class="btn small" data-edit-workspace-item="${item.id}">编辑金额与信息</button><button class="btn danger small" data-remove-item="${item.id}">移回待报销池</button></div></div>
    <div class="material-chips">${item.material.requirements.map((m) => `<span class="material-chip ${m.satisfied ? "done" : ""}">${m.satisfied ? "✓" : "○"} ${esc(m.label)}</span>`).join("")}</div>
    <div class="attachment-mini">${item.attachments.map(attachmentEditor).join("") || '<div class="notice warn">尚无附件</div>'}</div>
    <div class="upload-strip"><select data-upload-category>${categoryOptions(suggestedCategory)}</select><input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden data-upload-file><button class="btn small" data-pick-attachment="${item.id}">＋ 上传材料</button><small>${item.foreign_payment_required ? "外币报销请上传支付记录，系统将识别人民币实付金额；" : ""}支持图片或 PDF</small></div>
  </article>`;
}

function attachmentEditor(a) {
  const nameState = a.name_locked ? " · 用户名称已锁定，导出时不会覆盖" : " · 自动规范命名";
  const recognizeAction = a.category === "payment_record" ? `<button class="btn small" style="margin-top:5px" data-recognize-payment="${a.id}">重新识别人民币实付</button>` : "";
  return `<div class="attachment-row"><div><strong><a href="${a.download_url}" target="_blank" rel="noopener">${esc(a.normalized_name)}</a></strong><small>原名：${esc(a.original_name)} · ${Math.ceil(a.size_bytes/1024)} KB${nameState}</small>${a.recognition_error ? `<small class="danger-text">支付金额识别失败：${esc(a.recognition_error)}</small>` : ""}<div style="display:flex;gap:5px;margin-top:6px"><input class="input" style="min-height:29px;padding:4px 6px;font-size:9px" value="${esc(a.normalized_name)}" data-name-input="${a.id}"><button class="btn small" data-save-name="${a.id}">确认名称</button></div></div><div><select data-edit-category="${a.id}">${categoryOptions(a.category)}</select>${recognizeAction}<button class="btn danger small" style="margin-top:5px" data-delete-attachment="${a.id}">删除</button></div></div>`;
}

function wireBatchWorkspace(batch, batches) {
  const batchEditForm = $("#batch-edit-form");
  const hasUnsavedBatchInfo = () => {
    const values = Object.fromEntries(FormDataForm(batchEditForm));
    return values.name !== batch.name
      || String(values.project_id || "") !== String(batch.project_id ?? "")
      || values.purpose !== batch.purpose
      || values.notes !== batch.notes;
  };
  wireBatchMergeAction($("#batch-workspace"), {
    target: batch,
    batches,
    beforeOpen: () => {
      if (!hasUnsavedBatchInfo()) return true;
      toast("请先保存报销包信息，再合并其他报销包。", "error");
      $("#save-batch")?.focus();
      return false;
    },
    onMerged: async (result) => {
      state.currentBatchId = result.batch.id;
      await reloadBootstrap();
      await renderWorkspace();
    },
    onMutationFailure: (error, { close }) => handleMutationFailure(error, { refresh: renderWorkspace, close }),
  });
  $("#save-batch").onclick = async () => {
    try {
      const payload = { ...Object.fromEntries(FormDataForm($("#batch-edit-form"))), expected_version: batch.version };
      await api(`/batches/${batch.id}`, { method: "PATCH", body: payload });
      toast("报销包信息已保存");
      await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  };
  $("#export-batch").onclick = () => openExportBatchModal(batch);
  $("#delete-batch").onclick = () => openDeleteDraftBatchModal(batch);
  $$('[data-remove-item]').forEach((button) => button.onclick = async () => {
    const item = batch.items.find((entry) => entry.id === Number(button.dataset.removeItem));
    if (!item) return;
    try {
      await api(`/batches/${batch.id}/items/${item.id}/remove`, {
        method: "POST",
        body: { expected_version: batch.version, expected_item_version: item.version },
      });
      toast("条目已移回待报销池");
      await reloadBootstrap();
      await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  });
  $$('[data-edit-workspace-item]').forEach((button) => button.onclick = () => {
    const item = batch.items.find((entry) => entry.id === Number(button.dataset.editWorkspaceItem));
    if (item) openEditDraftModal(item, "workspace");
  });
  $$('[data-pick-attachment]').forEach((button) => {
    const card = button.closest('[data-workspace-item]');
    const file = $('[data-upload-file]', card);
    const item = batch.items.find((entry) => entry.id === Number(button.dataset.pickAttachment));
    button.onclick = () => file.click();
    const upload = (picked) => item && uploadWorkspaceAttachment(card, item, picked);
    file.onchange = () => upload(file.files[0]);
    ["dragenter", "dragover"].forEach((name) => card.addEventListener(name, (event) => {
      event.preventDefault();
      card.classList.add("drag");
    }));
    ["dragleave", "drop"].forEach((name) => card.addEventListener(name, (event) => {
      event.preventDefault();
      card.classList.remove("drag");
    }));
    card.addEventListener("drop", (event) => upload(event.dataTransfer.files[0]));
  });
  $$('[data-edit-category]').forEach((select) => select.onchange = async () => {
    const attachmentId = Number(select.dataset.editCategory);
    const item = batch.items.find((entry) => entry.attachments.some((attachment) => attachment.id === attachmentId));
    if (!item) return;
    try {
      await api(`/attachments/${attachmentId}`, { method: "PATCH", body: { category: select.value, ...itemPreconditions(item) } });
      toast("材料类型已更新");
      await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  });
  $$('[data-save-name]').forEach((button) => button.onclick = async () => {
    const attachmentId = Number(button.dataset.saveName);
    const input = $(`[data-name-input="${attachmentId}"]`);
    const item = batch.items.find((entry) => entry.attachments.some((attachment) => attachment.id === attachmentId));
    if (!item) return;
    try {
      await api(`/attachments/${attachmentId}`, { method: "PATCH", body: { normalized_name: input.value, ...itemPreconditions(item) } });
      toast("规范名称已更新");
      await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  });
  $$('[data-recognize-payment]').forEach((button) => button.onclick = async () => {
    const attachmentId = Number(button.dataset.recognizePayment);
    const item = batch.items.find((entry) => entry.attachments.some((attachment) => attachment.id === attachmentId));
    if (!item) return;
    const done = busy("正在重新识别支付记录中的人民币实付金额…");
    try {
      const result = await api(`/attachments/${attachmentId}/recognize-payment`, { method: "POST", body: itemPreconditions(item) });
      toast(result.message || "人民币实付金额已更新");
    } catch (error) {
      await handleMutationFailure(error);
    } finally {
      done();
      await renderWorkspace();
    }
  });
  $$('[data-delete-attachment]').forEach((button) => button.onclick = async () => {
    if (!confirm("确认删除这份受管附件？")) return;
    const attachmentId = Number(button.dataset.deleteAttachment);
    const item = batch.items.find((entry) => entry.attachments.some((attachment) => attachment.id === attachmentId));
    if (!item) return;
    try {
      await api(`/attachments/${attachmentId}`, { method: "DELETE", body: itemPreconditions(item) });
      toast("附件已删除");
      await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  });
}

function openDeleteDraftBatchModal(batch) {
  const modal = showModal(
    "删除处理中报销包",
    `<div class="notice warn">删除“${esc(batch.name)}”后，报销包本身会被移除，其中 ${batch.items.length} 条记录将退回待报销池，附件不会删除。</div>`,
    '<button class="btn" data-modal-close>取消</button><button class="btn danger" id="confirm-delete-batch">删除并退回条目</button>',
  );
  $("#confirm-delete-batch", modal.root).onclick = async () => {
    const done = busy("正在删除报销包…");
    try {
      await api(`/batches/${batch.id}`, { method: "DELETE", body: { expected_version: batch.version } });
      state.currentBatchId = null;
      modal.close(); toast("报销包已删除，条目已退回待报销池");
      await reloadBootstrap(); await renderWorkspace();
    } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace, close: modal.close }); }
    finally { done(); }
  };
}

async function uploadWorkspaceAttachment(card, item, file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  form.append("category", $('[data-upload-category]', card).value);
  appendItemPreconditions(form, item);
  const done = busy("正在导入附件…");
  try {
    const result = await api(`/items/${item.id}/attachments`, { method: "POST", body: form });
    toast(result.message || "附件导入成功，已保存受管副本");
    await renderWorkspace();
  } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace }); }
  finally { done(); }
}

function openExportBatchModal(batch) {
  const modal = showModal(
    "生成报销归档",
    `<div class="notice warn">导出会冻结当前版本，按材料规则重新核对完整性，并生成归档目录与 PDF。</div><div class="field"><label>输入完整报销包名称以确认</label><input id="export-confirmation-name" autocomplete="off" placeholder="${esc(batch.name)}"></div>`,
    '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="confirm-export-batch" disabled>确认生成归档</button>',
  );
  const confirmation = $("#export-confirmation-name", modal.root);
  const submit = $("#confirm-export-batch", modal.root);
  confirmation.oninput = () => { submit.disabled = confirmation.value !== batch.name; };
  submit.onclick = () => exportCurrentBatch(batch, confirmation.value, modal);
}

async function exportCurrentBatch(batch, confirmationName, modal) {
  const done = busy("正在冻结数据、生成归档与自适应 A4 PDF…");
  try {
    const result = await api(`/batches/${batch.id}/export`, {
      method: "POST",
      body: {
        expected_version: batch.version,
        expected_requirements_version: requirementsVersion(batch),
        confirmation_name: confirmationName,
      },
    });
    modal.close();
    toast(result.idempotent ? "已存在归档，已返回现有材料包" : `材料包已生成，共 ${result.pdf_report.page_count} 页`);
    await reloadBootstrap(); await navigate("history");
  } catch (error) { await handleMutationFailure(error, { refresh: renderWorkspace, close: modal.close }); }
  finally { done(); }
}

async function renderHistory(query = "", pageVersion = state.pageVersion) {
  const { batches } = await api(`/history${query ? `?${query}` : ""}`);
  if (pageVersion !== state.pageVersion) return;
  $("#page-content").innerHTML = `<form class="card filters" id="history-filters">
    <div class="field"><label>名称、用途或商户</label><input name="search" placeholder="输入关键词"></div>
    <div class="field"><label>状态</label><select name="status"><option value="">全部状态</option><option value="submitted">已提交</option><option value="reimbursed">已报销</option></select></div>
    <div class="field"><label>报销项目</label><select name="project_id"><option value="">全部项目</option>${projectOptions(undefined, true)}</select></div>
    <div class="field"><label>提交开始</label><input name="submitted_from" type="date"></div>
    <div class="field"><label>提交结束</label><input name="submitted_to" type="date"></div>
    <div class="field"><label>到账开始</label><input name="reimbursed_from" type="date"></div>
    <div class="field"><label>到账结束</label><input name="reimbursed_to" type="date"></div>
    <div class="field"><label>最低金额</label><input name="amount_min" type="number" min="0" step="0.01"></div>
    <div class="field"><label>最高金额</label><input name="amount_max" type="number" min="0" step="0.01"></div>
    <button class="btn" type="submit">查询</button></form>
    <div class="history-list">${batches.length ? batches.map(historyCard).join("") : emptyState("◷", "没有符合条件的历史记录", "材料包生成后会自动进入已提交历史。")}</div>`;
  restoreQuery($("#history-filters"), query);
  $("#history-filters").onsubmit = (event) => { event.preventDefault(); const params = new URLSearchParams(); FormDataForm(event.currentTarget).forEach(([k,v]) => { if(v) params.set(k,v); }); renderHistory(params.toString()); };
  $$('[data-toggle-history]').forEach((button) => button.onclick = () => { const panel = $(`#history-details-${button.dataset.toggleHistory}`); panel.hidden = !panel.hidden; button.textContent = panel.hidden ? "展开详情" : "收起详情"; });
  $$('[data-mark-reimbursed]').forEach((button) => button.onclick = () => {
    const batch = batches.find((entry) => entry.id === Number(button.dataset.markReimbursed));
    if (batch) openReimbursedModal(batch);
  });
  $$('[data-open-archive]').forEach((button) => button.onclick = async () => { try { await api(`/batches/${button.dataset.openArchive}/open`, { method: "POST", body: { kind: "archive" } }); toast("已打开归档目录"); } catch(error) { toast(error.message,"error"); } });
  $$('[data-delete-history]').forEach((button) => button.onclick = () => {
    const batch = batches.find((entry) => entry.id === Number(button.dataset.deleteHistory));
    if (batch) openDeleteHistoryModal(batch);
  });
  $$('[data-reopen-batch]').forEach((button) => button.onclick = () => {
    const batch = batches.find((entry) => entry.id === Number(button.dataset.reopenBatch));
    if (batch) openBatchReopenModal(batch, {
      onReopened: async (result) => {
        state.currentBatchId = result.batch.id;
        await reloadBootstrap();
        await navigate("workspace");
      },
      onMutationFailure: (error, { close }) => handleMutationFailure(error, { refresh: () => renderHistory(query), close }),
    });
  });
}

function historyCard(batch) {
  const tone = batch.status === "reimbursed" ? "ok" : "info";
  const foreignNeedsRepair = batch.items.some((item) => item.currency !== "CNY" && (item.reimbursement_amount == null || !item.attachments.some((attachment) => attachment.category === "payment_record")));
  return `<article class="card history-card"><div class="history-summary"><div><h3>${esc(batch.name)}</h3><small>${esc(batch.project_name || "未分项目")} · ${esc(batch.purpose || "未填写用途")}</small></div><div class="history-stat"><span>总金额</span><strong>${fmtBatchTotals(batch)}</strong></div><div class="history-stat"><span>提交日期</span><strong>${fmtDate(batch.submitted_date)}</strong></div><div class="history-stat"><span>到账日期</span><strong>${fmtDate(batch.reimbursed_date)}</strong></div><div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end"><span class="badge ${tone}">${esc(batch.status_label)}</span><button class="btn small" data-toggle-history="${batch.id}">展开详情</button></div></div>
    <div class="history-details" id="history-details-${batch.id}" hidden>${foreignNeedsRepair ? '<div class="notice warn">该历史记录缺少可核验的人民币实付金额或支付记录；请退回编辑后上传支付记录并重新归档。</div>' : ""}<div class="card-actions" style="justify-content:flex-start"><a class="btn small" href="${batch.pdf_download_url}" target="_blank">打开 PDF</a><button class="btn small" data-open-archive="${batch.id}">打开归档目录</button>${batch.status === "submitted" ? `<button class="btn primary small" data-mark-reimbursed="${batch.id}">标记已报销</button>` : ""}<button class="btn small" data-reopen-batch="${batch.id}">退回编辑</button><button class="btn danger small" data-delete-history="${batch.id}">删除历史记录</button></div>
      ${batch.items.map(traceItem).join("")}<div style="margin-top:14px"><strong style="font-size:10px">报销包审计日志</strong>${batch.audit_logs.map((log) => `<div class="audit-line">${fmtTime(log.created_at)} · ${esc(log.action)}</div>`).join("")}</div></div></article>`;
}

function openDeleteHistoryModal(batch) {
  const modal = showModal(
    "删除报销历史",
    `<div class="notice error"><strong>这会删除报销包及其中 ${batch.items.length} 条记录。</strong><br>系统会先创建数据库快照；受管附件会移入本地回收区并保留 30 天。若不勾选归档目录，已生成的 PDF 和归档材料仍留在原位置。</div><div class="field"><label>输入完整报销包名称以确认</label><input id="history-delete-confirmation" autocomplete="off" placeholder="${esc(batch.name)}"></div><label class="danger-check"><input id="delete-archive-files" type="checkbox"> 同时将本地归档目录移入 30 天回收区</label>`,
    '<button class="btn" data-modal-close>取消</button><button class="btn danger" id="confirm-delete-history" disabled>删除记录</button>',
  );
  const confirmation = $("#history-delete-confirmation", modal.root);
  const submit = $("#confirm-delete-history", modal.root);
  confirmation.oninput = () => { submit.disabled = confirmation.value !== batch.name; };
  submit.onclick = async () => {
    const done = busy("正在删除历史记录…");
    try {
      const result = await api(`/batches/${batch.id}`, {
        method: "DELETE",
        body: {
          confirmation: confirmation.value,
          delete_archive: $("#delete-archive-files", modal.root).checked,
          expected_version: batch.version,
        },
      });
      modal.close();
      toast(result.cleanup_warnings?.length ? "记录已删除，部分文件需要手工清理" : "历史记录已删除", result.cleanup_warnings?.length ? "error" : "success");
      await reloadBootstrap(); await renderHistory();
    } catch (error) { await handleMutationFailure(error, { refresh: renderHistory, close: modal.close }); }
    finally { done(); }
  };
}

function traceItem(item) {
  const aiJson = item.ai_raw ? `<details class="trace-json"><summary>查看 AI 识别原始草稿</summary><pre>${esc(JSON.stringify(item.ai_raw, null, 2))}</pre></details>` : "";
  const confirmedJson = item.confirmed_snapshot ? `<details class="trace-json"><summary>查看用户最终确认内容</summary><pre>${esc(JSON.stringify(item.confirmed_snapshot, null, 2))}</pre></details>` : "";
  return `<div class="trace-item"><h4>${esc(item.merchant)} · ${fmtItemAmount(item)}</h4><div class="trace-grid"><div>消费日期<br><strong>${fmtDate(item.expense_date)}</strong></div><div>用途<br><strong>${esc(item.purpose)}</strong></div><div>AI 草稿<br><strong>${item.ai_raw ? "已保留" : "手工录入"}</strong></div><div>最终确认<br><strong>${item.confirmed_snapshot ? "已保留" : "—"}</strong></div></div>${aiJson}${confirmedJson}<div class="attachment-mini">${item.attachments.map(traceAttachment).join("")}</div>${item.audit_logs.map((log) => `<div class="audit-line">${fmtTime(log.created_at)} · ${esc(log.action)}</div>`).join("")}</div>`;
}

function traceAttachment(a) {
  const recognition = a.ai_raw ? `<details class="trace-json"><summary>查看该附件的 AI 识别结果</summary><pre>${esc(JSON.stringify(a.ai_raw, null, 2))}</pre></details>` : "";
  return `<div class="attachment-row"><div><strong><a href="${a.download_url}" target="_blank">${esc(a.normalized_name)}</a></strong><small>材料：${esc(a.category_label)} · 原名：${esc(a.original_name)}</small>${recognition}</div></div>`;
}

function openReimbursedModal(batch) {
  const modal = showModal("标记为已报销", `<div class="form-grid" id="reimbursed-form"><div class="field full"><label>到账 / 报销日期</label><input name="reimbursed_date" type="date" value="${new Date().toISOString().slice(0,10)}"></div><div class="field full"><label>到账备注</label><textarea name="notes" placeholder="可记录到账金额、差异或凭证信息"></textarea></div></div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="confirm-reimbursed">确认已到账</button>');
  $("#confirm-reimbursed", modal.root).onclick = async () => {
    try {
      await api(`/batches/${batch.id}/mark-reimbursed`, {
        method: "POST",
        body: { ...Object.fromEntries(FormDataForm($("#reimbursed-form", modal.root))), expected_version: batch.version },
      });
      modal.close(); toast("报销包已标记为已报销"); await reloadBootstrap(); await renderHistory();
    } catch(error) { await handleMutationFailure(error, { refresh: renderHistory, close: modal.close }); }
  };
}

async function renderSettings(pageVersion = state.pageVersion) {
  const data = await api("/settings");
  if (pageVersion !== state.pageVersion) return;
  state.bootstrap.projects = data.projects;
  state.bootstrap.rules = data.rules;
  state.bootstrap.requirements_version = data.requirements_version;
  const latestBackup = data.storage.latest_backup
    ? new Date(data.storage.latest_backup.created_at).toLocaleString("zh-CN", { hour12: false })
    : "尚未创建";
  const storageHealthy = data.reconciliation.healthy;
  const integrityHealthy = data.integrity.healthy;
  const recognition = data.recognition;
  const recognitionMessages = {
    unconfigured: "尚未配置 DeepSeek API 密钥",
    unknown: "密钥已配置，等待首次实际识别验证",
    available: "最近一次 DeepSeek 识别成功",
    unavailable: recognition.last_error || "最近一次 DeepSeek 识别失败",
  };
  const recognitionNoticeClass = recognition.availability === "available" ? "success" : "warn";
  const recognitionAttempt = recognition.last_attempt_at ? fmtTime(recognition.last_attempt_at) : "尚无实际调用";
  $("#page-content").innerHTML = `<div class="settings-grid"><div>
    <section class="card settings-section"><h2>本地归档目录</h2><p>每个报销包将在此目录创建独立文件夹，保留规范命名的原始材料、归档清单和合并 PDF。</p><div class="field"><label>绝对路径</label><input id="archive-root" value="${esc(data.settings.archive_root)}"></div><div class="card-actions"><button class="btn primary" id="save-archive-root">验证并保存</button></div></section>
    <section class="card settings-section"><h2>报销项目</h2><p>停用项目不会影响历史记录；它仍可用于历史筛选，但不会出现在新建入口中。</p><div id="project-list">${data.projects.map(projectRow).join("")}</div><div class="card-actions"><button class="btn" id="add-project">＋ 新增项目</button></div></section>
    <section class="card settings-section"><h2>材料规则</h2><p>材料名称和金额区间都保存在本地数据库中；修改后立即用于所有未归档条目的完整性计算。</p><h3 class="settings-subhead">材料名称</h3><div id="material-name-list">${data.materials.map(materialNameRow).join("")}</div><div class="card-actions"><button class="btn" id="save-material-labels">保存材料名称</button></div><h3 class="settings-subhead">金额区间</h3><div id="rule-list">${data.rules.map(ruleRow).join("")}</div><div class="card-actions"><button class="btn primary" id="save-rules">保存材料规则</button></div></section>
  </div><aside><section class="card settings-section"><h2>数据与后台服务</h2><p>业务数据存放在独立的持久目录中；后台服务随登录启动，数据库每天自动生成快照。</p><pre class="settings-code">${esc(data.storage.data_dir)}</pre><div class="notice ${storageHealthy ? "success" : "warn"}">${storageHealthy ? "受管文件、数据库引用与清理队列一致" : `存储待处理：缺失 ${data.reconciliation.missing_managed_files.length}、无主 ${data.reconciliation.orphaned_managed_files.length}、待清理 ${data.reconciliation.pending_cleanup_count}`}</div><div class="notice ${integrityHealthy ? "success" : "warn"}">${integrityHealthy ? "金额、状态与报销包合计一致" : `发现 ${data.integrity.count} 项业务数据待修正：${data.integrity.entries.map((entry) => esc(entry.message)).join("；")}`}</div><div class="summary-cell"><span>数据库快照</span><strong>${data.storage.backup_count} 份 / 自动与手工各保留 ${data.storage.backup_retention} 份</strong></div><div class="summary-cell" style="margin-top:8px"><span>最近快照</span><strong style="font-size:12px">${esc(latestBackup)}</strong></div><div class="card-actions"><button class="btn primary" id="backup-now">立即备份</button><button class="btn" id="open-data-dir">打开数据目录</button></div></section>
    <section class="card settings-section"><h2>DeepSeek 识别</h2><p>密钥只由本地后端读取，不会发送到浏览器。在应用目录的 <code>.env.local</code> 中保存：</p><pre class="settings-code">DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash-vision-exp</pre><p>保存后重启应用生效。PDF 会先在本机逐页转换为图片；状态以最近一次实际识别为准，不会额外发起付费探测。</p><div class="notice ${recognitionNoticeClass}">${esc(recognitionMessages[recognition.availability] || "识别状态未知")}</div><div class="summary-cell"><span>当前模型</span><strong style="font-size:13px">${esc(recognition.model)}</strong></div><div class="summary-cell" style="margin-top:8px"><span>最近调用</span><strong style="font-size:12px">${esc(recognitionAttempt)}</strong></div></section>
    <section class="card settings-section"><h2>材料类型说明</h2><p>中国增值税发票、国外 Invoice 与 Receipt 均可满足“主凭据”。</p>${state.bootstrap.categories.map((c) => `<div class="activity-item"><i class="activity-dot"></i><div><strong>${esc(c.label)}</strong><small>${esc(c.material)}</small></div></div>`).join("")}</section></aside></div>`;
  $("#save-archive-root").onclick = async () => { try { const result = await api("/settings/archive-root", { method:"PUT", body:{ archive_root: $("#archive-root").value } }); toast(`归档目录已保存：${result.archive_root}`); } catch(error){ toast(error.message,"error"); } };
  $$('[data-save-project]').forEach((button) => button.onclick = () => saveProject(Number(button.dataset.saveProject)));
  $("#add-project").onclick = openAddProjectModal;
  $("#save-material-labels").onclick = saveMaterialLabels;
  $("#save-rules").onclick = saveRules;
  $("#backup-now").onclick = async () => {
    try {
      await api("/system/backup", { method: "POST", body: {} });
      toast("数据库快照已创建");
      await renderSettings();
    } catch(error) { toast(error.message, "error"); }
  };
  $("#open-data-dir").onclick = async () => {
    try {
      await api("/system/open-data-directory", { method: "POST", body: {} });
      toast("已打开持久数据目录");
    } catch(error) { toast(error.message, "error"); }
  };
}

function materialNameRow(material) {
  return `<div class="material-name-row" data-material-name="${material.code}"><code>${esc(material.code)}</code><input value="${esc(material.label)}" maxlength="30"></div>`;
}

function projectRow(project) {
  return `<div class="project-row" data-project-row="${project.id}"><input name="name" maxlength="120" value="${esc(project.name)}"><input name="code" maxlength="60" value="${esc(project.code)}" placeholder="编号"><input name="notes" maxlength="1000" value="${esc(project.notes)}" placeholder="备注"><div style="display:flex;gap:5px;align-items:center"><label style="font-size:9px"><input name="enabled" type="checkbox" ${project.enabled ? "checked" : ""}>启用</label><button class="btn small" data-save-project="${project.id}">保存</button></div></div>`;
}

function ruleRow(rule) {
  return `<div class="rule-row" data-rule-row><input name="label" value="${esc(rule.label)}"><input name="min_amount" type="number" min="0" step="0.01" value="${rule.min_amount}"><input name="max_amount" type="number" min="0" step="0.01" value="${rule.max_amount ?? ""}" placeholder="无上限"><div class="check-group">${state.bootstrap.materials.map(({code,label}) => `<label><input type="checkbox" name="required" value="${code}" ${rule.required.includes(code) ? "checked" : ""}>${esc(label)}</label>`).join("")}</div><span class="badge muted">规则</span></div>`;
}

async function saveMaterialLabels() {
  const materials = $$('[data-material-name]').map((row) => ({
    code: row.dataset.materialName,
    label: $('input', row).value,
  }));
  try {
    const result = await api("/material-labels", { method:"PUT", body:{materials} });
    state.bootstrap.materials = result.materials;
    toast("材料名称已保存");
    await renderSettings();
  } catch(error) { toast(error.message,"error"); }
}

async function saveProject(projectId) {
  const row = $(`[data-project-row="${projectId}"]`);
  const payload = Object.fromEntries(FormDataForm(row));
  try { await api(`/projects/${projectId}`, { method:"PATCH", body:payload }); toast("项目已更新"); await reloadBootstrap(); await renderSettings(); }
  catch(error){ toast(error.message,"error"); }
}

function openAddProjectModal() {
  const modal = showModal("新增报销项目", `<div class="form-grid" id="new-project-form"><div class="field full"><label>项目名称</label><input name="name" maxlength="120"></div><div class="field"><label>项目编号</label><input name="code" maxlength="60"></div><div class="field"><label>备注</label><input name="notes" maxlength="1000"></div></div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="confirm-add-project">新增</button>');
  $("#confirm-add-project", modal.root).onclick = async () => { try { await api("/projects", { method:"POST", body:Object.fromEntries(FormDataForm($("#new-project-form",modal.root))) }); modal.close(); toast("项目已新增"); await reloadBootstrap(); await renderSettings(); } catch(error){ toast(error.message,"error"); } };
}

async function saveRules() {
  const rules = $$('[data-rule-row]').map((row) => ({
    label: $('[name="label"]',row).value,
    min_amount: $('[name="min_amount"]',row).value,
    max_amount: $('[name="max_amount"]',row).value,
    required: $$('[name="required"]:checked',row).map((box) => box.value),
  }));
  try {
    await api("/material-rules", {
      method:"PUT",
      body:{ rules, expected_requirements_version: state.bootstrap.requirements_version },
    });
    toast("材料规则已保存"); await reloadBootstrap(); await renderSettings();
  }
  catch(error){ await handleMutationFailure(error, { refresh: renderSettings }); }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#today-chip").textContent = new Intl.DateTimeFormat("zh-CN", { year:"numeric", month:"long", day:"numeric", weekday:"short" }).format(new Date());
  $$(".nav-item").forEach((button) => button.onclick = () => navigate(button.dataset.page));
  $('[data-global-action="manual"]').onclick = () => openManualModal();
  try { await reloadBootstrap(); await navigate("dashboard"); }
  catch(error) { $("#page-content").innerHTML = emptyState("!", "应用初始化失败", error.message); toast(error.message,"error"); }
});
