"use strict";

const API_ROOT = "/api/llm-chat/memes";
const STATUS_LABELS = {
  indexed: "已索引",
  unindexed: "待标注",
  missing: "文件缺失",
};
const EMPTY_METADATA = {
  text: "",
  meaning: "",
  use_when: [],
  avoid_when: [],
  tags: [],
};
const METADATA_FIELDS = [
  {
    key: "text",
    label: "图片原文",
    kind: "textarea",
    rows: 2,
    maxLength: 120,
    placeholder: "没有清晰文字可留空",
    hint: "按图片内容填写，保留原有标点。",
  },
  {
    key: "meaning",
    label: "表达含义",
    kind: "textarea",
    rows: 3,
    maxLength: 160,
    placeholder: "这张表情实际表达什么语气和意思",
    hint: "描述整张图的实际含义，不要只写人物外观。",
  },
  {
    key: "use_when",
    label: "适用场景",
    kind: "list",
    maxItems: 4,
    maxLength: 90,
    placeholder: "例如：朋友恶作剧后吐槽",
    addLabel: "添加适用场景",
  },
  {
    key: "avoid_when",
    label: "避免使用",
    kind: "list",
    maxItems: 4,
    maxLength: 90,
    placeholder: "例如：早上好",
    addLabel: "添加避用短句",
  },
  {
    key: "tags",
    label: "检索标签",
    kind: "list",
    maxItems: 12,
    maxLength: 40,
    placeholder: "例如：嗔怪",
    addLabel: "添加标签",
  },
];
const ERROR_MESSAGES = {
  catalog_unavailable: "表情库暂时无法读取",
  meme_not_found: "没有找到对应的表情记录",
  invalid_tags: "标签内容无效",
  invalid_json: "请求中的 JSON 无效",
  request_too_large: "标签内容超过大小限制",
  upload_rejected: "图片格式、大小或标签不符合要求",
  upload_failed: "图片上传失败",
  storage_inconsistent: "图片存储状态不一致",
  tag_update_failed: "标签保存失败",
  image_missing: "图片文件已不存在",
  image_unavailable: "图片无效或超过大小限制",
  tagging_failed: "自动标注失败",
  delete_failed: "删除表情失败",
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
  editMetadata: document.querySelector("#edit-metadata"),
  saveTags: document.querySelector("#save-tags"),
  retagCurrent: document.querySelector("#retag-current"),
  uploadDialog: document.querySelector("#upload-dialog"),
  uploadForm: document.querySelector("#upload-form"),
  uploadInput: document.querySelector("#upload-files"),
  uploadMetadata: document.querySelector("#upload-metadata"),
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

function setBusy(button, busy, busyLabel = "处理中") {
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
    throw new Error(ERROR_MESSAGES[payload?.code] || payload?.message || `请求失败，状态码 ${response.status}`);
  }
  return payload;
}

function formatBytes(value) {
  if (value === null || value === undefined) return "文件不存在";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

function displayTags(item) {
  return Array.isArray(item.display_tags)
    ? item.display_tags.filter((tag) => typeof tag === "string" && tag.trim())
    : [];
}

function normalizeMetadata(value) {
  const payload = value && typeof value === "object" && !Array.isArray(value) ? value : EMPTY_METADATA;
  return {
    text: typeof payload.text === "string" ? payload.text : "",
    meaning: typeof payload.meaning === "string" ? payload.meaning : "",
    use_when: Array.isArray(payload.use_when) ? payload.use_when.filter((entry) => typeof entry === "string") : [],
    avoid_when: Array.isArray(payload.avoid_when) ? payload.avoid_when.filter((entry) => typeof entry === "string") : [],
    tags: Array.isArray(payload.tags) ? payload.tags.filter((entry) => typeof entry === "string") : [],
  };
}

function metadataFieldId(container, key) {
  return `${container.id}-${key.replaceAll("_", "-")}`;
}

function metadataListEntries(container, key) {
  return Array.from(container.querySelectorAll(`[data-metadata-entry="${key}"]`));
}

function updateMetadataListState(container, field) {
  const entries = metadataListEntries(container, field.key);
  const count = container.querySelector(`[data-metadata-count="${field.key}"]`);
  const add = container.querySelector(`[data-metadata-add="${field.key}"]`);
  if (count) count.textContent = `${entries.length} / ${field.maxItems}`;
  if (add) add.disabled = entries.length >= field.maxItems;
}

function appendMetadataEntry(container, field, value = "", focus = false) {
  const list = container.querySelector(`[data-metadata-list="${field.key}"]`);
  if (!list) return;
  const existing = metadataListEntries(container, field.key);
  const lastInput = existing.at(-1);
  if (!value && lastInput && !lastInput.value.trim()) {
    if (focus) lastInput.focus();
    return;
  }
  if (existing.length >= field.maxItems) return;

  const row = createElement("div", "metadata-entry");
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = field.maxLength;
  input.placeholder = field.placeholder;
  input.value = value;
  input.dataset.metadataEntry = field.key;
  input.setAttribute("aria-label", `${field.label}条目`);
  const remove = createElement("button", "metadata-entry-remove", "移除");
  remove.type = "button";
  remove.addEventListener("click", () => {
    row.remove();
    updateMetadataListState(container, field);
  });
  row.append(input, remove);
  list.append(row);
  updateMetadataListState(container, field);
  if (focus) input.focus();
}

function renderMetadataEditor(container, value = EMPTY_METADATA) {
  const metadata = normalizeMetadata(value);
  const fields = METADATA_FIELDS.map((field) => {
    if (field.kind === "textarea") {
      const wrapper = createElement("label", "metadata-field");
      const input = document.createElement("textarea");
      input.id = metadataFieldId(container, field.key);
      input.rows = field.rows;
      input.maxLength = field.maxLength;
      input.placeholder = field.placeholder;
      input.value = metadata[field.key];
      input.dataset.metadataScalar = field.key;
      wrapper.htmlFor = input.id;
      wrapper.append(
        createElement("span", "metadata-field-label", field.label),
        input,
        createElement("small", "metadata-field-hint", field.hint),
      );
      return wrapper;
    }

    const wrapper = createElement("section", "metadata-field metadata-list-field");
    wrapper.setAttribute("aria-label", field.label);
    const heading = createElement("div", "metadata-field-heading");
    const count = createElement("span", "metadata-entry-count", `0 / ${field.maxItems}`);
    count.dataset.metadataCount = field.key;
    heading.append(createElement("strong", "metadata-field-label", field.label), count);
    const list = createElement("div", "metadata-entry-list");
    list.dataset.metadataList = field.key;
    const add = createElement("button", "metadata-entry-add", `＋ ${field.addLabel}`);
    add.type = "button";
    add.dataset.metadataAdd = field.key;
    add.addEventListener("click", () => appendMetadataEntry(container, field, "", true));
    wrapper.append(heading, list, add);
    return wrapper;
  });
  container.replaceChildren(...fields);
  for (const field of METADATA_FIELDS.filter((candidate) => candidate.kind === "list")) {
    metadata[field.key].forEach((entry) => appendMetadataEntry(container, field, entry));
    updateMetadataListState(container, field);
  }
}

function readMetadataEditor(container, { optional = false } = {}) {
  const payload = { text: "", meaning: "", use_when: [], avoid_when: [], tags: [] };
  container.querySelectorAll("[data-metadata-scalar]").forEach((input) => {
    payload[input.dataset.metadataScalar] = input.value.trim();
  });
  for (const field of METADATA_FIELDS.filter((candidate) => candidate.kind === "list")) {
    const seen = new Set();
    payload[field.key] = metadataListEntries(container, field.key)
      .map((entry) => entry.value.trim())
      .filter((entry) => {
        if (!entry || seen.has(entry)) return false;
        seen.add(entry);
        return true;
      });
  }
  const hasPositiveContent = Boolean(
    payload.text || payload.meaning || payload.use_when.length || payload.tags.length,
  );
  const hasAnyContent = hasPositiveContent || payload.avoid_when.length;
  if (!hasPositiveContent) {
    if (optional && !hasAnyContent) return "";
    throw new Error("至少填写图片原文、表达含义、适用场景或检索标签中的一项");
  }
  return JSON.stringify(payload);
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
  preview.append(icon, createElement("span", "", "索引对应的图片文件已不存在。"));
  return preview;
}

function renderTags(item) {
  const list = createElement("div", "tag-list");
  const tags = displayTags(item);
  if (!tags.length) {
    list.append(createElement("span", "tag-placeholder", "暂无可检索标签"));
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
    image.alt = `${item.file_name} 的预览图`;
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
    createElement("span", "", `${formatBytes(item.size_bytes)} · ${item.tag_count} 个标签`),
  );
  const statusLine = createElement("div", "card-status-line");
  statusLine.append(statusBadge(item));
  if (item.status === "indexed") {
    statusLine.append(createElement("span", "index-state", item.embedding_ready ? "语义检索可用" : "关键词检索"));
  }

  const actions = createElement("div", "meme-actions");
  actions.append(
    cardButton("编辑", "button-secondary", () => openEdit(item)),
    cardButton("重标", "button-quiet", (event) => retagItem(item, event.currentTarget), item.status === "missing"),
    cardButton("删除", "delete-button", () => openDelete(item)),
  );
  content.append(statusLine, fileLine, renderTags(item), actions);
  card.append(media, content);
  return card;
}

function renderCatalog() {
  elements.grid.replaceChildren(...state.items.map(renderCard));
  elements.resultCount.textContent = String(state.total || 0);
  elements.pageLabel.textContent = `第 ${state.page} / ${state.pages} 页`;
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
      showToast("表情库加载失败", error.message, true);
    }
  } finally {
    elements.loading.hidden = true;
    elements.grid.hidden = false;
  }
}

function openEdit(item) {
  state.selected = item;
  elements.editFileName.textContent = item.file_name;
  renderMetadataEditor(elements.editMetadata, item.metadata);
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
  window.setTimeout(() => elements.editMetadata.querySelector("textarea")?.focus(), 0);
}

async function saveTags(event) {
  event.preventDefault();
  if (!state.selected) return;
  setBusy(elements.saveTags, true, "保存中");
  try {
    const tags = readMetadataEditor(elements.editMetadata);
    await request(`/${encodeURIComponent(state.selected.file_name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tags }),
    });
    elements.editDialog.close();
    showToast("检索信息已更新", state.selected.file_name);
    await loadCatalog();
  } catch (error) {
    showToast("标签保存失败", error.message, true);
  } finally {
    setBusy(elements.saveTags, false);
  }
}

async function retagItem(item, button) {
  if (!item || item.status === "missing") return;
  setBusy(button, true, "生成中");
  try {
    const payload = await request(`/${encodeURIComponent(item.file_name)}/retag`, { method: "POST" });
    showToast("标签已生成", item.file_name);
    if (state.selected?.file_name === item.file_name) {
      state.selected = payload.item;
      renderMetadataEditor(elements.editMetadata, payload.item.metadata);
    }
    await loadCatalog();
  } catch (error) {
    showToast("自动标注失败", error.message, true);
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
  setBusy(elements.confirmDelete, true, "删除中");
  try {
    await request(`/${encodeURIComponent(state.deleteTarget.file_name)}`, { method: "DELETE" });
    showToast("表情已删除", state.deleteTarget.file_name);
    elements.deleteDialog.close();
    state.deleteTarget = null;
    await loadCatalog();
  } catch (error) {
    showToast("删除失败", error.message, true);
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
  const existingKeys = new Set(
    state.uploads.map((entry) => `${entry.file.name}:${entry.file.size}:${entry.file.lastModified}`),
  );
  Array.from(files).forEach((file) => {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (existingKeys.has(key)) return;
    if (!acceptedTypes.has(file.type)) {
      showToast("不支持的图片格式", file.name, true);
      return;
    }
    if (file.size > 6 * 1024 * 1024) {
      showToast("图片超过 6 MiB", file.name, true);
      return;
    }
    state.uploads.push({
      file,
      previewUrl: URL.createObjectURL(file),
      status: "待上传",
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
    const action = entry.status === "待上传"
      ? cardButton("移除", "button-quiet", () => removeUpload(entry))
      : createElement("span", `upload-status ${entry.statusClass}`, entry.status);
    row.append(image, copy, action);
    return row;
  });
  elements.uploadQueue.replaceChildren(...rows);
}

async function uploadSelected(event) {
  event.preventDefault();
  if (!state.uploads.length) {
    showToast("请至少选择一张图片", "当前没有待上传文件。", true);
    return;
  }
  let sharedTags;
  try {
    sharedTags = readMetadataEditor(elements.uploadMetadata, { optional: true });
  } catch (error) {
    showToast("共享标签填写有误", error.message, true);
    return;
  }
  if (!sharedTags && !elements.autoTag.checked) {
    showToast("缺少共享标签", "请填写共享标签，或启用自动标注。", true);
    return;
  }
  setBusy(elements.startUpload, true, "上传中");
  let failures = 0;
  for (const entry of state.uploads) {
    entry.status = "上传中";
    entry.statusClass = "";
    renderUploadQueue();
    const form = new FormData();
    form.append("file", entry.file, entry.file.name);
    form.append("tags", sharedTags);
    form.append("auto_tag", String(elements.autoTag.checked));
    try {
      const payload = await request("", { method: "POST", body: form });
      entry.status = payload.status === "duplicate" ? "已存在" : "已添加";
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
    showToast("上传完成，但有文件失败", `${failures} 个文件未能导入。`, true);
  } else {
    showToast("上传完成", `已处理 ${state.uploads.length} 个文件。`);
    window.setTimeout(() => {
      elements.uploadDialog.close();
      clearUploads();
      renderMetadataEditor(elements.uploadMetadata);
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

renderMetadataEditor(elements.uploadMetadata);
bindEvents();
loadCatalog();
