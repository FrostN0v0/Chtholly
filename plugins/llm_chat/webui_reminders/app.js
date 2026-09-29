"use strict";

const apiBase = "/api/llm-chat/reminders";
const sessionsBase = "/api/llm-chat/sessions";
const bridgeOrigin = new URL(document.URL).origin;
const inFrame = window.parent !== window;
const pending = new Map();
const batchSize = 40;
const $ = (id) => document.getElementById(id);
let bridgeReady = !inFrame;
let pageActive = true;
let pageController = new AbortController();
let viewController = new AbortController();
let detailController = new AbortController();
let generation = 0;
let detailGeneration = 0;
let loading = false;
let actionBusy = false;
let offset = 0;
let loaded = 0;
let hasMore = false;
let searchQuery = "";
let searchTimer = null;
let occurrenceHasMore = false;
let occurrenceTotal = 0;
let occurrenceLoading = false;
let scopes = [];
let selectedScope = "all";
let selectedSession = "all";
let selectedStatus = "all";
let selectedTaskRef = "";
let selectedOccurrence = null;
let taskDetail = null;
let occurrences = [];
let occurrenceDetail = null;
let activeTab = "overview";

function abortError() { return new DOMException("读取已停止", "AbortError"); }
function apiUrl(path, method) {
  if (typeof path !== "string" || !path.startsWith("/")) throw new Error("无效请求路径");
  const absolute = path.startsWith("/api/");
  const url = new URL(absolute ? path : `${apiBase}${path}`, bridgeOrigin);
  if (url.origin !== bridgeOrigin || url.hash || url.username || url.password) throw new Error("无效请求路径");
  if (absolute) {
    const auditPath = /^\/api\/llm-chat\/sessions\/(?:turns\/[^/]+\/events|events\/[^/]+\/payload)$/;
    if (method !== "GET" || !auditPath.test(url.pathname)) throw new Error("无效审计路径");
  } else if (!url.pathname.startsWith(`${apiBase}/`)) throw new Error("无效请求路径");
  return url.pathname + url.search;
}
window.addEventListener("message", (event) => {
  if (!inFrame || event.source !== window.parent || event.origin !== bridgeOrigin) return;
  const message = event.data;
  if (!message || typeof message !== "object") return;
  if (message.type === "webui.ready") {
    bridgeReady = true;
    for (const item of pending.values()) item.send();
    return;
  }
  const item = pending.get(message.id);
  if (!item || !item.sent) return;
  if (Object.hasOwn(message, "error")) item.finish(new Error(String(message.error)));
  else if (Object.hasOwn(message, "result")) item.finish(null, message.result);
});
function bridgeApi(url, method, body, signal) {
  if (signal.aborted || !pageActive) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const timeout = method === "GET" ? 30000 : 320000;
    const item = {
      sent: false,
      finish(error, value) {
        if (!pending.delete(id)) return;
        clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        if (error) reject(error); else resolve(value);
      },
      send() {
        if (!bridgeReady || item.sent || !pending.has(id)) return;
        if (signal.aborted || !pageActive) return item.finish(abortError());
        item.sent = true;
        try {
          window.parent.postMessage({ id, method: "api", payload: {
            url, method, timeout, headers: { "Content-Type": "application/json" },
            ...(body !== undefined ? { data: body } : {}),
          } }, bridgeOrigin);
        } catch (error) { item.finish(error); }
      },
    };
    const onAbort = () => item.finish(abortError());
    const timer = setTimeout(() => item.finish(new Error(method === "GET"
      ? "WebUI 读取超时，请刷新后重试。"
      : "WebUI 操作等待超时；操作可能已执行，请刷新状态，不要重复提交。")), timeout + 10000);
    pending.set(id, item);
    signal.addEventListener("abort", onAbort, { once: true });
    item.send();
  });
}
async function request(path, { method = "GET", body, signal = viewController.signal } = {}) {
  if (method !== "GET" && method !== "POST") throw new Error("不支持的请求方法");
  const url = apiUrl(path, method);
  if (signal.aborted || !pageActive) throw abortError();
  let data;
  if (inFrame) data = await bridgeApi(url, method, body, signal);
  else {
    const response = await fetch(url, {
      method, credentials: "same-origin", cache: "no-store", signal,
      headers: { "Content-Type": "application/json" },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    data = await response.json().catch(() => ({ success: false, message: "服务器响应无法解析" }));
    if (!response.ok) {
      const error = new Error(data?.message || `请求失败（${response.status}）`);
      error.code = data?.code;
      error.status = response.status;
      throw error;
    }
  }
  if (signal.aborted || !pageActive) throw abortError();
  if (!data || typeof data !== "object" || data.success !== true) {
    const error = new Error(data?.message || "服务器响应无法解析");
    error.code = data?.code;
    throw error;
  }
  return data;
}
function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = String(value ?? "");
  if (className) node.className = className;
  return node;
}
function button(label, action, className = "") {
  const item = text("button", label, className);
  item.type = "button";
  item.addEventListener("click", () => Promise.resolve().then(action).catch(showError));
  return item;
}
function showError(error) {
  if (error?.name !== "AbortError") status(error?.message || "读取失败", true);
}
function status(message, error = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", error);
}
function date(value, timezone) {
  if (!value) return "未记录";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "未记录";
  try {
    return `${new Intl.DateTimeFormat("zh-CN", {
      timeZone: timezone || "UTC", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(parsed)} · ${timezone || "UTC"}`;
  } catch { return `${parsed.toISOString().slice(0, 16).replace("T", " ")} · UTC`; }
}
function zonedIso(value, timezone) {
  if (!value) return "";
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return "";
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(instant).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    const local = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
    const minutes = Math.round((local - instant.getTime()) / 60000);
    const sign = minutes >= 0 ? "+" : "-";
    const abs = Math.abs(minutes);
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
  } catch { return ""; }
}
function checkedTime(value, timezone, label) {
  const match = /^(\d{4}-\d\d-\d\d)T(\d\d):(\d\d)(?::(\d\d))?(Z|[+-]\d\d:\d\d)$/.exec(value);
  if (!match) throw new Error(`${label}须填写带时区偏移的 ISO 时间，例如 2026-10-01T09:00:00+08:00。`);
  const parsed = new Date(value);
  const normalized = zonedIso(value, timezone);
  if (Number.isNaN(parsed.getTime()) || !normalized) throw new Error(`${label}或 IANA 时区无效。`);
  const suppliedOffset = match[5] === "Z" ? "+00:00" : match[5];
  const supplied = `${match[1]}T${match[2]}:${match[3]}:${match[4] || "00"}${suppliedOffset}`;
  if (normalized !== supplied) throw new Error(`${label}的日期、时区偏移须与 ${timezone} 一致。`);
  if (parsed.getTime() <= Date.now()) throw new Error(`${label}必须晚于当前时间。`);
  return parsed;
}
const statusLabels = { active: "待执行", paused: "已暂停", completed: "已结束", cancelled: "已取消" };
const stateLabels = {
  claimed: "已领取，尚未开始", model_started: "模型处理中", dm_started: "私聊目标建立中",
  transport_started: "发送中，结果未确认", confirmed: "已有确认回执，结算中",
  completed: "已结算", paused: "已暂停", cancelled: "已取消",
  expired_recovered: "恢复时已过期", unknown: "执行结果不确定",
};
const pauseReasons = {
  paused_by_user: "用户已暂停", task_paused: "已暂停任务",
  missing_reminder_decision: "唤醒未提交结束决议", occurrence_result_unknown: "发送结果未确认",
  preparation_failed: "准备失败，未开始发送", model_unavailable: "模型不可用",
  model_failed: "模型执行失败", transport_result_unknown: "发送结果不确定，请勿直接重试",
};
const captureLabels = {
  complete: "完整捕获", redacted: "已脱敏", overflow: "超出捕获上限",
  recorded: "已记录", partial: "部分捕获", not_recorded: "未记录",
};
const triggerLabels = {
  scheduled_at: "预定唤醒", now: "实际唤醒", late_by_seconds: "迟到秒数",
  mode: "任务类型", event_at: "记录时间",
};
function triggerValue(key, value, timezone) {
  if (["scheduled_at", "now", "event_at"].includes(key)) return date(value, timezone);
  if (key === "mode") return value === "adaptive" ? "长期目标" : value === "once" ? "一次性提醒" : "未记录";
  return display(value);
}
const environmentLabels = {
  session_ref: "提醒执行会话", scope_ref: "交付频道", model: "实际请求模型",
  baseline_model: "会话基线模型", persona: "当次人格", persona_hash: "人格指纹",
  system_version: "系统指令版本", tool_schema_hash: "工具声明指纹",
  policy_version: "权限策略版本", created_at: "执行会话创建时间",
  delivery_channel_id: "交付目标频道", account_id: "账号", platform: "平台",
};
function ownerLabel(task) {
  const owner = task.owner_user_id || "未记录统一身份";
  return task.platform_user_id && task.platform_user_id !== owner
    ? `${owner}（平台用户 ${task.platform_user_id}）` : owner;
}
function scopeLabel(scope) {
  return `${scope.channel_name || scope.channel_id || "未命名频道"} · ${scope.platform || "未知平台"} · ${scope.account_id || "未知账号"}`;
}
function section(title) {
  const node = text("section", "", "section");
  node.append(text("h3", title));
  return node;
}
function fields(items) {
  const table = text("dl", "", "fields");
  for (const [label, value] of items) table.append(text("dt", label), text("dd", value == null || value === "" ? "未记录" : value));
  return table;
}
function display(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "未记录";
}
function disclosure(title, value) {
  const node = text("details", "", "disclosure");
  node.append(text("summary", title), text("pre", display(value)));
  return node;
}
function field(label, value, maxLength, multiline = false) {
  const wrapper = text("label", "", "field");
  const input = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) input.type = "text";
  input.value = value || "";
  input.maxLength = maxLength;
  if (multiline) input.rows = 3;
  wrapper.append(text("span", label), input);
  return { wrapper, input };
}
function editor(task) {
  const details = text("details", "", "editor");
  details.append(text("summary", "编辑任务"));
  const grid = text("div", "", "edit-grid");
  const goal = field("任务目标", task.goal, 1000, true);
  const background = field("背景摘要", task.background, 3000, true);
  const continuation = field("延续状态", task.continuation_state, 2000, true);
  const schedule = field("下次触发（含 UTC 偏移）", zonedIso(task.due_at, task.timezone), 40);
  const timezone = field("IANA 时区", task.timezone || "UTC", 64);
  const expiry = field("失效时间（含 UTC 偏移）", zonedIso(task.expires_at, task.timezone), 40);
  for (const control of [goal, background, continuation, schedule, timezone, expiry]) grid.append(control.wrapper);
  const save = button("保存修改", () => {
    const changes = {};
    const newGoal = goal.input.value.trim();
    if (!newGoal) throw new Error("任务目标不能为空。");
    if (newGoal !== task.goal) changes.goal = newGoal;
    if (background.input.value !== (task.background || "")) changes.background = background.input.value;
    if (continuation.input.value !== (task.continuation_state || "")) changes.continuation_state = continuation.input.value;
    const zone = timezone.input.value.trim();
    if (!zone) throw new Error("IANA 时区不能为空。");
    try { new Intl.DateTimeFormat("en-US", { timeZone: zone }); }
    catch { throw new Error("请输入有效的 IANA 时区。"); }
    const scheduled = schedule.input.value.trim();
    if (scheduled !== zonedIso(task.due_at, task.timezone) || zone !== task.timezone) {
      checkedTime(scheduled, zone, "下次触发");
      changes.schedule = scheduled;
    }
    if (zone !== task.timezone) changes.timezone = zone;
    const expires = expiry.input.value.trim();
    if (expires !== zonedIso(task.expires_at, task.timezone)) {
      if (expires) {
        const expiryTime = checkedTime(expires, zone, "失效时间");
        if (expiryTime <= new Date(changes.schedule || task.due_at)) throw new Error("失效时间须晚于下次触发时间。");
        changes.expires_at = expires;
      } else if (task.expires_at) throw new Error("已有失效时间不可清除，请输入新的未来时间或保持原值。");
    }
    if (changes.schedule && task.expires_at && !changes.expires_at && new Date(task.expires_at) <= new Date(changes.schedule)) {
      throw new Error("原有失效时间早于新的触发时间，请同时改到更晚。");
    }
    if (!Object.keys(changes).length) { status("没有需要保存的更改。"); return; }
    return manage(task, "update", changes);
  });
  save.disabled = actionBusy;
  details.append(text("p", "已过期的暂停任务须先改到未来，再单独恢复。", "hint"), grid, save);
  return details;
}
function taskActions(task) {
  const box = text("div", "", "actions");
  if (task.status !== "active" && task.status !== "paused") return box;
  const toggle = button(task.status === "active" ? "暂停" : "恢复", () => {
    if (task.status === "paused" && new Date(task.due_at).getTime() <= Date.now()) {
      throw new Error("原定时间已过：请先改期并保存，再单独恢复；不会重发旧唤醒。");
    }
    return manage(task, task.status === "active" ? "pause" : "resume");
  });
  toggle.disabled = actionBusy;
  const cancel = button("取消任务", () => {
    if (box.querySelector(".confirm-cancel")) return;
    const confirm = text("div", "", "confirm-cancel");
    confirm.append(text("p", `确定取消“${task.goal}”吗？已开始的发送无法收回。`));
    const controls = text("div", "", "actions");
    controls.append(button("确认取消", () => manage(task, "cancel"), "danger"), button("返回", () => { confirm.remove(); cancel.focus(); }));
    confirm.append(controls);
    box.append(confirm);
  }, "danger");
  cancel.disabled = actionBusy;
  box.append(toggle, cancel);
  return box;
}
function taskRow(task) {
  const item = button("", () => selectTask(task.task_ref), "task-item");
  item.classList.toggle("is-active", selectedTaskRef === task.task_ref);
  item.setAttribute("aria-current", String(selectedTaskRef === task.task_ref));
  const heading = text("div", "", "item-heading");
  const badge = text("span", statusLabels[task.status] || "状态未知", `badge ${task.status || "unknown"}`);
  heading.append(text("strong", task.goal || "（目标未记录）"), badge);
  const scope = scopes.find((row) => row.scope_ref === task.scope_ref);
  item.append(heading, text("span", `${scope ? scope.channel_name || scope.channel_id : "未归属频道"} · ${ownerLabel(task)}`, "snippet"));
  item.append(text("span", `${task.status === "active" ? "下次" : "原定"} ${date(task.due_at, task.timezone)}`, "muted"));
  if (task.occurrence) item.append(text("span", `最近执行：${stateLabels[task.occurrence.state] || "状态未知"}`, "muted"));
  return item;
}
function renderTaskRows() {
  for (const item of $("tasks").querySelectorAll(".task-item")) {
    item.classList.toggle("is-active", item.dataset.ref === selectedTaskRef);
    item.setAttribute("aria-current", String(item.dataset.ref === selectedTaskRef));
  }
}
function renderTask() {
  const heading = $("task-heading");
  heading.replaceChildren();
  if (!taskDetail) {
    heading.append(text("p", selectedTaskRef ? "正在读取任务…" : "请选择一个任务。"));
    for (const name of ["overview", "execution", "context", "source"]) $(name + "-view").replaceChildren();
    return;
  }
  const task = taskDetail;
  heading.append(text("h2", task.goal || "（目标未记录）"));
  heading.append(text("p", `${ownerLabel(task)} · ${statusLabels[task.status] || "状态未知"} · ${task.mode === "adaptive" ? "长期目标" : "一次性提醒"}`));
  const overview = $("overview-view");
  overview.replaceChildren();
  const current = section("当前任务");
  current.append(fields([
    ["交付位置", task.delivery === "dm" ? "本人私聊" : task.delivery === "channel" ? "原频道" : "未知"],
    ["创建方式", task.origin === "autonomous" ? "自主安排" : task.origin === "user" ? "用户委托" : "未知"],
    ["目标", task.goal], ["背景摘要", task.background], ["延续状态", task.continuation_state],
    [task.status === "active" ? "下次触发" : "原定时间", date(task.due_at, task.timezone)],
    ["失效时间", task.expires_at ? date(task.expires_at, task.timezone) : "未设置"],
    ["创建时间", date(task.created_at, task.timezone)], ["更新时间", date(task.updated_at, task.timezone)],
    ["修订", task.revision],
  ]));
  if (task.paused_reason) current.append(text("p", `暂停原因：${pauseReasons[task.paused_reason] || task.paused_reason}`, "warning"));
  if (task.status === "active" || task.status === "paused") current.append(editor(task), taskActions(task));
  overview.append(current);
  renderExecution(); renderContext(); renderSource();
}
function renderExecution() {
  const target = $("execution-view");
  target.replaceChildren();
  if (!taskDetail) return;
  const timeline = section(`唤醒记录（${occurrenceTotal}）`);
  const picker = text("div", "", "occurrence-list");
  for (const item of occurrences) {
    const choice = button(`#${item.sequence} · ${stateLabels[item.state] || "状态未知"}`, () => selectOccurrence(item.sequence));
    choice.classList.toggle("is-active", selectedOccurrence === item.sequence);
    choice.setAttribute("aria-pressed", String(selectedOccurrence === item.sequence));
    picker.append(choice);
  }
  if (occurrences.length) timeline.append(picker);
  else timeline.append(text("p", "尚未触发模型唤醒；任务计划不代表已联系用户。", "muted"));
  if (occurrenceHasMore) timeline.append(button("加载更早的执行", loadMoreOccurrences));
  target.append(timeline);
  const item = occurrenceDetail;
  if (!item) {
    if (selectedOccurrence != null) target.append(text("p", "正在读取本次执行…", "empty"));
    return;
  }
  const summary = section(`第 ${item.sequence} 次执行`);
  summary.append(fields([
    ["预定", date(item.scheduled_at, taskDetail.timezone)],
    ["开始", date(item.started_at, taskDetail.timezone)],
    ["模型开始", date(item.model_started_at, taskDetail.timezone)],
    ["传输开始", date(item.transport_started_at, taskDetail.timezone)],
    ["结束", date(item.finished_at, taskDetail.timezone)],
    ["状态", stateLabels[item.state] || "状态未记录"],
    ["已确认发送", `${item.confirmed_count ?? 0} 条`],
    ["失败原因", item.failure_reason || "无"],
  ]));
  if (item.transport_started && !item.confirmed_count) summary.append(text("p", "发送已开始但没有确认回执，不能视为未发送或自动重试。", "warning"));
  target.append(summary);
  const environment = section("当次执行环境");
  if (item.environment) environment.append(fields(Object.entries(item.environment).map(([key, value]) => [environmentLabels[key] || key, display(value)])));
  else environment.append(text("p", item.model_started ? "执行环境未记录。" : "尚未进入模型，执行环境未形成。", "muted"));
  target.append(environment);
  const inspection = item.inspection;
  if (!inspection) return;
  const calls = section("模型与工具调用");
  for (const call of inspection.model_calls || []) {
    const panel = section(`模型 · ${call.model || "未记录"} · ${call.status || "未知"}`);
    panel.append(fields([["尝试", call.attempt], ["耗时", call.duration_ms == null ? "未记录" : `${call.duration_ms} ms`], ["用量", display(call.usage)]]));
    if (call.request_event_ref) lazyAudit(panel, "查看真实模型输入", call.request_event_ref, "messages");
    if (call.response_event_ref) lazyAudit(panel, "查看真实模型输出", call.response_event_ref, "");
    calls.append(panel);
  }
  for (const call of inspection.tool_calls || []) {
    const panel = section(`工具 · ${call.tool_name || "未知"} · ${call.status || "未知"}`);
    panel.append(fields([["效果", call.effect || "未记录"], ["耗时", call.duration_ms == null ? "未记录" : `${call.duration_ms} ms`]]));
    if (call.call_event_ref) lazyAudit(panel, "查看实际工具输入", call.call_event_ref, "");
    if (call.result_event_ref) lazyAudit(panel, "查看实际工具结果", call.result_event_ref, "");
    calls.append(panel);
  }
  if (!inspection.model_calls?.length && !inspection.tool_calls?.length) calls.append(text("p", "本次未记录模型或工具请求。", "muted"));
  target.append(calls);
  const outputs = section("确认交付");
  if (inspection.outputs?.length) for (const output of inspection.outputs) outputs.append(disclosure("送达记录", output));
  else outputs.append(text("p", "没有可确认的交付记录。", "muted"));
  target.append(outputs);
}
async function loadAudit(ref, path, container, signal) {
  let next = 0;
  let total = 0;
  const output = text("pre", "正在读取…");
  const more = button("继续加载", readNext);
  more.hidden = true;
  container.append(output, more);
  async function readNext() {
    more.disabled = true;
    try {
      const query = new URLSearchParams({ path, offset: String(next), limit: "16000" });
      const result = await request(`${sessionsBase}/events/${encodeURIComponent(ref)}/payload?${query}`, { signal });
      if (!container.isConnected || signal.aborted) return;
      const item = result.item;
      if (!item || item.offset !== next) throw new Error("记录分页状态不一致");
      const chunk = typeof item.data === "string" ? item.data : JSON.stringify(item.data, null, 2);
      output.textContent = next ? output.textContent + chunk : chunk;
      total += chunk.length;
      if (total > 120000) { more.hidden = true; return; }
      next = item.next_offset;
      more.hidden = next == null;
    } catch (error) {
      if (error?.name !== "AbortError") output.textContent = error.message || "读取失败";
    } finally { more.disabled = false; }
  }
  await readNext();
}
function lazyAudit(container, title, ref, path) {
  const item = text("details", "", "disclosure");
  item.append(text("summary", title));
  let loaded = false;
  item.addEventListener("toggle", () => {
    if (!item.open || loaded || !ref) return;
    loaded = true;
    loadAudit(ref, path, item, detailController.signal).catch(showError);
  });
  container.append(item);
}
function renderContext() {
  const target = $("context-view");
  target.replaceChildren();
  if (!taskDetail) return;
  const item = occurrenceDetail;
  if (!item) return target.append(text("p", occurrences.length ? "选择一次执行以检查当时的上下文。" : "尚无执行上下文。", "empty"));
  const task = section("当次任务输入");
  if (item.trigger) task.append(fields(Object.entries(item.trigger).map(([key, value]) => [
    triggerLabels[key] || key,
    triggerValue(key, value, taskDetail.timezone),
  ])));
  else task.append(text("p", "本次未记录任务输入，不能用当前任务文本补全历史。", "muted"));
  target.append(task);
  const context = item.inspection?.context;
  const selected = section("实际选中的上下文");
  if (!context?.captured) selected.append(text("p", "本次未记录上下文快照，不能用当前配置重建。", "muted"));
  else {
    const selection = context.selection || {};
    const ordinary = selection.ordinary_chat;
    const ordinaryLabel = ordinary == null ? "旧记录未记录" : ordinary.status === "absent"
      ? "目标频道无当前普通会话" : ordinary.status === "empty"
        ? `会话 ${ordinary.session_ref || "未知"} · 暂无可用轮次` : ordinary.session_ref || "普通聊天会话未记录";
    selected.append(fields([
      ["普通聊天快照", ordinaryLabel],
      ["普通聊天轮次", (ordinary?.included_turn_refs || []).join("、") || "无"],
      ["提醒历史轮次", (selection.included_turn_refs || []).join("、") || "无"],
      ["捕获状态", captureLabels[context.capture_status] || "未知"],
    ]));
    if (context.blocks?.length) selected.append(disclosure("注入块", context.blocks));
    lazyAudit(selected, "查看实际选中消息", context.event_ref, "messages");
    lazyAudit(selected, "查看当次系统上下文", context.event_ref, "system");
  }
  target.append(selected);
  const decision = section("本次任务决议");
  if (item.decision) decision.append(disclosure("已记录决议", item.decision));
  else decision.append(text("p", "没有已记录的任务结束决议；确认发送不等于任务完成。", "muted"));
  target.append(decision);
}
function renderSource() {
  const target = $("source-view");
  target.replaceChildren();
  if (!taskDetail) return;
  const info = section("任务创建来源");
  const source = taskDetail.source;
  if (!source) info.append(text("p", "来源聊天轮次未记录或已不可验证。", "muted"));
  else {
    const scope = scopes.find((item) => item.scope_ref === source.scope_ref);
    info.append(fields([
      ["频道", scope ? scopeLabel(scope) : source.scope_ref || "未记录"],
      ["普通聊天会话", source.session_ref], ["来源轮次", source.turn_ref],
      ["原始输入摘要", source.input_preview || "未记录"],
    ]));
    if (source.turn_ref) {
      const full = section("原始用户输入");
      full.append(button("读取原始记录", async () => {
        const result = await request(`${sessionsBase}/turns/${encodeURIComponent(source.turn_ref)}/events`, { signal: detailController.signal });
        const event = result.items?.find((entry) => entry.event_type === "user_input");
        if (!event) { full.append(text("p", "来源轮次未记录用户输入。", "muted")); return; }
        lazyAudit(full, "展开来源用户输入", event.event_ref, "content");
      }));
      target.append(info, full);
      return;
    }
  }
  target.append(info);
}
function showTab(name) {
  activeTab = name;
  for (const button of document.querySelectorAll("[data-tab]")) button.setAttribute("aria-selected", String(button.dataset.tab === name));
  for (const value of ["overview", "execution", "context", "source"]) $(value + "-view").hidden = name !== value;
}
async function selectOccurrence(sequence) {
  selectedOccurrence = sequence;
  occurrenceDetail = null;
  renderExecution(); renderContext();
  const current = detailGeneration;
  const ref = selectedTaskRef;
  try {
    const result = await request(`/tasks/${encodeURIComponent(ref)}/occurrences/${sequence}`, { signal: detailController.signal });
    if (current !== detailGeneration || selectedOccurrence !== sequence) return;
    occurrenceDetail = result.item;
    renderExecution(); renderContext();
  } catch (error) { if (current === detailGeneration) showError(error); }
}
async function loadMoreOccurrences() {
  if (!selectedTaskRef || occurrenceLoading || !occurrenceHasMore) return;
  const current = detailGeneration;
  occurrenceLoading = true;
  try {
    const result = await request(`/tasks/${encodeURIComponent(selectedTaskRef)}/occurrences?limit=100&offset=${occurrences.length}`, { signal: detailController.signal });
    if (current !== detailGeneration) return;
    if (!Array.isArray(result.items) || (result.has_more && !result.items.length)) throw new Error("执行记录分页状态无效");
    occurrences.push(...result.items);
    occurrenceHasMore = Boolean(result.has_more);
    occurrenceTotal = result.total_count ?? occurrences.length;
    renderExecution();
  } catch (error) { if (current === detailGeneration) showError(error); }
  finally { occurrenceLoading = false; }
}
async function selectTask(ref) {
  detailController.abort();
  detailController = new AbortController();
  const current = ++detailGeneration;
  selectedTaskRef = ref;
  selectedOccurrence = null;
  occurrenceDetail = null;
  taskDetail = null;
  occurrences = [];
  occurrenceHasMore = false;
  occurrenceTotal = 0;
  occurrenceLoading = false;
  renderTaskRows(); renderTask();
  document.querySelector(".workspace").dataset.mobileView = "detail";
  try {
    const taskPath = `/tasks/${encodeURIComponent(ref)}`;
    const [task, history] = await Promise.all([
      request(taskPath, { signal: detailController.signal }),
      request(`${taskPath}/occurrences?limit=100&offset=0`, { signal: detailController.signal }),
    ]);
    if (current !== detailGeneration) return;
    taskDetail = task.item;
    occurrences = history.items || [];
    occurrenceHasMore = Boolean(history.has_more);
    occurrenceTotal = history.total_count ?? occurrences.length;
    renderTask();
    if (occurrences.length) await selectOccurrence(occurrences[0].sequence);
  } catch (error) { if (current === detailGeneration) showError(error); }
}
async function loadScopes(signal) {
  const items = [];
  for (let next = 0; ; ) {
    const result = await request(`/scopes?limit=100&offset=${next}`, { signal });
    if (!Array.isArray(result.items)) throw new Error("频道列表响应无效");
    items.push(...result.items);
    if (!result.has_more) break;
    if (!result.items.length) throw new Error("频道分页状态无效");
    next += result.items.length;
  }
  scopes = items;
  const picker = $("scope-select");
  picker.replaceChildren();
  for (const [value, label] of [["all", "全部频道"], ["unassigned", "未归属频道"]]) {
    const option = text("option", label); option.value = value; picker.append(option);
  }
  for (const scope of scopes) {
    const option = text("option", `${scopeLabel(scope)} · ${scope.task_count} 项`);
    option.value = scope.scope_ref;
    picker.append(option);
  }
  if (!["all", "unassigned"].includes(selectedScope) && !scopes.some((scope) => scope.scope_ref === selectedScope)) selectedScope = "all";
  picker.value = selectedScope;
}
async function loadSessions(signal) {
  const picker = $("session-select");
  picker.replaceChildren();
  const all = text("option", "全部来源会话"); all.value = "all"; picker.append(all);
  const unlinked = text("option", "未关联来源会话"); unlinked.value = "unlinked"; picker.append(unlinked);
  if (selectedScope === "all") {
    if (selectedSession !== "unlinked") selectedSession = "all";
    picker.value = selectedSession;
    picker.disabled = false;
    return;
  }
  if (selectedScope === "unassigned") {
    selectedSession = "all";
    picker.value = "all";
    picker.disabled = true;
    return;
  }
  picker.disabled = false;
  const items = [];
  for (let next = 0; ; ) {
    const result = await request(`/sessions?scope_ref=${encodeURIComponent(selectedScope)}&limit=100&offset=${next}`, { signal });
    if (!Array.isArray(result.items)) throw new Error("来源会话响应无效");
    items.push(...result.items);
    if (!result.has_more) break;
    if (!result.items.length) throw new Error("会话分页状态无效");
    next += result.items.length;
  }
  for (const session of items) {
    const option = text("option", `#${session.sequence} · ${session.status === "active" ? "当前" : "历史"} · ${session.task_count} 项 · ${date(session.created_at)}`);
    option.value = session.session_ref;
    picker.append(option);
  }
  if (!items.some((item) => item.session_ref === selectedSession) && selectedSession !== "unlinked") selectedSession = "all";
  picker.value = selectedSession;
}
function beginView() {
  viewController.abort();
  viewController = new AbortController();
  generation += 1;
  loading = false;
  offset = 0;
  loaded = 0;
  hasMore = false;
  $("tasks").replaceChildren();
  $("count").textContent = "";
  status("正在读取…");
  $("more").hidden = true;
  $("refresh").disabled = actionBusy;
  return generation;
}
async function loadMore() {
  if (!pageActive || loading) return;
  const current = generation;
  loading = true;
  $("more").disabled = true;
  $("refresh").disabled = true;
  status("正在读取…");
  try {
    const query = new URLSearchParams({ scope_ref: selectedScope, session_ref: selectedSession, status: selectedStatus, q: searchQuery, limit: String(batchSize), offset: String(offset) });
    const result = await request(`/tasks?${query}`, { signal: viewController.signal });
    if (!Array.isArray(result.items)) throw new Error("任务列表响应无效");
    if (current !== generation) return;
    for (const item of result.items) {
      const row = taskRow(item);
      row.dataset.ref = item.task_ref;
      $("tasks").append(row);
    }
    offset += result.items.length;
    loaded += result.items.length;
    hasMore = Boolean(result.has_more);
    $("count").textContent = `(${Number.isSafeInteger(result.total_count) ? result.total_count : loaded})`;
    $("more").hidden = !hasMore;
    if (!loaded) $("tasks").append(text("p", "当前筛选下没有任务。", "empty"));
    status("已同步");
  } catch (error) {
    if (current === generation) showError(error);
  } finally {
    if (current === generation) {
      loading = false;
      $("more").disabled = false;
      $("refresh").disabled = actionBusy;
    }
  }
}
async function refreshView(keepCount = false) {
  const target = keepCount ? Math.max(batchSize, loaded) : batchSize;
  const selected = selectedTaskRef;
  const current = beginView();
  try { await loadScopes(viewController.signal); await loadSessions(viewController.signal); }
  catch (error) { if (current === generation) showError(error); return false; }
  do {
    await loadMore();
  } while (current === generation && hasMore && loaded < target && !$("status").classList.contains("error"));
  if (current !== generation) return false;
  if (selected && [...$("tasks").querySelectorAll(".task-item")].some((item) => item.dataset.ref === selected)) await selectTask(selected);
  else if (selected) {
    selectedTaskRef = "";
    detailController.abort();
    taskDetail = null;
    renderTask();
    document.querySelector(".workspace").dataset.mobileView = "list";
  }
  return !$("status").classList.contains("error");
}
async function manage(task, action, changes = {}) {
  if (actionBusy) return;
  if (!task.task_ref || !Number.isSafeInteger(task.revision)) { status("任务引用或修订号缺失，请刷新后重试。", true); return; }
  actionBusy = true;
  for (const item of document.querySelectorAll("button, select")) item.disabled = true;
  status("正在提交，请勿重复操作…");
  let outcome;
  try {
    const result = await request(`/tasks/${encodeURIComponent(task.task_ref)}/manage`, {
      method: "POST", signal: pageController.signal,
      body: { scope_ref: selectedScope === "unassigned" ? "all" : selectedScope, revision: task.revision, action, ...changes },
    });
    if (!result.item) throw new Error("操作结果无法确认；请刷新状态，不要重复提交。");
    outcome = "操作已确认，任务状态已刷新。";
  } catch (error) {
    if (error?.name === "AbortError") return;
    outcome = error.status === 409 || error.code === "revision_conflict"
      ? `任务已被其他操作修改：${error.message}；请检查最新修订。`
      : `${error.message || "操作结果不确定"}；请核对状态，不要直接重复提交。`;
  } finally {
    actionBusy = false;
    for (const item of document.querySelectorAll("button, select")) item.disabled = false;
    const refreshed = pageActive && await refreshView(true);
    if (pageActive) status(refreshed ? outcome || "操作结果不确定，请检查当前状态。"
      : `${outcome || "操作结果不确定"} 刷新失败，请手动核对。`, !refreshed || !outcome?.startsWith("操作已确认"));
  }
}
$("scope-select").addEventListener("change", () => { selectedScope = $("scope-select").value; selectedSession = "all"; refreshView(); });
$("session-select").addEventListener("change", () => { selectedSession = $("session-select").value; refreshView(); });
$("status-select").addEventListener("change", () => { selectedStatus = $("status-select").value; refreshView(); });
$("search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { searchQuery = $("search-input").value.trim(); refreshView(); }, 250);
});
$("refresh").addEventListener("click", () => refreshView(true));
$("more").addEventListener("click", () => loadMore());
$("back-to-tasks").addEventListener("click", () => { document.querySelector(".workspace").dataset.mobileView = "list"; });
for (const tab of document.querySelectorAll("[data-tab]")) tab.addEventListener("click", () => showTab(tab.dataset.tab));
window.addEventListener("pagehide", () => { pageActive = false; pageController.abort(); viewController.abort(); detailController.abort(); });
window.addEventListener("pageshow", (event) => {
  if (event.persisted) { pageActive = true; pageController = new AbortController(); detailController = new AbortController(); refreshView(true); }
});
refreshView();
