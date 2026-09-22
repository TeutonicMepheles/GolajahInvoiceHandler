import { state } from "../../core/state.js";
import { api } from "../../shared/api.js";
import { $, $$, esc } from "../../shared/dom.js";
import { FormDataForm } from "../../shared/forms.js";
import { busy, emptyState, showModal, toast } from "../../shared/ui.js";
const fmtQuoteMoney = (value) => {
  const number = Number(value || 0);
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: Number.isInteger(number) ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(number)} 元`;
};

function nextQuotationNodeId(prefix) {
  state.quotationNodeSequence += 1;
  return `${prefix}-${state.quotationNodeSequence}`;
}

function distributeQuotationAmounts(total, count, roundingUnit) {
  if (!count) return [];
  const totalUnits = Math.round(Number(total) / Number(roundingUnit));
  const baseUnits = Math.floor(totalUnits / count);
  const higherCount = totalUnits % count;
  return Array.from(
    { length: count },
    (_, index) => (baseUnits + (index < higherCount ? 1 : 0)) * Number(roundingUnit),
  );
}

function redistributeQuotationDetails(result) {
  const itemAmounts = distributeQuotationAmounts(result.basic_cost, result.items.length, result.rounding_unit);
  result.items.forEach((item, index) => {
    item.index = index + 1;
    item.amount = itemAmounts[index];
    const childAmounts = distributeQuotationAmounts(item.amount, item.children.length, result.rounding_unit);
    item.children.forEach((child, childIndex) => {
      child.index = childIndex + 1;
      child.amount = childAmounts[childIndex];
    });
  });
}

function initializeQuotationDetails(result) {
  state.quotationNodeSequence = 0;
  result.items = result.items.map((item, index) => ({
    ...item,
    id: nextQuotationNodeId("item"),
    name: item.name || `开发条目 ${index + 1}`,
    description: item.description || "",
    children: [],
  }));
  redistributeQuotationDetails(result);
  return result;
}

function findQuotationNode(result, nodeId) {
  for (const item of result.items) {
    if (item.id === nodeId) return item;
    const child = item.children.find((entry) => entry.id === nodeId);
    if (child) return child;
  }
  return null;
}

function quotationNodeName(node, fallback) {
  return String(node?.name || "").trim() || fallback;
}

function quotationNodeDescription(node) {
  return String(node?.description || "").trim();
}

function quotationDistribution(result) {
  const groups = new Map();
  result.items.forEach((item) => groups.set(item.amount, (groups.get(item.amount) || 0) + 1));
  return [...groups.entries()]
    .sort(([left], [right]) => left - right)
    .map(([amount, count]) => `${count} 项 × ${fmtQuoteMoney(amount)}`)
    .join("，");
}

export async function renderQuotation(pageVersion = state.pageVersion) {
  state.quotation = null;
  state.quotationNodeSequence = 0;
  state.quotationDescriptionEnabled = false;
  state.quotationExportMeta = {
    title: "项目开发报价单",
    client_name: "",
    project_manager: "",
    quote_date: new Date().toISOString().slice(0, 10),
  };
  $("#page-content").innerHTML = `<div class="quote-layout">
    <section class="card quote-form-card">
      <div class="section-head"><div><h2>测算条件</h2><p>先确认金额上限和费用比例，再寻找最接近上限的整洁方案</p></div><span class="badge info">本地计算</span></div>
      <form id="quotation-form" class="quote-form">
        <div class="field full"><label>合同金额上限（元）</label><input name="upper_limit" type="number" min="1" max="1000000000" step="1" value="47500" required><small>最终合同总额不会超过该金额</small></div>
        <div class="field full"><label>增值税计算口径</label><select name="vat_mode"><option value="inclusive">含税价倒算（匹配现有报价单）</option><option value="direct">按合同总额直接计提</option></select></div>
        <div class="field"><label>增值税率（%）</label><input name="vat_rate" type="number" min="0" max="100" step="0.01" value="3" required></div>
        <div class="field"><label>附加税率（%）</label><input name="surcharge_rate" type="number" min="0" max="100" step="0.01" value="12" required></div>
        <div class="field"><label>管理费比例（%）</label><input name="management_rate" type="number" min="0" max="100" step="0.01" value="16" required></div>
        <div class="field"><label>大类条目数</label><input name="category_count" type="number" min="1" max="100" step="1" value="8" required></div>
        <div class="field full"><label>基础费用取整粒度</label><select name="rounding_unit"><option value="100">整百元</option><option value="500">整五百元</option><option value="1000">整千元</option></select><small>系统保证每个开发条目都是该粒度的整数倍</small></div>
        <button class="btn primary quote-submit" type="submit">重新测算</button>
      </form>
      <div class="quote-method">
        <strong>计算顺序</strong>
        <span>① 总额不超过上限</span><span>② 税费四舍五入到元</span><span>③ 管理费舍去小数到元</span><span>④ 反算基础费用并搜索整洁值</span>
      </div>
    </section>
    <section id="quotation-result" class="quote-result">${emptyState("¥", "正在生成初始方案", "系统会自动用默认比例完成一次测算。")}</section>
  </div>`;
  const form = $("#quotation-form");
  form.onsubmit = async (event) => { event.preventDefault(); await calculateQuotation(form, pageVersion); };
  await calculateQuotation(form, pageVersion);
}

async function calculateQuotation(form, pageVersion = state.pageVersion) {
  const button = $('.quote-submit', form);
  button.disabled = true;
  button.textContent = "正在反算…";
  try {
    const result = await api("/quotations/calculate", {
      method: "POST",
      body: Object.fromEntries(FormDataForm(form)),
    });
    if (pageVersion !== state.pageVersion) return;
    state.quotation = initializeQuotationDetails(result);
    renderQuotationResult();
  } catch (error) {
    if (pageVersion !== state.pageVersion) return;
    $("#quotation-result").innerHTML = emptyState("!", "当前条件无法形成报价", error.message);
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "重新测算";
  }
}

function renderQuotationResult(focusNodeId = null) {
  const result = state.quotation;
  if (!result) return;
  const root = $("#quotation-result");
  root.innerHTML = quotationResult(result);
  $("#copy-quotation", root).onclick = () => copyQuotation(result);
  $("#export-quotation-docx", root).onclick = () => openQuotationExportModal(result);
  $("#add-quotation-item", root).onclick = addQuotationItem;
  $("#toggle-quotation-description", root).onclick = toggleQuotationDescriptions;
  $$('[data-add-quotation-child]', root).forEach((button) => {
    button.onclick = () => addQuotationChild(button.dataset.addQuotationChild);
  });
  $$('[data-remove-quotation-item]', root).forEach((button) => {
    button.onclick = () => removeQuotationItem(button.dataset.removeQuotationItem);
  });
  $$('[data-remove-quotation-child]', root).forEach((button) => {
    button.onclick = () => removeQuotationChild(button.dataset.parentId, button.dataset.removeQuotationChild);
  });
  $$('[data-quotation-name]', root).forEach((input) => {
    input.oninput = () => {
      const node = findQuotationNode(result, input.dataset.quotationName);
      if (node) node.name = input.value;
    };
  });
  $$('[data-quotation-description]', root).forEach((input) => {
    input.oninput = () => {
      const node = findQuotationNode(result, input.dataset.quotationDescription);
      if (node) node.description = input.value;
    };
  });
  if (focusNodeId) {
    requestAnimationFrame(() => {
      const input = $(`[data-quotation-name="${focusNodeId}"]`, root);
      input?.focus();
      input?.select();
    });
  }
}

function toggleQuotationDescriptions() {
  state.quotationDescriptionEnabled = !state.quotationDescriptionEnabled;
  renderQuotationResult();
  if (state.quotationDescriptionEnabled) {
    requestAnimationFrame(() => {
      $('[data-quotation-description]', $("#quotation-result"))?.focus();
    });
  }
}

function addQuotationItem() {
  const result = state.quotation;
  const newCount = result.items.length + 1;
  const maximumCount = Math.min(100, Math.floor(result.basic_cost / result.rounding_unit));
  if (newCount > maximumCount) {
    toast(`当前基础费用最多可分为 ${maximumCount} 个非零整洁条目`, "error");
    return;
  }
  const preview = distributeQuotationAmounts(result.basic_cost, newCount, result.rounding_unit);
  const childWouldBeZero = result.items.some(
    (item, index) => item.children.length && preview[index] < item.children.length * result.rounding_unit,
  );
  if (childWouldBeZero) {
    toast("现有子项目数量较多，继续新增条目会产生零金额子项目", "error");
    return;
  }
  const item = {
    id: nextQuotationNodeId("item"),
    name: "",
    description: "",
    amount: 0,
    children: [],
  };
  result.items.push(item);
  redistributeQuotationDetails(result);
  renderQuotationResult(item.id);
}

function removeQuotationItem(itemId) {
  const result = state.quotation;
  if (result.items.length <= 1) {
    toast("基础开发费用至少需要保留一个条目", "error");
    return;
  }
  result.items = result.items.filter((item) => item.id !== itemId);
  redistributeQuotationDetails(result);
  renderQuotationResult();
}

function addQuotationChild(itemId) {
  const result = state.quotation;
  const item = result.items.find((entry) => entry.id === itemId);
  if (!item) return;
  const maximumCount = Math.floor(item.amount / result.rounding_unit);
  if (item.children.length + 1 > maximumCount) {
    toast(`该条目最多可分为 ${maximumCount} 个非零整洁子项目`, "error");
    return;
  }
  const child = {
    id: nextQuotationNodeId("child"),
    name: "",
    description: item.children.length ? "" : item.description,
    amount: 0,
  };
  if (!item.children.length) item.description = "";
  item.children.push(child);
  redistributeQuotationDetails(result);
  renderQuotationResult(child.id);
}

function removeQuotationChild(itemId, childId) {
  const result = state.quotation;
  const item = result.items.find((entry) => entry.id === itemId);
  if (!item) return;
  const removedChild = item.children.find((child) => child.id === childId);
  item.children = item.children.filter((child) => child.id !== childId);
  if (!item.children.length && !quotationNodeDescription(item) && removedChild) {
    item.description = removedChild.description || "";
  }
  redistributeQuotationDetails(result);
  renderQuotationResult();
}

function quotationResult(result) {
  const vatMode = result.vat_mode === "inclusive" ? "含税价倒算" : "按合同总额直接计提";
  const distribution = quotationDistribution(result);
  const amounts = result.items.map((item) => item.amount);
  const childCount = result.items.reduce((sum, item) => sum + item.children.length, 0);
  const descriptionsEnabled = state.quotationDescriptionEnabled;
  const descriptionInput = (node, label) => `<textarea class="quote-description-input" data-quotation-description="${node.id}" maxlength="1000" rows="2" placeholder="请输入说明内容" aria-label="${esc(label)}说明">${esc(node.description)}</textarea>`;
  const itemRows = result.items.map((item, index) => {
    const itemNumber = index + 1;
    const itemName = quotationNodeName(item, `开发条目 ${itemNumber}`);
    const childRows = item.children.map((child, childIndex) => `<tr class="quote-child-row">
      <td class="quote-index-column">${itemNumber}.${childIndex + 1}</td>
      <td class="quote-name-column"><div class="quote-name-cell child"><span aria-hidden="true">↳</span><input class="quote-name-input" data-quotation-name="${child.id}" value="${esc(child.name)}" placeholder="请输入子项目名称" aria-label="${esc(itemName)}的第 ${childIndex + 1} 个子项目名称"></div></td>
      ${descriptionsEnabled ? `<td class="quote-description-column">${descriptionInput(child, `${itemName}的第 ${childIndex + 1} 个子项目`)}</td>` : ""}
      <td class="quote-price-column">${fmtQuoteMoney(child.amount)}</td>
      <td class="quote-actions-column"><button class="btn ghost small quote-delete-action" type="button" data-parent-id="${item.id}" data-remove-quotation-child="${child.id}">删除</button></td>
    </tr>`).join("");
    return `<tr class="quote-item-row">
      <td class="quote-index-column">${itemNumber}</td>
      <td class="quote-name-column"><input class="quote-name-input" data-quotation-name="${item.id}" value="${esc(item.name)}" placeholder="请输入开发条目名称" aria-label="第 ${itemNumber} 个开发条目名称"></td>
      ${descriptionsEnabled ? `<td class="quote-description-column">${item.children.length ? '<span class="quote-description-hint">请在子项目行填写说明</span>' : descriptionInput(item, `第 ${itemNumber} 个开发条目`)}</td>` : ""}
      <td class="quote-price-column">${fmtQuoteMoney(item.amount)}${item.children.length ? '<small class="quote-subtotal-label">小计</small>' : ""}</td>
      <td class="quote-actions-column"><div class="quote-row-actions"><button class="btn ghost small" type="button" data-add-quotation-child="${item.id}">＋子项目</button><button class="btn ghost small quote-delete-action" type="button" data-remove-quotation-item="${item.id}" ${result.items.length === 1 ? "disabled" : ""}>删除</button></div></td>
    </tr>${childRows}`;
  }).join("");
  return `<div class="quote-result-stack">
    <section class="card quote-hero">
      <div><span class="quote-kicker">推荐合同总额</span><strong>${fmtQuoteMoney(result.contract_total)}</strong><p>低于上限 ${fmtQuoteMoney(result.gap_to_limit)} · ${vatMode}</p></div>
      <div class="quote-hero-actions"><button class="btn" id="copy-quotation">复制报价明细</button><button class="btn" id="export-quotation-docx">导出报价单.docx</button></div>
    </section>
    <div class="quote-metrics">
      <div class="card"><span>基础开发费用</span><strong>${fmtQuoteMoney(result.basic_cost)}</strong><small>${result.items.length} 个条目${childCount ? ` · ${childCount} 个子项目` : ""} · ${distribution}</small></div>
      <div class="card"><span>其他费用</span><strong>${fmtQuoteMoney(result.other_cost)}</strong><small>增值税、附加税与管理费</small></div>
      <div class="card"><span>条目金额范围</span><strong>${fmtQuoteMoney(Math.min(...amounts))}–${fmtQuoteMoney(Math.max(...amounts))}</strong><small>平均 ${fmtQuoteMoney(result.basic_cost / result.items.length)}</small></div>
    </div>
    <section class="card quote-detail-card">
      <div class="section-head quote-detail-head"><div><h2>基础开发费用明细</h2><p>金额按 ${fmtQuoteMoney(result.rounding_unit)} 粒度均衡分配；无法等分时允许不等额，条目间最多相差一个取整单位</p></div><div class="quote-detail-actions"><span class="badge ok">合计 ${fmtQuoteMoney(result.basic_cost)}</span><button class="btn small" id="toggle-quotation-description" type="button" aria-pressed="${descriptionsEnabled}">${descriptionsEnabled ? "－ 移除说明列" : "＋ 新增说明列"}</button><button class="btn small" id="add-quotation-item" type="button">＋ 新增条目</button></div></div>
      <div class="quote-table-wrap"><table class="quote-table quote-editable-table ${descriptionsEnabled ? "has-description" : ""}"><thead><tr><th class="quote-index-column">序号</th><th class="quote-name-column">板块 / 项目</th>${descriptionsEnabled ? '<th class="quote-description-column">说明</th>' : ""}<th class="quote-price-column">价格</th><th class="quote-actions-column">操作</th></tr></thead><tbody>${itemRows}</tbody></table></div>
    </section>
    <section class="card quote-detail-card">
      <div class="section-head"><div><h2>其他费用明细</h2><p>小数与零散金额保留在其他费用中</p></div><span class="badge warn">合计 ${fmtQuoteMoney(result.other_cost)}</span></div>
      <div class="quote-fee-list">
        <div><span>增值税 <small>${result.rates.vat}% · 理论 ${fmtQuoteMoney(result.theoretical.vat)}</small></span><strong>${fmtQuoteMoney(result.fees.vat)}</strong></div>
        <div><span>附加税 <small>增值税的 ${result.rates.surcharge}% · 理论 ${fmtQuoteMoney(result.theoretical.surcharge)}</small></span><strong>${fmtQuoteMoney(result.fees.surcharge)}</strong></div>
        <div><span>管理费 <small>合同总额的 ${result.rates.management}% · 理论 ${fmtQuoteMoney(result.theoretical.management)}</small></span><strong>${fmtQuoteMoney(result.fees.management)}</strong></div>
        <div class="quote-total-row"><span>合同总额</span><strong>${fmtQuoteMoney(result.contract_total)}</strong></div>
      </div>
    </section>
  </div>`;
}

function openQuotationExportModal(result) {
  const meta = state.quotationExportMeta;
  const modal = showModal("导出报价单.docx", `<div class="form-grid" id="quotation-export-form">
    <div class="field full"><label>报价单标题</label><input name="title" maxlength="60" value="${esc(meta.title)}" required></div>
    <div class="field full"><label>需求方名称（可选）</label><input name="client_name" maxlength="80" value="${esc(meta.client_name)}" placeholder="填写后用于开头的需求说明"></div>
    <div class="field"><label>项目负责人（可选）</label><input name="project_manager" maxlength="40" value="${esc(meta.project_manager)}"></div>
    <div class="field"><label>报价日期</label><input name="quote_date" type="date" value="${esc(meta.quote_date)}" required></div>
    <div class="field full"><div class="notice success">导出文件将沿用同济 AIGC 工具链报价单的内容格式，并保留末页联系信息与机构标志落款。当前将导出${state.quotationDescriptionEnabled ? "“板块、项目、说明、价格”四列" : "“板块、项目、价格”三列"}。</div></div>
  </div>`, '<button class="btn" data-modal-close>取消</button><button class="btn primary" id="confirm-quotation-export">生成并下载</button>');
  $("#confirm-quotation-export", modal.root).onclick = async () => {
    const form = $("#quotation-export-form", modal.root);
    const fields = Object.fromEntries(FormDataForm(form));
    if (!String(fields.title || "").trim()) {
      toast("请填写报价单标题", "error");
      return;
    }
    state.quotationExportMeta = { ...fields };
    const done = busy("正在生成 Word 报价单…");
    try {
      const response = await fetch("/api/quotations/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...fields,
          include_descriptions: state.quotationDescriptionEnabled,
          quotation: result,
        }),
      });
      if (!response.ok) {
        const payload = (response.headers.get("content-type") || "").includes("application/json")
          ? await response.json()
          : null;
        throw new Error(payload?.message || `导出失败（${response.status}）`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const safeTitle = String(fields.title).replace(/[<>:"/\\|?*]+/g, "_").replace(/[. ]+$/g, "") || "报价单";
      link.href = url;
      link.download = `${safeTitle}.docx`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      modal.close();
      toast("报价单 Word 文件已生成");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      done();
    }
  };
}

async function copyQuotation(result) {
  const vatMode = result.vat_mode === "inclusive" ? "含税价倒算" : "按合同总额直接计提";
  const lines = [
    "报价测算明细",
    `合同金额上限：${fmtQuoteMoney(result.upper_limit)}`,
    `推荐合同总额：${fmtQuoteMoney(result.contract_total)}（距上限 ${fmtQuoteMoney(result.gap_to_limit)}）`,
    "",
    "一、基础开发费用",
    ...result.items.flatMap((item, index) => {
      const itemNumber = index + 1;
      const itemName = quotationNodeName(item, `开发条目 ${itemNumber}`);
      const descriptionLine = (node, indent) => {
        const description = quotationNodeDescription(node);
        return state.quotationDescriptionEnabled && description
          ? [`${indent}说明：${description.replaceAll("\n", "；")}`]
          : [];
      };
      if (!item.children.length) {
        return [
          `${itemNumber}. ${itemName}：${fmtQuoteMoney(item.amount)}`,
          ...descriptionLine(item, "  "),
        ];
      }
      return [
        `${itemNumber}. ${itemName}（小计）：${fmtQuoteMoney(item.amount)}`,
        ...item.children.flatMap((child, childIndex) => [
          `  ${itemNumber}.${childIndex + 1} ${quotationNodeName(child, `子项目 ${childIndex + 1}`)}：${fmtQuoteMoney(child.amount)}`,
          ...descriptionLine(child, "    "),
        ]),
      ];
    }),
    `基础开发费用合计：${fmtQuoteMoney(result.basic_cost)}`,
    "",
    "二、其他费用",
    `增值税（${result.rates.vat}%，${vatMode}）：${fmtQuoteMoney(result.fees.vat)}`,
    `附加税（增值税的 ${result.rates.surcharge}%）：${fmtQuoteMoney(result.fees.surcharge)}`,
    `管理费（合同总额的 ${result.rates.management}%）：${fmtQuoteMoney(result.fees.management)}`,
    `其他费用合计：${fmtQuoteMoney(result.other_cost)}`,
    `总计：${fmtQuoteMoney(result.contract_total)}`,
  ];
  const text = lines.join("\n");
  try {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (_clipboardError) {
        copied = false;
      }
    }
    if (!copied) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.append(textarea);
      textarea.focus();
      textarea.select();
      copied = document.execCommand("copy");
      textarea.remove();
    }
    if (!copied) throw new Error("copy_failed");
    toast("报价明细已复制");
  } catch (_error) {
    toast("复制失败，请手工选择明细", "error");
  }
}
