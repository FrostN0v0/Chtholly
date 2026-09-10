"use strict";

const API = "/api/plugin-workshop";
const $ = (id) => document.getElementById(id);
const encoder = new TextEncoder();
const state = {
  status: null, projects: [], versions: [], name: "", version: null, detail: null,
  files: {}, baseline: {}, file: "", compare: null, busy: false, healthy: false,
  projectLimit: 50, versionLimit: 10, moreProjects: false, moreVersions: false,
  draftFiles: {}, draftFile: "", submitting: false,
};
const labels = { pending: "等待验收", validating: "验收中", passed: "通过", failed: "失败", unavailable: "隔离环境不可用", cancelled: "已取消" };

function text(id, value) {
  const node = $(id);
  const next = String(value ?? "");
  if (node.textContent !== next) node.textContent = next;
}
function notice(message, kind = "") {
  text("notice", message);
  $("notice").className = `notice ${kind}`;
}
function element(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(value ?? "");
  if (className) node.className = className;
  return node;
}
function option(value, label) {
  const node = element("option", label);
  node.value = String(value);
  return node;
}
function preserveScroll(callback) {
  const positions = [...document.querySelectorAll(".scroll, .code")].map((node) => [node, node.scrollTop, node.scrollLeft]);
  const page = [window.scrollX, window.scrollY];
  callback();
  for (const [node, top, left] of positions) { node.scrollTop = top; node.scrollLeft = left; }
  window.scrollTo(...page);
}
const bridgeOrigin = new URL(document.URL).origin;
const bridgeRequests = new Map();
window.addEventListener("message", (event) => {
  if (event.source !== window.parent || event.origin !== bridgeOrigin) return;
  const message = event.data;
  if (!message || typeof message !== "object") return;
  const pending = bridgeRequests.get(message.id);
  if (!pending) return;
  bridgeRequests.delete(message.id);
  window.clearTimeout(pending.timer);
  if (message.error) pending.reject(new Error(String(message.error)));
  else pending.resolve(message.result);
});
function bridgeApi(path, options) {
  const id = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      bridgeRequests.delete(id);
      reject(new Error("WebUI request timed out; refresh to inspect the durable result."));
    }, 330000);
    bridgeRequests.set(id, { resolve, reject, timer });
    window.parent.postMessage({ id, method: "api", payload: {
      url: API + path, method: options.method || "GET", timeout: 320000,
      headers: { "Content-Type": "application/json" },
      ...(options.body ? { data: JSON.parse(options.body) } : {}),
    } }, bridgeOrigin);
  });
}
async function api(path, options = {}) {
  if (window.parent !== window) {
    const data = await bridgeApi(path, options);
    if (!data || data.success !== true) throw new Error(data?.message || "Workshop request failed");
    return data;
  }
  const response = await fetch(API + path, {
    credentials: "same-origin", cache: "no-store", ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) throw new Error(`服务器未返回 JSON（HTTP ${response.status}）；请检查 WebUI 登录与路由。`);
  const data = await response.json();
  if (!response.ok || data.success !== true) {
    if (response.status === 401 || response.status === 403) state.healthy = false;
    throw new Error(`${data.message || "请求失败"} (${data.code || response.status})`);
  }
  return data;
}
function projectPath(name = state.name) { return `/projects/${encodeURIComponent(name)}`; }
function versionPath(version = state.version, name = state.name) { return `${projectPath(name)}/versions/${version}`; }
function selectedProject() { return state.projects.find((project) => project.plugin_name === state.name); }
function reportPassed(record) {
  const report = record?.report;
  return record?.validation_status === "passed" && report?.status === "passed"
    && report.source_hash === record.source_hash && Array.isArray(report.checks)
    && report.checks.length > 0 && report.checks.every((check) => check.passed === true);
}
function actionControls() {
  const record = state.detail;
  const ready = state.status?.ready === true && state.healthy;
  const locked = state.busy || state.submitting;
  const active = record?.active === true;
  const approved = !!record?.approved_by && reportPassed(record);
  const project = selectedProject();
  const reasons = {
    validate: !record ? "请先选择版本" : !ready ? "服务尚未就绪或连接失效" : !state.status?.sandbox?.available ? "隔离环境不可用；请先构建或修复 Docker 镜像" : active ? "活动版本不可原位重新验收" : "",
    approve: !record ? "请先选择版本" : !ready ? "服务尚未就绪或连接失效" : !reportPassed(record) ? "需要与精确哈希匹配且全部通过的验收报告" : record.approved_by ? "此版本已经批准" : !$("acknowledge-native").checked ? "必须明确勾选完整原生权限授权" : "",
    activate: !record ? "请先选择版本" : !ready ? "服务尚未就绪或连接失效" : !approved ? "需要已批准、验收通过的精确版本" : active && project?.enabled ? "此版本已启用，无需重复加载" : "",
    rollback: !record ? "请先选择版本" : !ready ? "服务尚未就绪或连接失效" : !approved ? "需要已批准且验收通过" : !record.ever_active ? "只能回滚到曾成功激活的版本" : active && project?.enabled ? "此版本已经启用" : "",
    disable: !project ? "请先选择项目" : !ready ? "服务尚未就绪或连接失效" : !project.enabled ? "项目当前未启用" : "",
  };
  const names = { validate: "重新验收", approve: "批准", activate: "激活", rollback: "回滚", disable: "停用" };
  const list = [];
  for (const [id, reason] of Object.entries(reasons)) {
    $(id).disabled = locked || !!reason;
    $(id).title = locked ? "操作进行中，请等待真实后端结果" : reason || "操作将使用上方显示的精确版本与哈希";
    if (reason) list.push(element("li", `${names[id]}：${reason}`));
  }
  if (record?.approved_by && !active) list.push(element("li", "重新验收将清除已有审批；内容不变也需重新人工批准。"));
  $("action-reasons").replaceChildren(...list);
  $("refresh").disabled = locked;
  $("new-candidate").disabled = locked || !ready;
  $("new-candidate").title = ready ? "保存不可变候选并请求隔离验收；不会自动激活" : "需要已认证、已就绪的工坊服务";
  $("draft-from-version").disabled = locked || !record;
  $("acknowledge-native").disabled = locked || !record || !!record.approved_by;
  $("source-file").disabled = locked;
  $("compare-version").disabled = locked;
  for (const node of document.querySelectorAll("#projects button, #versions button, .more")) node.disabled = locked;
}
function renderStatus() {
  const status = state.status;
  $("service-status").className = `badge ${status?.ready ? "good" : "bad"}`;
  text("service-status", `服务：${status?.ready ? "就绪" : "未就绪"}`);
  $("sandbox-status").className = `badge ${status?.sandbox?.available ? "good" : "bad"}`;
  text("sandbox-status", `隔离环境：${status?.sandbox?.available ? "可用" : "不可用"}`);
  text("prerequisites", status?.sandbox?.available
    ? "容器功能验收可用。审批不会执行源码；激活与回滚仅由人工授权后调用真实 Entari 生命周期。"
    : `不能完成隔离验收：${status?.sandbox?.reason || "服务尚未给出可用的 Docker 隔离状态"}。不会在宿主进程降级执行候选，也不会自动构建镜像。`);
  text("backend-detail", JSON.stringify(status, null, 2));
  text("connection", state.healthy ? "已认证 · 已连接" : "连接失效");
  $("connection").className = `badge ${state.healthy ? "good" : "bad"}`;
}
function listButton(title, meta, selected, action) {
  const node = document.createElement("button");
  node.type = "button";
  node.setAttribute("aria-current", String(selected));
  node.append(element("span", title, "item-title"), element("span", meta, "item-meta"));
  node.addEventListener("click", action);
  return node;
}
function renderLists() {
  $("projects").replaceChildren(...state.projects.map((project) => listButton(
    project.plugin_name,
    `${project.enabled ? "已启用" : "未启用"} · 活动 v${project.active_version ?? "—"}${project.last_error ? " · 存在运行错误" : ""}`,
    state.name === project.plugin_name,
    () => selectProject(project.plugin_name),
  )));
  if (!state.projects.length) $("projects").append(element("p", "暂无项目。可以提交第一个不可变候选。", "empty"));
  $("versions").replaceChildren(...state.versions.map((record) => listButton(
    `v${record.version}${record.active ? " · 活动" : ""}`,
    `${labels[record.validation_status] || record.validation_status} · ${record.approved_by ? "已批准" : "未批准"}`,
    state.version === record.version,
    () => selectVersion(record.version),
  )));
  if (!state.versions.length) $("versions").append(element("p", state.name ? "暂无版本" : "请先选择项目", "empty"));
  text("project-count", `${state.projects.length}${state.moreProjects ? "+" : ""}`);
  text("version-count", `${state.versions.length}${state.moreVersions ? "+" : ""}`);
  $("more-projects").hidden = !state.moreProjects;
  $("more-versions").hidden = !state.moreVersions;
}
function renderSummary(record) {
  const manifest = record.manifest;
  text("description", manifest.description);
  $("commands").replaceChildren(...manifest.commands.map((value) => element("span", value, "chip")));
  $("permissions").replaceChildren(...manifest.permissions.map((value) => element("li", value)));
  if (!manifest.permissions.length) $("permissions").append(element("li", "未声明权限；这不表示原生插件没有权限。"));
  text("data-description", manifest.data_description);
  text("configuration", JSON.stringify(manifest.configuration, null, 2));
  $("declared-checks").replaceChildren(...manifest.checks.map((check) => {
    const node = element("div", "", "check");
    node.append(element("strong", check.command));
    node.append(element("p", `期望包含：${check.expected_contains}`));
    node.append(element("p", `${check.operator ? "超级用户身份" : "普通用户身份"} · ${check.repeatable ? "在替换 / 恢复后重复执行" : "仅初次执行的变更 / 初始化检查"}`, "muted"));
    return node;
  }));
}
function renderReport(record) {
  const report = record.report;
  text("report-summary", `${labels[record.validation_status] || record.validation_status}${report ? ` · ${report.checks.filter((check) => check.passed).length}/${report.checks.length} 项通过` : " · 尚无报告"}`);
  $("report-checks").replaceChildren(...(report?.checks || []).map((check) => {
    const node = element("div", "", `check ${check.passed ? "passed" : "failed"}`);
    node.append(element("strong", `${check.passed ? "通过" : "失败"} · ${check.name}`), element("p", check.detail));
    return node;
  }));
  text("report-environment", report ? JSON.stringify({ source_hash: report.source_hash, image_id: report.image_id, framework_version: report.framework_version, python_version: report.python_version }, null, 2) : "无报告；不能批准。");
  text("report-log", report?.log || "尚无验收日志。");
}
function linearDiff(before, after, path) {
  if (before === after) return "文件内容完全一致。";
  if (before === undefined) return `--- /dev/null\n+++ ${path}\n` + after.split("\n").map((line) => `+${line}`).join("\n");
  if (after === undefined) return `--- ${path}\n+++ /dev/null\n` + before.split("\n").map((line) => `-${line}`).join("\n");
  const oldLines = before.split("\n");
  const newLines = after.split("\n");
  let start = 0;
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) start++;
  let endOld = oldLines.length, endNew = newLines.length;
  while (endOld > start && endNew > start && oldLines[endOld - 1] === newLines[endNew - 1]) { endOld--; endNew--; }
  const from = Math.max(0, start - 3);
  const trailing = Math.min(3, oldLines.length - endOld);
  const lines = [`--- baseline/${path}`, `+++ candidate/${path}`, `@@ -${from + 1},${endOld - from + trailing} +${from + 1},${endNew - from + trailing} @@`];
  for (let i = from; i < start; i++) lines.push(` ${oldLines[i]}`);
  for (let i = start; i < endOld; i++) lines.push(`-${oldLines[i]}`);
  for (let i = start; i < endNew; i++) lines.push(`+${newLines[i]}`);
  for (let i = endOld; i < endOld + trailing; i++) lines.push(` ${oldLines[i]}`);
  return lines.join("\n");
}
function renderSource() {
  const paths = [...new Set([...Object.keys(state.files), ...Object.keys(state.baseline)])].sort();
  if (!paths.includes(state.file)) state.file = paths.includes("__init__.py") ? "__init__.py" : paths[0] || "";
  $("source-file").replaceChildren(...paths.map((path) => option(path, `${path}${!(path in state.files) ? "（已删除）" : !(path in state.baseline) && state.compare ? "（新增）" : ""}`)));
  $("source-file").value = state.file;
  const versions = new Set(state.versions.map((record) => record.version));
  const active = selectedProject()?.active_version;
  if (active) versions.add(active);
  if (state.compare) versions.add(state.compare);
  versions.delete(state.version);
  $("compare-version").replaceChildren(option("", "不比较"), ...[...versions].sort((a, b) => b - a).map((version) => option(version, `v${version}${version === active ? " · 活动 / 最后活动" : ""}`)));
  $("compare-version").value = state.compare || "";
  const source = state.files[state.file];
  text("source-code", source === undefined ? "此文件在候选版本中不存在。" : source);
  text("source-diff", state.compare ? linearDiff(state.baseline[state.file], source, state.file) : "选择活动版本或历史版本查看差异。差异将首尾共同内容折叠为 3 行上下文，中间变化完整显示。");
  text("source-meta", `${Object.keys(state.files).length} 个候选文件 · 当前文件 ${source === undefined ? "已删除" : `${encoder.encode(source).length} 字节`} · 基线 ${state.compare ? `v${state.compare}` : "未选择"}`);
}
function renderDetail() {
  const record = state.detail;
  $("empty-selection").hidden = !!record;
  $("review-content").hidden = !record;
  if (!record) return;
  text("version-title", `${record.plugin_name} / v${record.version} · ${record.manifest.title}`);
  text("version-meta", `创建于 ${record.created_at} · ${record.approved_by ? `由 ${record.approved_by} 批准于 ${record.approved_at}` : "尚未批准"}${selectedProject()?.last_error ? ` · 运行错误：${selectedProject().last_error}` : ""}`);
  text("source-hash", record.source_hash);
  renderSummary(record);
  renderReport(record);
  renderSource();
}
async function loadProjects() {
  const projects = [];
  let last = [];
  while (projects.length < state.projectLimit || (state.name && !projects.some((project) => project.plugin_name === state.name))) {
    last = (await api(`/projects?limit=50&offset=${projects.length}`)).items;
    projects.push(...last);
    if (last.length < 50) break;
  }
  state.projects = projects;
  state.projectLimit = Math.max(state.projectLimit, projects.length);
  state.moreProjects = last.length === 50;
  if (!state.name && projects.length) state.name = projects[0].plugin_name;
}
async function loadVersions() {
  if (!state.name) { state.versions = []; return; }
  const records = [];
  let last = [];
  while (records.length < state.versionLimit && records.length < 256) {
    last = (await api(`${projectPath()}/versions?limit=10&offset=${records.length}`)).items;
    records.push(...last);
    if (last.length < 10) break;
  }
  state.versions = records.sort((a, b) => b.version - a.version);
  state.moreVersions = last.length === 10 && records.length < 256;
  if (!state.version && records.length) state.version = state.versions[0].version;
}
async function loadDetail() {
  if (!state.name || !state.version) { state.detail = null; return; }
  const [detail, source] = await Promise.all([api(versionPath()), api(`${versionPath()}/source`)]);
  const previousHash = state.detail?.source_hash;
  state.detail = detail.item;
  state.files = source.item;
  if (previousHash !== state.detail.source_hash) $("acknowledge-native").checked = false;
  if (state.compare === null) {
    const active = selectedProject()?.active_version;
    state.compare = active && active !== state.version ? active : state.versions.find((record) => record.version < state.version)?.version || "";
  }
  state.baseline = state.compare ? (await api(`${versionPath(state.compare)}/source`)).item : {};
}
async function refresh({ silent = false, message = "" } = {}) {
  if (state.busy || state.submitting) return;
  state.busy = true;
  actionControls();
  if (!silent) notice("正在读取后端状态与不可变版本…");
  try {
    state.status = (await api("/status")).item;
    await loadProjects();
    await loadVersions();
    await loadDetail();
    state.healthy = true;
    preserveScroll(() => { renderStatus(); renderLists(); renderDetail(); });
    if (message) notice(message, "success");
    else if (!silent) notice(`状态已更新 · ${new Date().toLocaleTimeString()}。所有操作都以当前显示的不可变版本与哈希为准。`);
  } catch (error) {
    state.healthy = false;
    text("connection", "读取失败");
    $("connection").className = "badge bad";
    notice(`未能完成刷新：${error.message}。旧内容仅供参考，所有管理操作已锁定。`, "error");
  } finally {
    state.busy = false;
    actionControls();
  }
}
async function selectProject(name) {
  if (state.busy || state.submitting || name === state.name) return;
  state.name = name; state.version = null; state.compare = null; state.file = ""; state.detail = null; state.versionLimit = 10;
  $("acknowledge-native").checked = false;
  await refresh();
}
async function selectVersion(version) {
  if (state.busy || state.submitting || version === state.version) return;
  state.version = version; state.compare = null; state.file = "";
  $("acknowledge-native").checked = false;
  await refresh();
}
function confirmOperation(message) {
  const dialog = $("operation-dialog");
  if (dialog.open) return Promise.resolve(false);
  text("operation-summary", message);
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.showModal();
  });
}
async function mutate(action) {
  if (state.busy || state.submitting || $(action).disabled || !state.detail) return;
  const record = state.detail;
  const prompts = {
    validate: "重新验收会清除已有审批（活动版本不可操作）。候选只在真实隔离容器运行。继续？",
    approve: `批准 ${record.plugin_name} v${record.version}？\nSHA-256: ${record.source_hash}\n这是授予完整原生进程权限的人工信任决定。审批本身不会激活。`,
    activate: `将 ${record.plugin_name} v${record.version} 加载到机器人进程？\nSHA-256: ${record.source_hash}\n候选将拥有进程完整权限并可能产生不可撤回的数据或外部副作用。`,
    rollback: `将代码回滚到 ${record.plugin_name} v${record.version}？\nSHA-256: ${record.source_hash}\n数据与外部副作用不会随代码回滚。`,
    disable: `停用 ${record.plugin_name}？\n保留版本历史与数据；已发生的外部副作用不会撤回。`,
  };
  if (!await confirmOperation(prompts[action])) return;
  state.busy = true;
  actionControls();
  notice(`正在执行 ${action} · ${record.plugin_name} v${record.version}。等待后端确认，请勿重复提交。`);
  let success = "";
  try {
    const payload = action === "approve" ? { source_hash: record.source_hash, acknowledge_native: $("acknowledge-native").checked }
      : ["activate", "rollback"].includes(action) ? { source_hash: record.source_hash } : {};
    const route = action === "disable" ? `${projectPath(record.plugin_name)}/disable` : `${versionPath(record.version, record.plugin_name)}/${action}`;
    const result = (await api(route, { method: "POST", body: JSON.stringify(payload) })).item;
    if (action === "validate") {
      success = `验收已完成：${labels[result.validation_status] || result.validation_status}。${result.validation_status === "passed" ? "仍需人工审批，尚未自动激活。" : "未取得可批准的通过结果；请查看报告并修订新版本。"}`;
    } else if (action === "approve") success = `已批准 ${result.plugin_name} v${result.version}，SHA-256 ${result.source_hash}；尚未自动激活。`;
    else success = `${action} 后端已确认：${result.plugin_name} · ${result.enabled ? "已启用" : "已停用"} · 活动 / 最后活动 v${result.active_version ?? "无"}。`;
    $("acknowledge-native").checked = false;
  } catch (error) {
    state.healthy = false;
    notice(`操作未确认成功：${error.message}。请刷新核对真实状态后再决定，不要盲目重试。`, "error");
  } finally {
    state.busy = false;
    actionControls();
  }
  if (success) await refresh({ message: success });
}
function setTab(name, focus = false) {
  for (const node of document.querySelectorAll("[data-tab]")) {
    const selected = node.dataset.tab === name;
    node.setAttribute("aria-selected", String(selected));
    node.tabIndex = selected ? 0 : -1;
    $(`${node.dataset.tab}-view`).hidden = !selected;
    if (selected && focus) node.focus();
  }
}
function saveDraftFile() {
  if (state.draftFile) state.draftFiles[state.draftFile] = $("candidate-source").value;
}
function renderDraftFiles() {
  const files = Object.keys(state.draftFiles).sort();
  if (!files.includes(state.draftFile)) state.draftFile = files[0] || "";
  $("candidate-file").replaceChildren(...files.map((path) => option(path, path)));
  $("candidate-file").value = state.draftFile;
  $("candidate-source").value = state.draftFiles[state.draftFile] ?? "";
  text("editor-label", `文件内容 · ${state.draftFile || "尚无文件"}`);
  updateDraftSize();
}
function updateDraftSize() {
  const files = Object.values(state.draftFiles);
  const total = files.reduce((sum, value) => sum + encoder.encode(value).length, 0);
  text("candidate-size", `${files.length}/32 个文件 · ${total.toLocaleString()} / 262,144 字节`);
}
function openCandidate(fromVersion = false) {
  if (state.busy || state.submitting) return;
  const manifest = fromVersion ? state.detail.manifest : { title: "", description: "", commands: [], permissions: [], data_description: "", configuration: {}, checks: [] };
  $("candidate-name").value = fromVersion ? state.name : "";
  $("candidate-manifest").value = JSON.stringify(manifest, null, 2);
  state.draftFiles = fromVersion ? { ...state.files } : { "__init__.py": "" };
  state.draftFile = "__init__.py";
  $("new-file-path").value = "";
  text("candidate-error", ""); text("candidate-progress", "");
  renderDraftFiles();
  $("candidate-dialog").showModal();
}
async function submitCandidate(event) {
  event.preventDefault();
  if (state.submitting || state.busy) return;
  text("candidate-error", "");
  const name = $("candidate-name").value.trim();
  let manifest;
  try {
    if (!/^[a-z][a-z0-9_]{2,39}$/.test(name)) throw new Error("项目名格式不正确。");
    manifest = JSON.parse($("candidate-manifest").value);
    if (!manifest || Array.isArray(manifest) || typeof manifest !== "object") throw new Error("Manifest 必须为 JSON 对象。");
    const entries = Object.entries(state.draftFiles);
    if (!Object.hasOwn(state.draftFiles, "__init__.py") || !entries.length || entries.length > 32) throw new Error("需要 __init__.py，文件总数必须在 1–32 之间。");
    const sizes = entries.map(([, value]) => encoder.encode(value).length);
    if (sizes.some((size) => size > 65536) || sizes.reduce((sum, size) => sum + size, 0) > 262144) throw new Error("候选文件超过单文件或总大小限制。");
  } catch (error) { text("candidate-error", `尚未提交：${error.message}`); return; }
  state.submitting = true;
  for (const control of $("candidate-form").querySelectorAll("button, input, textarea, select")) control.disabled = true;
  actionControls();
  text("candidate-progress", "正在保存不可变版本并等待容器验收。不会自动批准或激活，请勿重复提交…");
  let created = null;
  try {
    created = (await api(`${projectPath(name)}/versions`, { method: "POST", body: JSON.stringify({ files: state.draftFiles, manifest }) })).item;
    state.name = created.plugin_name; state.version = created.version; state.compare = null; state.file = "";
    $("candidate-dialog").close();
  } catch (error) {
    text("candidate-error", `提交未确认完成：${error.message}。版本可能已持久保存；请关闭后刷新版本列表核对，避免重复提交。草稿已保留。`);
    text("candidate-progress", "未确认完成；不会报告假成功。");
  } finally {
    state.submitting = false;
    for (const control of $("candidate-form").querySelectorAll("button, input, textarea, select")) control.disabled = false;
    actionControls();
  }
  if (created) await refresh({ message: `已保存不可变候选 ${created.plugin_name} v${created.version} · 验收${labels[created.validation_status] || created.validation_status}。未批准、未激活；请审阅精确哈希和报告。` });
}

$("refresh").addEventListener("click", () => refresh());
$("more-projects").addEventListener("click", () => { state.projectLimit += 50; refresh(); });
$("more-versions").addEventListener("click", () => { state.versionLimit += 10; refresh(); });
$("acknowledge-native").addEventListener("change", actionControls);
$("operation-confirm").addEventListener("click", () => $("operation-dialog").close("confirm"));
$("operation-cancel").addEventListener("click", () => $("operation-dialog").close("cancel"));
for (const action of ["validate", "approve", "activate", "rollback", "disable"]) $(action).addEventListener("click", () => mutate(action));
for (const node of document.querySelectorAll("[data-tab]")) {
  node.addEventListener("click", () => setTab(node.dataset.tab));
  node.addEventListener("keydown", (event) => {
    const tabs = ["summary", "source", "report"];
    let index = tabs.indexOf(node.dataset.tab);
    if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") index = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = tabs.length - 1;
    else return;
    event.preventDefault(); setTab(tabs[index], true);
  });
}
$("source-file").addEventListener("change", () => { state.file = $("source-file").value; renderSource(); });
$("compare-version").addEventListener("change", () => { state.compare = Number($("compare-version").value) || ""; refresh({ silent: true }); });
$("new-candidate").addEventListener("click", () => openCandidate());
$("draft-from-version").addEventListener("click", () => openCandidate(true));
$("close-candidate").addEventListener("click", () => { if (!state.submitting) $("candidate-dialog").close(); });
$("candidate-dialog").addEventListener("cancel", (event) => { if (state.submitting) event.preventDefault(); });
$("candidate-form").addEventListener("submit", submitCandidate);
$("candidate-file").addEventListener("change", () => { state.draftFile = $("candidate-file").value; renderDraftFiles(); });
$("candidate-source").addEventListener("input", () => { saveDraftFile(); updateDraftSize(); });
$("add-file").addEventListener("click", () => {
  const path = $("new-file-path").value.trim();
  if (!path || path === "__proto__" || path === "constructor" || path === "prototype" || Object.hasOwn(state.draftFiles, path)) { text("candidate-error", "请输入尚不存在的普通相对文件路径。"); return; }
  if (Object.keys(state.draftFiles).length >= 32) { text("candidate-error", "最多 32 个文件。"); return; }
  state.draftFiles[path] = ""; state.draftFile = path; $("new-file-path").value = "";
  text("candidate-error", ""); renderDraftFiles();
});
$("remove-file").addEventListener("click", () => {
  if (state.draftFile === "__init__.py") { text("candidate-error", "__init__.py 是必需文件，不能移除。"); return; }
  delete state.draftFiles[state.draftFile]; state.draftFile = "__init__.py"; renderDraftFiles();
});
refresh();
window.setInterval(() => {
  if (!document.hidden && !state.busy && !state.submitting && !$("candidate-dialog").open && !$("operation-dialog").open && !$("acknowledge-native").checked) refresh({ silent: true });
}, 20000);
