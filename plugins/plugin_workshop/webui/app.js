"use strict";

const API = "/api/plugin-workshop";
const $ = (id) => document.getElementById(id);
const encoder = new TextEncoder();
const state = {
  status: null, projects: [], versions: [], name: "", version: null, detail: null,
  files: {}, baseline: {}, file: "", compare: null, busy: false, healthy: false,
  projectLimit: 50, versionLimit: 10, moreProjects: false, moreVersions: false,
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
function bridgeApi(path) {
  const id = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      bridgeRequests.delete(id);
      reject(new Error("WebUI 读取超时；请刷新重试。"));
    }, 330000);
    bridgeRequests.set(id, { resolve, reject, timer });
    window.parent.postMessage({ id, method: "api", payload: {
      url: API + path, method: "GET", timeout: 320000,
    } }, bridgeOrigin);
  });
}
async function api(path) {
  if (window.parent !== window) {
    const data = await bridgeApi(path);
    if (!data || data.success !== true) throw new Error(data?.message || "Workshop request failed");
    return data;
  }
  const response = await fetch(API + path, {
    method: "GET", credentials: "same-origin", cache: "no-store",
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
function readControls() {
  const locked = state.busy;
  $("refresh").disabled = locked;
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
    ? "容器功能验收可用。本页只读；超级用户会话中的模型工具可分别提交验收和激活精确版本。"
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
  if (!state.projects.length) $("projects").append(element("p", "暂无插件工坊项目。", "empty"));
  $("versions").replaceChildren(...state.versions.map((record) => listButton(
    `v${record.version}${record.active ? " · 活动" : ""}`,
    labels[record.validation_status] || record.validation_status,
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
  text("report-environment", report ? JSON.stringify({ source_hash: report.source_hash, image_id: report.image_id, framework_version: report.framework_version, python_version: report.python_version }, null, 2) : "无报告；不能激活。");
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
  text("version-meta", `创建于 ${record.created_at} · ${record.active ? "当前活动版本" : record.ever_active ? "曾成功激活" : "尚未激活"}${selectedProject()?.last_error ? ` · 运行错误：${selectedProject().last_error}` : ""}`);
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
  state.detail = detail.item;
  state.files = source.item;
  if (state.compare === null) {
    const active = selectedProject()?.active_version;
    state.compare = active && active !== state.version ? active : state.versions.find((record) => record.version < state.version)?.version || "";
  }
  state.baseline = state.compare ? (await api(`${versionPath(state.compare)}/source`)).item : {};
}
async function refresh({ silent = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  readControls();
  if (!silent) notice("正在读取后端状态与不可变版本…");
  try {
    state.status = (await api("/status")).item;
    await loadProjects();
    await loadVersions();
    await loadDetail();
    state.healthy = true;
    preserveScroll(() => { renderStatus(); renderLists(); renderDetail(); });
    if (!silent) notice(`状态已更新 · ${new Date().toLocaleTimeString()}。本页仅供查看不可变版本、源码与验收结果。`);
  } catch (error) {
    state.healthy = false;
    text("connection", "读取失败");
    $("connection").className = "badge bad";
    notice(`未能完成刷新：${error.message}。旧内容仅供参考。`, "error");
  } finally {
    state.busy = false;
    readControls();
  }
}
async function selectProject(name) {
  if (state.busy || name === state.name) return;
  state.name = name; state.version = null; state.compare = null; state.file = ""; state.detail = null; state.versionLimit = 10;
  await refresh();
}
async function selectVersion(version) {
  if (state.busy || version === state.version) return;
  state.version = version; state.compare = null; state.file = "";
  await refresh();
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

$("refresh").addEventListener("click", () => refresh());
$("more-projects").addEventListener("click", () => { state.projectLimit += 50; refresh(); });
$("more-versions").addEventListener("click", () => { state.versionLimit += 10; refresh(); });
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
refresh();
window.setInterval(() => {
  if (!document.hidden && !state.busy) refresh({ silent: true });
}, 20000);
