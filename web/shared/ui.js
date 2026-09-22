import { $, $$, esc } from "./dom.js";

export function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toast-root").append(node);
  setTimeout(() => node.remove(), 3800);
}

export function busy(message = "正在处理…") {
  const node = document.createElement("div");
  node.className = "busy-overlay";
  node.innerHTML = `<div class="busy-box"><span class="spinner"></span>${esc(message)}</div>`;
  document.body.append(node);
  return () => node.remove();
}

export function showModal(title, bodyHtml, actionsHtml = "") {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop"><div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head"><h2>${esc(title)}</h2><button class="modal-close" aria-label="关闭">×</button></div>
    <div class="modal-body">${bodyHtml}</div>
    <div class="modal-actions">${actionsHtml || '<button class="btn" data-modal-close>关闭</button>'}</div>
  </div></div>`;
  const close = () => { root.innerHTML = ""; };
  $(".modal-close", root).onclick = close;
  $$('[data-modal-close]', root).forEach((button) => button.onclick = close);
  $(".modal-backdrop", root).addEventListener("click", (event) => {
    if (event.target === event.currentTarget) close();
  });
  return { root, close };
}

export function emptyState(icon, title, text, action = "") {
  return `<div class="card empty"><div class="empty-icon">${icon}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}
