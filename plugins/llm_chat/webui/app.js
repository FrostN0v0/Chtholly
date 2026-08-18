"use strict";

const API_ROOT = "/api/llm-chat/memes";
const STATUS_LABELS = {
  indexed: "Indexed",
  unindexed: "Needs tags",
  missing: "Missing file",
};

const state = {
  items: [],
  page: 1,
  pages: 1,
  pageSize: 24,
  status: "all",
  sort: "newest",
  query: "",
  selected: null,
  deleteTarget: null,
  uploads: [],
  listController: null,
};

const elements = {
  grid: document.querySelector("#meme-grid"),
  loading: document.querySelector("#loading-state"),
  empty: document.querySelector("#empty-state"),
  resultCount: document.querySelector("#result-count"),
  pageLabel: document.querySelector("#page-label"),
  previousPage: document.querySelector("#previous-page"),
  nextPage: document.querySelector("#next-page"),
  search: document.querySelector("#search-input"),
  sort: document.querySelector("#sort-select"),
  refresh: document.querySelector("#refresh-list"),
  editDialog: document.querySelector("#edit-dialog"),
  editForm: document.querySelector("#edit-form"),
  editImage: document.querySelector("#edit-image"),
  editFileName: document.querySelector("#edit-file-name"),
  editStatus: document.querySelector("#edit-status"),
  editTags: document.querySelector("#edit-tags"),
  saveTags: document.querySelector("#save-tags"),
  retagCurrent: document.querySelector("#retag-current"),
  uploadDialog: document.querySelector("#upload-dialog"),
  uploadForm: document.querySelector("#upload-form"),
  uploadInput: document.querySelector("#upload-files"),
  uploadTags: document.querySelector("#upload-tags"),
  autoTag: document.querySelector("#auto-tag"),
  dropZone: document.querySelector("#drop-zone"),
  uploadQueue: document.querySelector("#upload-queue"),
  startUpload: document.querySelector("#start-upload"),
  deleteDialog: document.querySelector("#delete-dialog"),
  deleteForm: document.querySelector("#delete-form"),
  deleteFileName: document.querySelector("#delete-file-name"),
  confirmDelete: document.querySelector("#confirm-delete"),
  toastStack: document.querySelector("#toast-stack"),
};

function createElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function setBusy(button, busy, busyLabel = "Working") {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.disabled = false;
  }
}

function showToast(title, message = "", error = false) {
  const toast = createElement("div", `toast${error ? " is-error" : ""}`);
  const copy = createElement("div");
  copy.append(createElement("strong", "", title));
  if (message) copy.append(createElement("span", "", message));
  toast.append(createElement("span"), copy);
  elements.toastStack.append(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

async function request(path = "", options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-Requested-With", "meme-webui");
  }
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok || payload?.success === false) {
    throw new Error(payload?.message || `Request failed with status ${response.status}`);
  }
  return payload;
}

function formatBytes(value) {
  if (value === null || value === undefined) return "No file";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

function splitTags(tags) {
  return String(tags || "")
    .split(/[，,、;；\n\r]+/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function statusBadge(item, overlay = false) {
  const badge = createElement(
    "span",
    `status-badge status-${item.status}${overlay ? " status-overlay" : ""}`,
    STATUS_LABELS[item.status] || item.status,
  );
  return badge;
}

function missingPreview() {
  const preview = createElement("div", "missing-preview");
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M4 5h16v14H4zM4 16l4-4 3 3 2-2 7 6M9 9h.01");
  icon.append(path);
  preview.append(icon, createElement("span", "", "The indexed file is no longer present."));
  return preview;
}

function renderTags(item) {
  const list = createElement("div", "tag-list");
  const tags = splitTags(item.tags);
  if (!tags.length) {
    list.append(createElement("span", "tag-placeholder", "No searchable text yet"));
    return list;
  }
  tags.slice(0, 6).forEach((tag) => list.append(createElement("span", "tag-pill", tag)));
  if (tags.length > 6) list.append(createElement("span", "tag-pill", `+${tags.length - 6}`));
  return list;
}

function cardButton(label, className, handler, disabled = false) {
  const button = createElement("button", `button ${className}`, label);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function renderCard(item) {
  const card = createElement("article", "meme-card");
  const media = createElement("div", "meme-media");
  if (item.image_url) {
    const image = createElement("img");
    image.src = item.image_url;
    image.alt = `Preview of ${item.file_name}`;
    image.loading = "lazy";
    image.addEventListener("error", () => {
      image.remove();
      media.append(missingPreview());
    }, { once: true });
    media.append(image);
  } else {
    media.append(missingPreview());
  }

  const content = createElement("div", "meme-content");
  const fileLine = createElement("div", "file-line");
  fileLine.append(
    createElement("strong", "", item.file_name),
    createElement("span", "", `${formatBytes(item.size_bytes)} · ${item.tag_count} tags`),
  );
  const statusLine = createElement("div", "card-status-line");
  statusLine.append(
    statusBadge(item),
    createElement("span", "index-state", item.embedding_ready ? "Vector ready" : "Keyword fallback"),
  );

  const actions = createElement("div", "meme-actions");
  actions.append(
    cardButton("Edit", "button-secondary", () => openEdit(item)),
    cardButton("Retag", "button-quiet", (event) => retagItem(item, event.currentTarget), item.status === "missing"),
    cardButton("Delete", "delete-button", () => openDelete(item)),
  );
  content.append(statusLine, fileLine, renderTags(item), actions);
  card.append(media, content);
  return card;
}

function renderCatalog() {
  elements.grid.replaceChildren(...state.items.map(renderCard));
  elements.resultCount.textContent = String(state.total || 0);
  elements.pageLabel.textContent = `Page ${state.page} of ${state.pages}`;
  elements.previousPage.disabled = state.page <= 1;
  elements.nextPage.disabled = state.page >= state.pages;
  elements.empty.hidden = state.items.length !== 0;
}

function renderStats(stats) {
  ["stored", "indexed", "unindexed", "missing"].forEach((key) => {
    const node = document.querySelector(`#stat-${key}`);
    if (node) node.textContent = String(stats?.[key] || 0);
  });
}

async function loadCatalog({ resetPage = false } = {}) {
  if (resetPage) state.page = 1;
  if (state.listController) state.listController.abort();
  state.listController = new AbortController();
  elements.loading.hidden = false;
  elements.grid.hidden = true;
  elements.empty.hidden = true;
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    status: state.status,
    sort: state.sort,
  });
  if (state.query) params.set("q", state.query);
  try {
    const payload = await request(`?${params}`, { signal: state.listController.signal });
    state.items = payload.items || [];
    state.total = payload.total || 0;
    state.page = payload.page || 1;
    state.pages = payload.pages || 1;
    renderStats(payload.stats);
    renderCatalog();
  } catch (error) {
    if (error.name !== "AbortError") {
      showToast("Catalog unavailable", error.message, true);
    }
  } finally {
    elements.loading.hidden = true;
    elements.grid.hidden = false;
  }
}

function openEdit(item) {
  state.selected = item;
  elements.editFileName.textContent = item.file_name;
  elements.editTags.value = item.tags || "";
  elements.editStatus.textContent = STATUS_LABELS[item.status] || item.status;
  elements.editStatus.className = `status-badge status-${item.status}`;
  elements.retagCurrent.disabled = item.status === "missing";
  if (item.image_url) {
    elements.editImage.hidden = false;
    elements.editImage.src = item.image_url;
  } else {
    elements.editImage.hidden = true;
    elements.editImage.removeAttribute("src");
  }
  elements.editDialog.showModal();
  window.setTimeout(() => elements.editTags.focus(), 0);
}

async function saveTags(event) {
  event.preventDefault();
  if (!state.selected) return;
  setBusy(elements.saveTags, true, "Saving");
  try {
    await request(`/${encodeURIComponent(state.selected.file_name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags: elements.editTags.value }),
    });
    elements.editDialog.close();
    showToast("Search index updated", state.selected.file_name);
    await loadCatalog();
  } catch (error) {
    showToast("Tags were not saved", error.message, true);
  } finally {
    setBusy(elements.saveTags, false);
  }
}

async function retagItem(item, button) {
  if (!item || item.status === "missing") return;
  setBusy(button, true, "Generating");
  try {
    const payload = await request(`/${encodeURIComponent(item.file_name)}/retag`, { method: "POST" });
    showToast("Tags generated", item.file_name);
    if (state.selected?.file_name === item.file_name) {
      state.selected = payload.item;
      elements.editTags.value = payload.item.tags || "";
    }
    await loadCatalog();
  } catch (error) {
    showToast("Automatic tagging failed", error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function openDelete(item) {
  state.deleteTarget = item;
  elements.deleteFileName.textContent = item.file_name;
  elements.deleteDialog.showModal();
}

async function deleteSelected(event) {
  event.preventDefault();
  if (!state.deleteTarget) return;
  setBusy(elements.confirmDelete, true, "Deleting");
  try {
    await request(`/${encodeURIComponent(state.deleteTarget.file_name)}`, { method: "DELETE" });
    showToast("Meme deleted", state.deleteTarget.file_name);
    elements.deleteDialog.close();
    state.deleteTarget = null;
    await loadCatalog();
  } catch (error) {
    showToast("Delete failed", error.message, true);
    await loadCatalog();
  } finally {
    setBusy(elements.confirmDelete, false);
  }
}

function clearUploads() {
  state.uploads.forEach((entry) => URL.revokeObjectURL(entry.previewUrl));
  state.uploads = [];
  elements.uploadInput.value = "";
  renderUploadQueue();
}

function addUploadFiles(files) {
  const acceptedTypes = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
  const existingKeys = new Set(state.uploads.map((entry) => `${entry.file.name}:${entry.file.size}:${entry.file.lastModified}`));
  Array.from(files).forEach((file) => {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (existingKeys.has(key)) return;
    if (!acceptedTypes.has(file.type)) {
      showToast("Unsupported image", file.name, true);
      return;
    }
    if (file.size > 6 * 1024 * 1024) {
      showToast("Image is larger than 6 MiB", file.name, true);
      return;
    }
    state.uploads.push({
      file,
      previewUrl: URL.createObjectURL(file),
      status: "Ready",
      statusClass: "",
    });
    existingKeys.add(key);
  });
  renderUploadQueue();
}

function removeUpload(entry) {
  URL.revokeObjectURL(entry.previewUrl);
  state.uploads = state.uploads.filter((candidate) => candidate !== entry);
  renderUploadQueue();
}

function renderUploadQueue() {
  const rows = state.uploads.map((entry) => {
    const row = createElement("div", "upload-item");
    const image = createElement("img");
    image.src = entry.previewUrl;
    image.alt = "";
    const copy = createElement("div", "upload-item-copy");
    copy.append(
      createElement("strong", "", entry.file.name),
      createElement("span", "", formatBytes(entry.file.size)),
    );
    const action = entry.status === "Ready"
      ? cardButton("Remove", "button-quiet", () => removeUpload(entry))
      : createElement("span", `upload-status ${entry.statusClass}`, entry.status);
    row.append(image, copy, action);
    return row;
  });
  elements.uploadQueue.replaceChildren(...rows);
}

async function uploadSelected(event) {
  event.preventDefault();
  if (!state.uploads.length) {
    showToast("Choose at least one image", "No files are queued.", true);
    return;
  }
  const sharedTags = elements.uploadTags.value.trim();
  if (!sharedTags && !elements.autoTag.checked) {
    showToast("Tags are required", "Enter shared tags or enable automatic tagging.", true);
    return;
  }
  setBusy(elements.startUpload, true, "Uploading");
  let failures = 0;
  for (const entry of state.uploads) {
    entry.status = "Uploading";
    entry.statusClass = "";
    renderUploadQueue();
    const form = new FormData();
    form.append("file", entry.file, entry.file.name);
    form.append("tags", sharedTags);
    form.append("auto_tag", String(elements.autoTag.checked));
    try {
      const payload = await request("", { method: "POST", body: form });
      entry.status = payload.status === "duplicate" ? "Already stored" : "Added";
      entry.statusClass = "is-success";
    } catch (error) {
      failures += 1;
      entry.status = error.message;
      entry.statusClass = "is-error";
    }
    renderUploadQueue();
  }
  setBusy(elements.startUpload, false);
  await loadCatalog({ resetPage: true });
  if (failures) {
    showToast("Upload completed with errors", `${failures} file(s) were rejected.`, true);
  } else {
    showToast("Upload complete", `${state.uploads.length} file(s) processed.`);
    window.setTimeout(() => {
      elements.uploadDialog.close();
      clearUploads();
      elements.uploadTags.value = "";
    }, 450);
  }
}

function debounce(callback, delay) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => callback(...args), delay);
  };
}

function bindEvents() {
  document.querySelector("#open-upload").addEventListener("click", () => elements.uploadDialog.showModal());
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => document.querySelector(`#${button.dataset.closeDialog}`).close());
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });
  document.querySelectorAll(".filter-chip").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip").forEach((chip) => chip.classList.remove("is-active"));
      button.classList.add("is-active");
      state.status = button.dataset.status;
      loadCatalog({ resetPage: true });
    });
  });
  elements.search.addEventListener("input", debounce(() => {
    state.query = elements.search.value.trim();
    loadCatalog({ resetPage: true });
  }, 250));
  elements.sort.addEventListener("change", () => {
    state.sort = elements.sort.value;
    loadCatalog({ resetPage: true });
  });
  elements.refresh.addEventListener("click", () => loadCatalog());
  elements.previousPage.addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    loadCatalog();
  });
  elements.nextPage.addEventListener("click", () => {
    state.page = Math.min(state.pages, state.page + 1);
    loadCatalog();
  });
  elements.editForm.addEventListener("submit", saveTags);
  elements.retagCurrent.addEventListener("click", (event) => retagItem(state.selected, event.currentTarget));
  elements.deleteForm.addEventListener("submit", deleteSelected);
  elements.uploadForm.addEventListener("submit", uploadSelected);
  elements.uploadInput.addEventListener("change", () => addUploadFiles(elements.uploadInput.files));
  ["dragenter", "dragover"].forEach((name) => {
    elements.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      elements.dropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    elements.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      elements.dropZone.classList.remove("is-dragging");
    });
  });
  elements.dropZone.addEventListener("drop", (event) => addUploadFiles(event.dataTransfer.files));
  elements.uploadDialog.addEventListener("close", () => {
    if (!state.uploads.some((entry) => entry.status === "Uploading")) clearUploads();
  });
}

bindEvents();
loadCatalog();
