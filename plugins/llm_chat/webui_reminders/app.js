"use strict";

const apiBase = "/api/llm-chat/reminders";
const bridgeOrigin = new URL(document.URL).origin;
const inFrame = window.parent !== window;
const pending = new Map();
const batchSize = 40;
const $ = (id) => document.getElementById(id);
let bridgeReady = !inFrame;
let pageActive = true;
let pageController = new AbortController();
let viewController = new AbortController();
let generation = 0;
let loading = false;
let actionBusy = false;
let offset = 0;
let loaded = 0;
let scopes = [];
let selectedScope = "all";
const groups = new Map();

function abortError() { return new DOMException("读取已停止", "AbortError"); }
function apiUrl(path) {
  if (typeof path !== "string" || !path.startsWith("/")) throw new Error("无效请求路径");
  const url = new URL(`${apiBase}${path}`, bridgeOrigin);
  if (url.origin !== bridgeOrigin || !url.pathname.startsWith(`${apiBase}/`) || url.hash) throw new Error("无效请求路径");
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
  const url = apiUrl(path);
  if (method !== "GET" && method !== "POST") throw new Error("不支持的请求方法");
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
  node.textContent = value;
  if (className) node.className = className;
  return node;
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
    const offsetText = `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${offsetText}`;
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
  paused_by_user: "用户已暂停",
  task_paused: "已暂停任务",
  missing_reminder_decision: "本次唤醒未提交结束决议，已停止后续触发",
  occurrence_result_unknown: "发送结果未确认，请核对接收情况",
  preparation_failed: "唤醒准备失败，未开始发送",
  model_unavailable: "当前模型不可用",
  model_failed: "模型执行失败",
  transport_result_unknown: "发送结果不确定，请勿直接重试",
};
function scopeLabel(scope) {
  return `${scope.channel_name || scope.channel_id || "未命名会话"} · ${scope.platform || "未知平台"} · ${scope.account_id || "未知账号"}`;
}
function ownerLabel(task) {
  const owner = task.owner_user_id || "未记录统一身份";
  return task.platform_user_id && task.platform_user_id !== owner
    ? `${owner}（平台用户 ${task.platform_user_id}）` : owner;
}
function groupFor(task) {
  const scope = scopes.find((item) => item.scope_ref === task.scope_ref);
  const label = scope ? scopeLabel(scope) : task.scope_ref ? "未列出的会话" : "未归属会话";
  const owner = ownerLabel(task);
  const key = `${task.scope_ref || ""}\0${task.owner_user_id || ""}\0${task.platform_user_id || ""}`;
  if (groups.has(key)) return groups.get(key);
  const group = text("section", "", "task-group");
  const heading = text("h2", `${label} · 用户 ${owner}`, "group-title");
  group.append(heading);
  $("tasks").append(group);
  groups.set(key, group);
  return group;
}
function field(label, value, maxLength, multiline = false) {
  const wrapper = text("label", "", "field");
  const caption = text("span", label);
  const input = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) input.type = "text";
  input.value = value || "";
  input.maxLength = maxLength;
  if (multiline) input.rows = 3;
  wrapper.append(caption, input);
  return { wrapper, input };
}
function editor(task) {
  const details = text("details", "", "editor");
  details.append(text("summary", "编辑任务"));
  details.setAttribute("aria-label", `编辑任务：${task.goal}，用户 ${ownerLabel(task)}`);
  const help = text("p", "修改时间请填写带明确偏移的 ISO 8601 时间，并与 IANA 时区一致。已暂停且触发时间已过的任务，请先改到未来，再单独恢复。", "hint");
  const grid = text("div", "", "edit-grid");
  const goal = field("任务目标", task.goal, 1000, true);
  const background = field("背景摘要", task.background, 3000, true);
  const continuation = field("延续状态", task.continuation_state, 2000, true);
  const schedule = field("下次触发（ISO 8601，含偏移）", zonedIso(task.due_at, task.timezone), 40);
  const timezone = field("IANA 时区", task.timezone || "UTC", 64);
  const expiry = field(task.expires_at ? "失效时间（只能改期，不可清除）" : "失效时间（可选，ISO 8601，含偏移）", zonedIso(task.expires_at, task.timezone), 40);
  schedule.input.placeholder = "2026-10-01T09:00:00+08:00";
  expiry.input.placeholder = task.expires_at ? "请输入新的未来时间" : "可留空";
  for (const control of [goal, background, continuation, schedule, timezone, expiry]) grid.append(control.wrapper);
  const save = text("button", "保存修改");
  save.type = "button";
  save.setAttribute("aria-label", `保存任务修改：${task.goal}，用户 ${ownerLabel(task)}`);
  save.disabled = actionBusy;
  save.addEventListener("click", () => {
    try {
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
          const due = new Date(changes.schedule || task.due_at);
          if (expiryTime <= due) throw new Error("失效时间须晚于下次触发时间。");
          changes.expires_at = expires;
        } else if (task.expires_at) throw new Error("已有失效时间不能留空清除；请输入新的未来时间，或保持原值。");
      }
      if (changes.schedule && task.expires_at && !changes.expires_at && new Date(task.expires_at) <= new Date(changes.schedule)) {
        throw new Error("原有失效时间已早于新的触发时间，请同时改到更晚的失效时间。");
      }
      if (!Object.keys(changes).length) { status("没有需要保存的更改。"); return; }
      manage(task, "update", changes);
    } catch (error) { status(error.message, true); }
  });
  details.append(help, grid, save);
  return details;
}
function card(task) {
  const node = text("article", "", "card");
  const head = text("div", "", "card-head");
  head.append(text("h3", task.goal || "（目标未记录）"));
  const badge = text("span", statusLabels[task.status] || "状态未知", "badge");
  if (task.status === "active" || task.status === "paused") badge.classList.add(task.status);
  head.append(badge);
  node.append(head);
  const meta = text("div", "", "meta");
  const type = task.mode === "adaptive" ? "长期目标" : task.mode === "once" ? "一次性提醒" : "类型未知";
  const origin = task.origin === "user" ? "用户委托" : task.origin === "autonomous" ? "自主安排" : "来源未记录";
  const target = task.delivery === "dm" ? "本人私聊" : task.delivery === "channel" ? "原会话" : "位置未记录";
  meta.append(text("span", `目标位置：${target} · ${type} · ${origin}`));
  meta.append(text("span", `所属用户：${ownerLabel(task)}`));
  meta.append(text("span", `${task.status === "active" ? "下次触发" : "原定触发"}：${date(task.due_at, task.timezone)}`));
  meta.append(text("span", `更新：${date(task.updated_at, task.timezone)} · 修订 ${task.revision ?? "未知"}`));
  if (task.expires_at) meta.append(text("span", `失效：${date(task.expires_at, task.timezone)}`));
  if (task.paused_reason) meta.append(text("span", `暂停原因：${pauseReasons[task.paused_reason] || task.paused_reason}`));
  node.append(meta);
  const run = text("div", "", "run");
  const occurrence = task.occurrence;
  if (occurrence) {
    run.append(text("strong", "最近一次定时唤醒"));
    run.append(text("p", `预定：${date(occurrence.scheduled_at, task.timezone)} · ${stateLabels[occurrence.state] || "结果未记录"}`));
    const count = Number.isSafeInteger(occurrence.confirmed_count) ? occurrence.confirmed_count : 0;
    run.append(text("p", `真实确认发送：${count} 条。确认回执仅代表消息已交付，不代表提醒目标已完成。`));
    if (occurrence.transport_started && occurrence.state === "unknown") {
      run.append(text("p", "发送已开始但结果不确定；请核对实际接收情况，不要盲目恢复旧时间。", "warning"));
    } else if (occurrence.transport_started && !count) {
      run.append(text("p", "曾开始外部传输，尚无已确认回执。", "warning"));
    }
  } else {
    run.append(text("strong", "尚无定时唤醒记录"));
    run.append(text("p", "仅记录任务计划，不代表已联系用户。"));
  }
  node.append(run);
  if (task.status === "active" || task.status === "paused") {
    const actions = text("div", "", "actions");
    const toggle = text("button", task.status === "active" ? "暂停" : "恢复");
    toggle.type = "button";
    toggle.disabled = actionBusy;
    toggle.setAttribute("aria-label", `${task.status === "active" ? "暂停" : "恢复"}任务：${task.goal}，用户 ${ownerLabel(task)}`);
    toggle.addEventListener("click", () => {
      if (task.status === "paused" && new Date(task.due_at).getTime() <= Date.now()) {
        status("原定时间已过：请先编辑到未来时间并保存，再单独恢复；不会自动重发旧唤醒。", true);
        return;
      }
      manage(task, task.status === "active" ? "pause" : "resume");
    });
    const cancel = text("button", "取消任务", "danger");
    cancel.type = "button";
    cancel.disabled = actionBusy;
    cancel.setAttribute("aria-label", `取消任务：${task.goal}，用户 ${ownerLabel(task)}`);
    cancel.addEventListener("click", () => {
      if (node.querySelector(".confirm-cancel")) return;
      const confirmation = text("div", "", "confirm-cancel");
      confirmation.setAttribute("role", "group");
      confirmation.setAttribute("aria-label", `确认取消任务：${task.goal}`);
      confirmation.append(text("p", `确定取消任务“${task.goal}”吗？此操作不能撤销，已开始的发送也无法收回。`));
      const controls = text("div", "", "actions");
      const approve = text("button", "确认取消", "danger");
      approve.type = "button";
      approve.addEventListener("click", () => manage(task, "cancel"));
      const dismiss = text("button", "返回");
      dismiss.type = "button";
      dismiss.addEventListener("click", () => { confirmation.remove(); cancel.focus(); });
      controls.append(approve, dismiss);
      confirmation.append(controls);
      node.append(confirmation);
      approve.focus();
    });
    actions.append(toggle, cancel);
    node.append(editor(task), actions);
  }
  return node;
}

async function loadScopes(signal) {
  const items = [];
  let next = 0;
  while (next <= 10000) {
    const result = await request(`/scopes?limit=100&offset=${next}`, { signal });
    if (!Array.isArray(result.items)) throw new Error("会话列表响应无效");
    items.push(...result.items);
    next += result.items.length;
    if (!result.has_more) break;
    if (!result.items.length || next > 10000) throw new Error("会话列表过长，请稍后刷新。");
  }
  if (signal.aborted) return;
  scopes = items;
  const picker = $("scope-select");
  picker.replaceChildren();
  const all = text("option", `所有任务（已列出会话 ${scopes.reduce((sum, scope) => sum + (scope.task_count || 0), 0)} 项，含未归属任务）`);
  all.value = "all";
  picker.append(all);
  for (const scope of scopes) {
    const option = text("option", `${scopeLabel(scope)} · ${scope.task_count} 项（待执行 ${scope.active_count}）`);
    option.value = scope.scope_ref;
    picker.append(option);
  }
  if (selectedScope !== "all" && !scopes.some((scope) => scope.scope_ref === selectedScope)) selectedScope = "all";
  picker.value = selectedScope;
}
function beginView() {
  viewController.abort();
  viewController = new AbortController();
  generation += 1;
  loading = false;
  offset = 0;
  loaded = 0;
  groups.clear();
  $("tasks").replaceChildren();
  $("count").textContent = "";
  $("more").hidden = true;
  $("refresh").disabled = actionBusy;
  return generation;
}
async function loadMore() {
  if (!pageActive || loading) return;
  const current = generation;
  const signal = viewController.signal;
  loading = true;
  $("more").disabled = true;
  $("refresh").disabled = true;
  status("正在读取…");
  try {
    const result = await request(`/tasks?scope_ref=${encodeURIComponent(selectedScope)}&limit=${batchSize}&offset=${offset}`, { signal });
    if (!Array.isArray(result.items)) throw new Error("任务列表响应无效");
    if (current !== generation) return;
    for (const item of result.items) groupFor(item).append(card(item));
    offset += result.items.length;
    loaded += result.items.length;
    $("count").textContent = `已显示 ${loaded} 项`;
    status("已同步");
    $("more").hidden = !result.has_more || offset >= 10000;
    if (!loaded) $("tasks").append(text("p", "此范围暂无提醒任务。", "empty"));
  } catch (error) {
    if (current === generation && error?.name !== "AbortError") status(error.message || "读取失败", true);
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
  const current = beginView();
  try { await loadScopes(viewController.signal); }
  catch (error) {
    if (current !== generation || error?.name === "AbortError") return;
    status(error.message || "读取会话失败", true);
    return;
  }
  if (current !== generation) return false;
  do {
    await loadMore();
  } while (current === generation && loaded && loaded < target && !$("more").hidden && !$("status").classList.contains("error"));
  return !$("status").classList.contains("error");
}
async function manage(task, action, changes = {}) {
  if (actionBusy) return;
  if (!task.task_ref || !Number.isSafeInteger(task.revision)) { status("任务引用或修订号缺失，请刷新后重试。", true); return; }
  actionBusy = true;
  $("refresh").disabled = true;
  $("scope-select").disabled = true;
  $("more").disabled = true;
  for (const button of $("tasks").querySelectorAll("button")) button.disabled = true;
  status("正在提交，请勿重复操作…");
  let outcome;
  try {
    const result = await request(`/tasks/${encodeURIComponent(task.task_ref)}/manage`, {
      method: "POST", signal: pageController.signal,
      body: { scope_ref: selectedScope, revision: task.revision, action, ...changes },
    });
    if (!result.item) throw new Error("操作结果无法确认；请检查刷新后的状态，不要重复提交。");
    outcome = "操作已确认，已刷新任务状态。";
  } catch (error) {
    if (error?.name === "AbortError") return;
    outcome = error.status === 409 || error.code === "revision_conflict"
      ? `任务已被其他操作修改：${error.message}；已读取最新修订，请检查后再操作。`
      : `${error.message || "操作结果不确定"}；正在只读刷新，请检查当前状态，不要直接重复提交。`;
  } finally {
    const refreshed = await refreshView(true);
    actionBusy = false;
    $("scope-select").disabled = false;
    $("refresh").disabled = false;
    for (const button of $("tasks").querySelectorAll("button")) button.disabled = false;
    if (pageActive) status(refreshed ? (outcome || "操作结果不确定，请检查当前状态。") :
      `${outcome || "操作结果不确定"} 刷新失败，请手动刷新以核对最新状态。`, !refreshed || !outcome?.startsWith("操作已确认"));
  }
}
$("scope-select").addEventListener("change", () => {
  selectedScope = $("scope-select").value;
  beginView();
  loadMore();
});
$("refresh").addEventListener("click", () => refreshView(true));
$("more").addEventListener("click", loadMore);
window.addEventListener("pagehide", () => { pageActive = false; pageController.abort(); viewController.abort(); });
window.addEventListener("pageshow", (event) => {
  if (event.persisted) { pageActive = true; pageController = new AbortController(); refreshView(); }
});
refreshView();
