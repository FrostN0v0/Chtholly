"use strict";

const apiBase = "/api/llm-chat/reminders";
const bridgeOrigin = new URL(document.URL).origin;
const inFrame = window.parent !== window;
const pending = new Map();
let bridgeReady = !inFrame;
let pageActive = true;
let controller = new AbortController();
let loading = false;
let offset = 0;
let loaded = 0;
let generation = 0;
const batchSize = 40;
const $ = (id) => document.getElementById(id);

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

function bridgeApi(url, signal) {
  if (signal.aborted || !pageActive) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
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
            url, method: "GET", timeout: 30000, headers: { "Content-Type": "application/json" },
          } }, bridgeOrigin);
        } catch (error) { item.finish(error); }
      },
    };
    const onAbort = () => item.finish(abortError());
    const timer = setTimeout(() => item.finish(new Error("WebUI 读取超时，请刷新后重试。")), 40000);
    pending.set(id, item);
    signal.addEventListener("abort", onAbort, { once: true });
    item.send();
  });
}

async function request(path, signal) {
  const url = apiUrl(path);
  let data;
  if (inFrame) data = await bridgeApi(url, signal);
  else {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store", signal });
    data = await response.json().catch(() => ({ success: false, message: "服务器响应无法解析" }));
    if (!response.ok) throw new Error(data.message || `请求失败（${response.status}）`);
  }
  if (signal.aborted || !pageActive) throw abortError();
  if (!data || typeof data !== "object" || data.success !== true || !Array.isArray(data.items)) {
    throw new Error(data?.message || "服务器响应无法解析");
  }
  return data;
}

function text(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
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
const statusLabels = { active: "待执行", paused: "已暂停", completed: "已结束", cancelled: "已取消" };
const stateLabels = {
  claimed: "已领取，尚未开始", model_started: "模型处理中", dm_started: "私聊目标建立中",
  transport_started: "发送中，结果未确认", confirmed: "已有确认回执，结算中",
  completed: "已结算", paused: "已暂停", cancelled: "已取消",
  expired_recovered: "恢复时已过期", unknown: "执行结果不确定",
};
function card(task) {
  const node = text("article", "", "card");
  const head = text("div", "", "card-head");
  head.append(text("h2", task.goal || "（目标未记录）"));
  const badge = text("span", statusLabels[task.status] || "状态未知", "badge");
  if (task.status === "active" || task.status === "paused") badge.classList.add(task.status);
  head.append(badge);
  node.append(head);
  const meta = text("div", "", "meta");
  const type = task.mode === "adaptive" ? "长期目标" : task.mode === "once" ? "一次性提醒" : "类型未知";
  const origin = task.origin === "user" ? "用户委托" : task.origin === "autonomous" ? "自主安排" : "来源未记录";
  const target = task.delivery === "dm" ? "本人私聊" : task.delivery === "channel" ? "原会话" : "位置未记录";
  const dueLabel = task.status === "active" ? "下次触发" : "原定触发";
  meta.append(text("span", `${type} · ${origin} · ${target}`));
  meta.append(text("span", `${dueLabel}：${date(task.due_at, task.timezone)}`));
  meta.append(text("span", `更新：${date(task.updated_at, task.timezone)}`));
  node.append(meta);
  const run = text("div", "", "run");
  const occurrence = task.occurrence;
  if (occurrence) {
    run.append(text("strong", "最近一次定时唤醒"));
    run.append(text("p", `预定：${date(occurrence.scheduled_at, task.timezone)} · ${stateLabels[occurrence.state] || "结果未记录"}`));
    run.append(text("p", `真实确认发送：${Number.isSafeInteger(occurrence.confirmed_count) ? occurrence.confirmed_count : 0} 条；未确认的发送不会计入。`));
    if (task.mode === "adaptive" && task.status === "active") {
      run.append(text("p", `当前任务保留下一次触发点：${date(task.due_at, task.timezone)}`));
    }
  } else {
    run.append(text("strong", "尚无定时唤醒记录"));
    run.append(text("p", "仅记录任务计划，不代表已联系用户。"));
  }
  node.append(run);
  return node;
}

async function loadMore(reset = false) {
  if (reset) {
    controller.abort();
    controller = new AbortController();
    generation += 1;
    offset = 0;
    loaded = 0;
    $("tasks").replaceChildren();
    $("count").textContent = "";
    $("more").hidden = true;
    loading = false;
  }
  if (!pageActive || loading) return;
  const current = generation;
  const signal = controller.signal;
  loading = true;
  $("more").disabled = true;
  $("refresh").disabled = true;
  $("status").classList.remove("error");
  $("status").textContent = "正在读取…";
  try {
    const result = await request(`/tasks?limit=${batchSize}&offset=${offset}`, signal);
    if (current !== generation) return;
    for (const item of result.items) $("tasks").append(card(item));
    offset += result.items.length;
    loaded += result.items.length;
    $("count").textContent = `已显示 ${loaded} 项`;
    $("status").textContent = "已同步";
    $("more").hidden = !result.has_more || offset >= 10000;
    if (!loaded) $("tasks").append(text("p", "暂无提醒任务。", "empty"));
  } catch (error) {
    if (current === generation && error?.name !== "AbortError") {
      $("status").textContent = error instanceof Error ? error.message : "读取失败";
      $("status").classList.add("error");
      $("more").hidden = offset === 0;
    }
  } finally {
    if (current === generation) {
      loading = false;
      $("more").disabled = false;
      $("refresh").disabled = false;
    }
  }
}
$("refresh").addEventListener("click", () => loadMore(true));
$("more").addEventListener("click", () => loadMore());
window.addEventListener("pagehide", () => { pageActive = false; controller.abort(); });
window.addEventListener("pageshow", (event) => {
  if (event.persisted) { pageActive = true; loadMore(true); }
});
loadMore(true);
