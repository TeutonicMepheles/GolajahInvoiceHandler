import { api } from "../../shared/api.js";
import { $, $$, esc } from "../../shared/dom.js";
import { busy, showModal, toast } from "../../shared/ui.js";

const REVIEW_CONFLICT_CODES = new Set([
  "stale_version",
  "review_changed",
  "review_required",
  "duplicate_review_overflow",
  "duplicate_review_not_required",
  "duplicate_review_session_not_found",
  "duplicate_review_session_expired",
  "duplicate_review_session_completed",
  "invalid_review_cursor",
  "invalid_overflow_review_token",
  "invalid_merge_target",
  "merge_target_not_reviewed",
  "source_not_draft",
  "target_locked",
  "invalid_item_status",
  "item_not_found",
  "batch_exporting",
]);

const EDITABLE_FIELDS = [
  "merchant",
  "expense_date",
  "amount",
  "currency",
  "converted_amount",
  "project_id",
  "purpose",
];

export function isInvoiceReviewConflict(error) {
  return REVIEW_CONFLICT_CODES.has(error?.code);
}

function itemPreconditions(item) {
  const payload = { expected_version: item.version };
  if (item.batch_ref) payload.expected_batch_version = item.batch_ref.batch_version;
  return payload;
}

function normalizedComparable(field, value) {
  if (field === "amount" || field === "converted_amount") {
    if (value === null || value === undefined || value === "") return "";
    const number = Number(value);
    return Number.isFinite(number) ? String(number) : String(value).trim();
  }
  if (field === "project_id") {
    if (value === null || value === undefined || value === "") return "";
    return String(Number(value));
  }
  if (field === "currency") return String(value || "").trim().toUpperCase();
  return String(value ?? "").trim();
}

function changedFields(item, changes) {
  if (!changes) return null;
  const result = {};
  EDITABLE_FIELDS.forEach((field) => {
    if (!(field in changes)) return;
    if (normalizedComparable(field, changes[field]) !== normalizedComparable(field, item[field])) {
      result[field] = changes[field];
    }
  });
  return Object.keys(result).length ? result : null;
}

function candidateId(candidate) {
  return Number(candidate?.id ?? candidate?.item?.id);
}

function candidateVersion(candidate) {
  return Number(candidate?.version ?? candidate?.item?.version);
}

function candidateBatchRef(candidate) {
  return candidate?.batch_ref || candidate?.item?.batch_ref || null;
}

function candidateValue(candidate, key, fallback = "") {
  return candidate?.[key] ?? candidate?.item?.[key] ?? fallback;
}

function candidateIsBlocking(candidate, blockingIds) {
  return blockingIds.has(candidateId(candidate));
}

function candidateCanMerge(candidate) {
  if (candidate?.merge_allowed !== undefined) return Boolean(candidate.merge_allowed);
  return !Boolean(candidateValue(candidate, "historical", false));
}

function formatCandidateAmount(candidate) {
  const cents = candidateValue(candidate, "amount_cents", null);
  const amount = cents === null
    ? Number(candidateValue(candidate, "amount", 0))
    : Number(cents) / 100;
  return `${Number.isFinite(amount) ? amount.toFixed(2) : "0.00"} ${esc(candidateValue(candidate, "currency", "CNY"))}`;
}

function candidateReason(candidate) {
  const labels = [];
  const reasonLabels = {
    exact_file: "同一文件",
    high_confidence: "高置信匹配",
    historical: "历史记录",
  };
  if (candidateValue(candidate, "exact_file", false)) labels.push("同一文件");
  if (candidateValue(candidate, "high_confidence", false)) labels.push("高置信匹配");
  if (candidateValue(candidate, "historical", false)) labels.push("历史记录");
  const reasons = candidateValue(candidate, "reason", []);
  if (Array.isArray(reasons)) reasons.forEach((reason) => labels.push(reasonLabels[reason] || String(reason)));
  return [...new Set(labels)].join(" · ") || "相似记录";
}

function candidateRow(candidate, blockingIds, selectedTargetId, selectionEnabled) {
  const id = candidateId(candidate);
  const historical = Boolean(candidateValue(candidate, "historical", false));
  const canMerge = candidateCanMerge(candidate) && !historical;
  const blocking = candidateIsBlocking(candidate, blockingIds);
  const choice = canMerge
    ? `<label class="review-merge-choice"><input type="radio" name="review-merge-target" value="${id}" ${id === selectedTargetId ? "checked" : ""} ${selectionEnabled ? "" : "disabled"}> 合并到此条</label>`
    : '<span class="badge warn">仅可保留为独立报销</span>';
  return `<article class="review-candidate ${blocking ? "blocking" : ""}" data-review-candidate="${id}">
    <div><strong>#${id} · ${esc(candidateValue(candidate, "merchant", "未命名记录"))}</strong><small>${esc(candidateValue(candidate, "expense_date", "日期待确认"))} · ${formatCandidateAmount(candidate)} · ${esc(candidateValue(candidate, "status", ""))}</small><small>${esc(candidateReason(candidate))}</small></div>
    <div>${blocking ? '<span class="badge danger">阻断项</span>' : '<span class="badge muted">参考项</span>'}${choice}</div>
  </article>`;
}

function reviewPage(payload) {
  const session = payload?.review_session || payload?.session || payload || {};
  const page = payload?.page || session.page || session;
  const candidates = page.candidates || page.duplicate_candidates || page.items || [];
  const nextCursor = page.next_cursor ?? session.next_cursor ?? null;
  const hasMore = page.has_more ?? session.has_more ?? Boolean(nextCursor);
  return {
    sessionId: session.session_id || session.id || payload?.session_id,
    candidates: Array.isArray(candidates) ? candidates : [],
    nextCursor,
    hasMore: Boolean(hasMore),
    reviewedCount: Number(page.reviewed_count ?? session.reviewed_count ?? 0),
    blockingTotal: Number(page.blocking_total ?? session.blocking_total ?? 0),
    overflowToken: page.overflow_review_token || session.overflow_review_token || payload?.overflow_review_token || null,
  };
}

function currentSelectedTarget(root, fallback = null) {
  const selected = $('[name="review-merge-target"]:checked', root);
  return selected ? Number(selected.value) : fallback;
}

function allRequiredChecksComplete(root, review, overflowComplete) {
  const uncertaintyChecks = $$('[data-review-uncertainty]', root);
  if (uncertaintyChecks.some((input) => !input.checked)) return false;
  const recognitionCheck = $('[data-review-recognition]', root);
  if (recognitionCheck && !recognitionCheck.checked) return false;
  const duplicateCheck = $('[data-review-duplicates]', root);
  if (duplicateCheck && !duplicateCheck.checked) return false;
  return !review.duplicate_review_overflow || overflowComplete;
}

async function loadCurrentItem(itemId) {
  const result = await api(`/items/${itemId}`);
  if (!result?.item?.review?.token) {
    const error = new Error("服务器未返回可用的核对快照，请刷新后重试。");
    error.code = "review_required";
    throw error;
  }
  return result.item;
}

export async function openInvoiceReview({
  item,
  changes = null,
  initialMergeTargetId = null,
  onComplete = async () => {},
  onConflict = async () => {},
}) {
  async function refreshConflict(error, message) {
    toast(message, "error");
    try { await onConflict(error); }
    catch (refreshError) { toast(refreshError.message, "error"); }
  }

  const preparing = busy("正在保存并读取最新核对内容…");
  let detail;
  try {
    const updates = changedFields(item, changes);
    if (updates) {
      await api(`/items/${item.id}`, {
        method: "PATCH",
        body: { ...updates, ...itemPreconditions(item) },
      });
    }
    detail = await loadCurrentItem(item.id);
  } catch (error) {
    preparing();
    if (isInvoiceReviewConflict(error)) {
      await refreshConflict(error, "条目已被其他窗口或 Agent 更新，已刷新，请重新核对。");
      return;
    }
    toast(error.message, "error");
    return;
  }
  preparing();

  const review = detail.review;
  const uncertaintyIds = (review.uncertainties || []).map((entry) => entry.uncertainty_id);
  const blockingIds = new Set((review.blocking_duplicate_ids || []).map(Number));
  let selectedTargetId = initialMergeTargetId ? Number(initialMergeTargetId) : null;
  const initialCandidate = (review.duplicate_candidates || []).find((candidate) => candidateId(candidate) === selectedTargetId);
  if (selectedTargetId && (!initialCandidate || !candidateCanMerge(initialCandidate))) {
    selectedTargetId = null;
  }
  let sessionId = null;
  let nextCursor = null;
  let overflowToken = null;
  let overflowComplete = !review.duplicate_review_overflow;
  let reviewedCount = 0;
  let sessionCandidates = [];
  let pageCandidates = review.duplicate_candidates || [];
  let sessionBusy = false;

  const uncertaintyHtml = review.uncertainties?.length
    ? `<section class="review-section"><h3>逐项确认不确定内容</h3><div class="review-check-list">${review.uncertainties.map((entry) => `<label><input type="checkbox" data-review-uncertainty="${esc(entry.uncertainty_id)}"> <span>${esc(entry.message || entry.label || entry.uncertainty_id)}</span></label>`).join("")}</div></section>`
    : '<div class="notice success">当前字段没有未确认的不确定项。</div>';
  const recognitionHtml = review.recognition_failure
    ? `<label class="review-explicit-check notice warn"><input type="checkbox" data-review-recognition> <span>识别失败：${esc(review.recognition_failure)}。我已根据原始材料手工核对当前字段。</span></label>`
    : "";
  const modal = showModal(
    "核对并确认报销条目",
    `<div class="review-summary"><strong>${esc(detail.merchant || "未填写商户")}</strong><span>${esc(detail.expense_date || "日期待填写")} · ${(Number(detail.amount_cents || 0) / 100).toFixed(2)} ${esc(detail.currency || "CNY")}</span><small>${esc(detail.purpose || "用途待填写")}</small></div>
      ${recognitionHtml}${uncertaintyHtml}
      <section class="review-section"><div class="section-head"><div><h3>重复项核对</h3><p>${review.blocking_total || 0} 个阻断候选；核对内容变化后本次确认自动失效</p></div></div><div id="review-duplicate-panel"></div></section>`,
    '<button class="btn" data-modal-close>取消</button><button class="btn" id="review-merge" disabled>合并到所选条目</button><button class="btn primary" id="review-confirm" disabled>确认进入待报销</button>',
  );
  const duplicatePanel = $("#review-duplicate-panel", modal.root);
  const confirmButton = $("#review-confirm", modal.root);
  const mergeButton = $("#review-merge", modal.root);
  $(".modal", modal.root)?.classList.add("review-modal");

  function renderDuplicates() {
    const overflow = review.duplicate_review_overflow;
    const candidates = overflow && sessionId ? pageCandidates : (review.duplicate_candidates || []);
    const blockingTotal = Number(review.blocking_total || 0);
    const noCandidates = !candidates.length
      ? '<div class="notice success">未发现需要处置的重复候选。</div>'
      : `<div class="review-candidate-list">${candidates.map((candidate) => candidateRow(candidate, blockingIds, selectedTargetId, !overflow || Boolean(sessionId))).join("")}</div>`;
    const selectedCandidate = selectedTargetId
      ? [...sessionCandidates, ...(review.duplicate_candidates || [])]
        .find((candidate) => candidateId(candidate) === selectedTargetId)
      : null;
    const selectedSummary = selectedCandidate
      ? `<div class="notice warn" data-review-selected-target="${selectedTargetId}"><strong>当前合并目标：#${selectedTargetId} · ${esc(candidateValue(selectedCandidate, "merchant", "未命名记录"))}</strong><br><span>${esc(candidateValue(selectedCandidate, "expense_date", "日期待确认"))} · ${formatCandidateAmount(selectedCandidate)} · ${esc(candidateValue(selectedCandidate, "status", ""))}</span></div>`
      : "";
    const progress = overflow
      ? sessionId
        ? `<div class="review-overflow-status"><strong>已顺序核对 ${reviewedCount || sessionCandidates.length} / ${blockingTotal}</strong><span>${overflowComplete ? "全部候选已读完，可作出处置" : "必须按顺序读取下一页"}</span></div>`
        : `<div class="notice warn"><strong>候选数量超过单页上限。</strong><br>必须启动一次 15 分钟的完整顺序核对；读完最后一页前不能确认或合并。</div>`
      : "";
    const nextAction = overflow
      ? !sessionId
        ? '<button class="btn" id="start-overflow-review">开始完整核对</button>'
        : !overflowComplete
          ? '<button class="btn" id="next-overflow-review">核对下一页</button>'
          : ""
      : "";
    const acknowledgement = blockingTotal
      ? `<label class="review-explicit-check"><input type="checkbox" data-review-duplicates> <span>我已逐项核对全部 ${blockingTotal} 个阻断候选，并会在下方明确选择“保留为独立报销”或合并到一个可修改候选。</span></label>`
      : "";
    duplicatePanel.innerHTML = `${progress}${selectedSummary}${noCandidates}<div class="review-overflow-actions">${nextAction}</div>${acknowledgement}`;
    $$('[name="review-merge-target"]', duplicatePanel).forEach((radio) => {
      radio.onchange = () => { selectedTargetId = Number(radio.value); updateActions(); };
    });
    const duplicateCheck = $('[data-review-duplicates]', duplicatePanel);
    if (duplicateCheck) duplicateCheck.onchange = updateActions;
    const start = $("#start-overflow-review", duplicatePanel);
    if (start) start.onclick = startOverflowReview;
    const next = $("#next-overflow-review", duplicatePanel);
    if (next) next.onclick = nextOverflowReview;
    updateActions();
  }

  function updateActions() {
    const ready = allRequiredChecksComplete(modal.root, review, overflowComplete);
    const hasBlocking = Number(review.blocking_total || 0) > 0;
    confirmButton.disabled = !ready;
    confirmButton.textContent = hasBlocking ? "确认保留为独立报销" : "确认进入待报销";
    const targetId = currentSelectedTarget(modal.root, selectedTargetId);
    mergeButton.disabled = !ready || !targetId;
    mergeButton.textContent = targetId ? `合并到 #${targetId}` : "合并到所选条目";
  }

  async function handleReviewError(error) {
    if (isInvoiceReviewConflict(error)) {
      modal.close();
      await refreshConflict(error, "核对内容已变化或核对会话已失效，已刷新，请重新开始。");
      return;
    }
    toast(error.message, "error");
  }

  async function startOverflowReview() {
    if (sessionBusy) return;
    sessionBusy = true;
    const done = busy("正在创建完整重复项核对会话…");
    try {
      const payload = await api(`/items/${detail.id}/duplicate-review-sessions`, {
        method: "POST",
        body: { expected_version: detail.version, review_token: review.token },
      });
      const page = reviewPage(payload);
      if (!page.sessionId) throw new Error("服务器未返回核对会话编号。");
      sessionId = page.sessionId;
      pageCandidates = page.candidates;
      sessionCandidates = [...page.candidates];
      page.candidates.forEach((candidate) => blockingIds.add(candidateId(candidate)));
      nextCursor = page.nextCursor;
      reviewedCount = page.reviewedCount || sessionCandidates.length;
      overflowToken = page.overflowToken;
      overflowComplete = !page.hasMore && Boolean(overflowToken);
      renderDuplicates();
    } catch (error) {
      await handleReviewError(error);
    } finally {
      sessionBusy = false;
      done();
    }
  }

  async function nextOverflowReview() {
    if (sessionBusy || !sessionId || !nextCursor) return;
    selectedTargetId = currentSelectedTarget(modal.root, selectedTargetId);
    sessionBusy = true;
    const done = busy("正在读取下一页重复候选…");
    try {
      const payload = await api(`/items/${detail.id}/duplicate-review-sessions/${encodeURIComponent(sessionId)}/next`, {
        method: "POST",
        body: { cursor: nextCursor },
      });
      const page = reviewPage(payload);
      pageCandidates = page.candidates;
      sessionCandidates.push(...page.candidates);
      page.candidates.forEach((candidate) => blockingIds.add(candidateId(candidate)));
      nextCursor = page.nextCursor;
      reviewedCount = page.reviewedCount || sessionCandidates.length;
      overflowToken = page.overflowToken || overflowToken;
      overflowComplete = !page.hasMore && Boolean(overflowToken);
      if (overflowComplete && selectedTargetId && !sessionCandidates.some((candidate) => candidateId(candidate) === selectedTargetId)) {
        selectedTargetId = null;
      }
      renderDuplicates();
    } catch (error) {
      await handleReviewError(error);
    } finally {
      sessionBusy = false;
      done();
    }
  }

  $$('[data-review-uncertainty], [data-review-recognition]', modal.root).forEach((input) => {
    input.onchange = updateActions;
  });

  confirmButton.onclick = async () => {
    if (!allRequiredChecksComplete(modal.root, review, overflowComplete)) return;
    const hasBlocking = Number(review.blocking_total || 0) > 0;
    const body = {
      expected_version: detail.version,
      review_token: review.token,
      duplicate_resolution: hasBlocking ? "keep_separate" : "none",
      acknowledged_uncertainty_ids: uncertaintyIds,
      acknowledged_duplicate_ids: hasBlocking && !review.duplicate_review_overflow
        ? [...blockingIds]
        : [],
    };
    if (overflowToken) body.overflow_review_token = overflowToken;
    const done = busy("正在提交已核对的报销条目…");
    try {
      const result = await api(`/items/${detail.id}/confirm`, { method: "POST", body });
      modal.close();
      toast(hasBlocking ? "已记录重复核对，条目保留为独立报销" : "条目已确认并进入待报销池");
      await onComplete({ action: "confirm", result });
    } catch (error) {
      await handleReviewError(error);
    } finally {
      done();
    }
  };

  mergeButton.onclick = async () => {
    const targetId = currentSelectedTarget(modal.root, selectedTargetId);
    if (!targetId || !allRequiredChecksComplete(modal.root, review, overflowComplete)) return;
    const mergeCandidates = review.duplicate_review_overflow
      ? sessionCandidates
      : (review.duplicate_candidates || []);
    const candidate = mergeCandidates
      .find((entry) => candidateId(entry) === targetId);
    if (!candidate || !candidateCanMerge(candidate)) return;
    const done = busy("正在按核对快照合并附件…");
    try {
      const target = (await api(`/items/${targetId}`)).item;
      if (candidateVersion(candidate) !== Number(target.version)) {
        const changed = new Error("合并目标已变化，请重新核对。");
        changed.code = "review_changed";
        throw changed;
      }
      const body = {
        source_version: detail.version,
        target_version: target.version,
        source_review_token: review.token,
      };
      const batchRef = target.batch_ref || candidateBatchRef(candidate);
      if (batchRef) body.expected_batch_version = batchRef.batch_version;
      if (overflowToken) body.overflow_review_token = overflowToken;
      const result = await api(`/items/${detail.id}/merge/${targetId}`, { method: "POST", body });
      modal.close();
      toast("文件已作为附件关联到已核对的已有条目");
      await onComplete({ action: "merge", result });
    } catch (error) {
      await handleReviewError(error);
    } finally {
      done();
    }
  };

  renderDuplicates();
  updateActions();
}
