import { api } from "../../shared/api.js";
import { $, $$, esc } from "../../shared/dom.js";
import { busy, showModal, toast } from "../../shared/ui.js";

const fmtMoney = (amount) => `${Number(amount || 0).toFixed(2)} CNY`;

function mergeSourceOption(batch, target) {
  const projectMismatch = Number(batch.project_id) !== Number(target.project_id);
  const archiveHint = batch.superseded_archive_path
    ? '<small class="batch-merge-archive-hint">含旧版归档，合并后移入 30 天回收区</small>'
    : "";
  return `<label class="batch-merge-option">
    <input type="checkbox" value="${batch.id}" data-batch-merge-source ${projectMismatch ? "disabled" : ""}>
    <span><strong>${esc(batch.name)}</strong><small>${batch.items.length} 笔 · ${fmtMoney(batch.total_amount)} · ${esc(batch.project_name || "未分项目")}</small>${projectMismatch ? '<small class="batch-merge-disabled-hint">项目不同，不能合并</small>' : archiveHint}</span>
  </label>`;
}

export function renderBatchMergeAction(target, batches) {
  const otherDrafts = (batches || []).filter((batch) => batch.id !== target.id && !batch.exporting);
  const available = otherDrafts.filter((batch) => Number(batch.project_id) === Number(target.project_id));
  const unavailableReason = target.exporting
    ? "当前报销包正在导出"
    : !available.length && otherDrafts.length
      ? "没有可合并的同项目报销包"
      : !available.length ? "没有其他处理中的报销包" : "";
  const label = unavailableReason ? `合并其他报销包：${unavailableReason}` : "合并其他报销包";
  return `<button class="btn" type="button" data-open-batch-merge aria-label="${esc(label)}" title="${esc(unavailableReason)}" ${unavailableReason ? "disabled" : ""}>合并其他报销包</button>`;
}

export function wireBatchMergeAction(root, options) {
  const { target, batches, beforeOpen, onMerged, onMutationFailure } = options;
  const trigger = $("[data-open-batch-merge]", root);
  if (!trigger) return;
  trigger.onclick = async () => {
    if (beforeOpen && await beforeOpen() === false) return;
    openBatchMergeModal(target, batches, { onMerged, onMutationFailure });
  };
}

export function openBatchMergeModal(target, batches, options = {}) {
  const { onMerged, onMutationFailure } = options;
  const sources = (batches || []).filter((batch) => batch.id !== target.id && !batch.exporting);
  const modal = showModal(
    "合并处理中报销包",
    `<div class="notice warn">所有选中报销包的条目和附件将追加到“${esc(target.name)}”。合并后保留目标包的名称、项目、用途和备注，来源报销包本身会被移除；之后可在目标包统一编辑和导出。</div>
    <div class="batch-merge-target"><span>合并目标</span><strong>${esc(target.name)}</strong><small>${target.items.length} 笔 · ${fmtMoney(target.total_amount)} · ${esc(target.project_name || "未分项目")}</small></div>
    <div class="field full"><label>选择要并入的报销包（仅限同一报销项目）</label><div class="batch-merge-list">${sources.map((batch) => mergeSourceOption(batch, target)).join("")}</div></div>
    <div class="notice warn batch-merge-archive-warning" data-batch-merge-archive-warning hidden>所选来源包含历史退回包。其旧版归档会移入 30 天回收区；目标包自己的旧归档仍保留到下一次成功导出。</div>
    <div class="batch-merge-selection-summary" data-batch-merge-summary>尚未选择来源报销包</div>
    <div class="field full"><label>输入目标报销包完整名称以确认</label><input data-batch-merge-confirmation aria-label="确认目标报销包名称" autocomplete="off" placeholder="${esc(target.name)}"></div>`,
    '<button class="btn" type="button" data-modal-close>取消</button><button class="btn primary" type="button" data-confirm-batch-merge disabled>合并到此报销包</button>',
  );
  $(".modal", modal.root)?.classList.add("batch-merge-modal");
  const confirmation = $("[data-batch-merge-confirmation]", modal.root);
  const submit = $("[data-confirm-batch-merge]", modal.root);
  const summary = $("[data-batch-merge-summary]", modal.root);
  const archiveWarning = $("[data-batch-merge-archive-warning]", modal.root);

  const selected = () => {
    const ids = new Set($$("[data-batch-merge-source]:checked", modal.root).map((input) => Number(input.value)));
    return sources.filter((batch) => ids.has(batch.id));
  };
  const refresh = () => {
    const chosen = selected();
    const itemCount = chosen.reduce((total, batch) => total + batch.items.length, 0);
    const total = chosen.reduce((amount, batch) => amount + Number(batch.total_amount || 0), 0);
    summary.textContent = chosen.length
      ? `将并入 ${chosen.length} 个来源包、${itemCount} 笔条目、${fmtMoney(total)}；合并后共 ${target.items.length + itemCount} 笔、${fmtMoney(Number(target.total_amount || 0) + total)}`
      : "尚未选择来源报销包";
    archiveWarning.hidden = !chosen.some((batch) => batch.superseded_archive_path);
    submit.disabled = !chosen.length || confirmation.value !== target.name || target.items.length + itemCount > 200;
    if (target.items.length + itemCount > 200) summary.textContent += "；合并后超过 200 笔上限";
  };
  $$("[data-batch-merge-source]", modal.root).forEach((input) => { input.onchange = refresh; });
  confirmation.oninput = refresh;
  confirmation.onkeydown = (event) => {
    if (event.key === "Enter" && !submit.disabled) {
      event.preventDefault();
      submit.click();
    }
  };

  submit.onclick = async () => {
    const chosen = selected();
    if (!chosen.length || confirmation.value !== target.name) return;
    const done = busy("正在合并报销包…");
    try {
      const result = await api("/batch-management/merge", {
        method: "POST",
        body: {
          target_batch_id: target.id,
          target_version: target.version,
          sources: chosen.map((batch) => ({ batch_id: batch.id, expected_version: batch.version })),
          confirmation: confirmation.value,
          discard_source_archives: chosen.some((batch) => batch.superseded_archive_path),
        },
      });
      modal.close();
      const warnings = result.cleanup_warnings || [];
      const retryable = warnings.filter((warning) => ["cleanup_failed", "cleanup_pending", "cleanup_queue_failed"].includes(warning.code)).length;
      const missingPdf = warnings.filter((warning) => warning.code === "superseded_pdf_missing").length;
      const other = warnings.length - retryable - missingPdf;
      const warningDetails = [
        retryable ? `${retryable} 项旧归档回收任务待自动重试` : "",
        missingPdf ? `${missingPdf} 个旧版 PDF 在合并前已缺失` : "",
        other ? `${other} 项旧归档状态需检查` : "",
      ].filter(Boolean).join("；");
      toast(
        warnings.length
          ? `报销包已合并，共 ${result.batch.items.length} 笔；${warningDetails}`
          : `已合并 ${result.merged_source_ids.length} 个报销包，共 ${result.batch.items.length} 笔`,
        warnings.length ? "warn" : "success",
      );
      await onMerged?.(result);
    } catch (error) {
      if (onMutationFailure) await onMutationFailure(error, { close: modal.close });
      else toast(error.message, "error");
    } finally {
      done();
    }
  };
}

export function openBatchReopenModal(batch, options = {}) {
  const { onReopened, onMutationFailure } = options;
  const modal = showModal(
    "退回报销包编辑",
    `<div class="notice warn">退回后，报销包将从历史记录移回“本次报销”。已提交/已报销状态、提交日期、到账日期及到账备注会被清除；原归档保留到新版归档成功，之后移入 30 天回收区。</div><div class="field"><label>输入完整报销包名称以确认</label><input data-batch-reopen-confirmation aria-label="确认退回的报销包名称" autocomplete="off" placeholder="${esc(batch.name)}"></div>`,
    '<button class="btn" type="button" data-modal-close>取消</button><button class="btn primary" type="button" data-confirm-batch-reopen disabled>退回编辑</button>',
  );
  const confirmation = $("[data-batch-reopen-confirmation]", modal.root);
  const submit = $("[data-confirm-batch-reopen]", modal.root);
  confirmation.oninput = () => { submit.disabled = confirmation.value !== batch.name; };
  confirmation.onkeydown = (event) => {
    if (event.key === "Enter" && !submit.disabled) {
      event.preventDefault();
      submit.click();
    }
  };
  submit.onclick = async () => {
    const done = busy("正在安全退回报销包…");
    try {
      const result = await api(`/batches/${batch.id}/reopen`, {
        method: "POST",
        body: { confirmation: confirmation.value, expected_version: batch.version },
      });
      modal.close();
      toast("报销包已退回编辑，原归档仍安全保留");
      await onReopened?.(result);
    } catch (error) {
      if (onMutationFailure) await onMutationFailure(error, { close: modal.close });
      else toast(error.message, "error");
    } finally {
      done();
    }
  };
}
