"use strict";

const apiBase = "/api/llm-chat/sessions";
const labels = {
  active: "进行中", closed: "已关闭", sealed: "已封存", confirmed: "已确认送达",
  requested: "已发起", succeeded: "成功", failed: "失败", cancelled: "已取消",
  partial: "部分完成", rejected: "已拒绝", completed: "已完成", recorded: "已记录",
  running: "生成中", pending: "等待中", none: "无副作用", not_recorded: "本轮未记录",
  unknown: "未知", captured: "已捕获", redacted: "已脱敏", initial: "首次创建",
  complete: "完整捕获", overflow: "超出捕获上限",
  legacy_aggregate: "旧版尝试聚合（请求数未知）", mixed: "逐请求与旧版聚合混合",
  idle: "空闲超时", turn_limit: "轮次上限", runtime_change: "运行时变更",
  hard_reset: "硬重置", webui_new: "新会话", webui_rollover: "续接会话",
  webui_hard_reset: "硬重置", legacy_import: "历史导入", persona_change: "切换人格",
  user_input: "用户输入", assistant_output: "旧版回复摘要", assistant_tool_call: "工具调用",
  message_delivery: "确认送达消息", turn_timing: "用户输入接收时间",
  legacy_incomplete: "旧版记录（送达审计不完整）", not_delivered: "尚无确认送达",
  tool_result: "工具结果", model_attempt: "生成尝试（非单次请求）", model_request: "模型请求",
  model_response: "模型响应", context_snapshot: "上下文注入快照", context_selection: "上下文选择",
  persona_state: "人格与记忆快照", engagement: "回应意向",
  current_state: "当前状态", current_speaker: "当前发言人", relationship_style: "关系与回应方式",
  current_participant_ref: "当前发言人引用", self_reference_attached: "本轮角色参考图",
  persona_reference_configured: "\u89d2\u8272\u53c2\u8003\u56fe\u5df2\u914d\u7f6e",
  user_profile: "用户画像", relevant_memories: "相关记忆", agent_session: "会话交接与锚点",
  recent_impression: "近期印象", reply_intent: "回复意图", system: "完整系统指令",
  messages: "实际选中消息", persona: "人格快照", selection: "历史选择证据", budgets: "历史预算",
  estimated_tokens: "预估输入", full_session_tokens: "会话估算", max_input_tokens: "输入上限",
  output_reserve_tokens: "输出预留", rollover_ratio: "续接阈值", minimum_recent_turns: "最少近期轮次",
  inline_event_chars: "内联事件字数", included_count: "选中轮次", excluded_count: "排除轮次",
  relationship: "\u5173\u7cfb\u4e0e\u60c5\u7eea", relationship_evaluation: "\u5173\u7cfb\u8bc4\u4f30", response_decision: "\u81ea\u4e3b\u56de\u5e94",
  silent: "\u81ea\u4e3b\u6c89\u9ed8", declined: "\u5df2\u7ec8\u6b62 (declined)", superseded: "\u5df2\u88ab\u65b0\u8f6e\u6b21\u66ff\u4ee3",
};
const $ = (id) => document.getElementById(id);
const state = {
  scopes: [], scope: null, sessions: [], session: null, detail: null, turns: [], turn: null,
  inspection: null, events: [], navigation: 0, scopeLoad: 0, sessionLoad: 0,
  pollTimer: null, pollBusy: false, payload: null, action: null, actionBusy: false,
};

function node(tag, className = "", text = "") {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = String(text ?? "");
  return item;
}
function button(text, action, className = "") {
  const item = node("button", className, text);
  item.type = "button";
  if (text) item.dataset.focusKey = text;
  item.addEventListener("click", () => Promise.resolve().then(action).catch(showError));
  return item;
}
function text(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "未记录";
}
function short(value, limit = 140) {
  const source = text(value).replace(/\s+/g, " ").trim();
  return source.length > limit ? `${source.slice(0, limit)}…` : source;
}
function label(value) { return labels[value] || value || "未知"; }
function date(value) {
  if (!value) return "时间未记录";
  const source = /(?:Z|[+-]\d\d:\d\d)$/i.test(value) ? value : `${value}Z`;
  const parsed = new Date(source);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}
function number(value) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "未知"; }
function duration(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "耗时未记录";
  return value < 1000 ? `${number(value)} ms` : `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
}
function turnDuration(turn) {
  if (turn.duration_source === "received_to_confirmed_delivery") return `总耗时 ${duration(turn.duration_ms)}（接收输入 → 最后确认送达）`;
  if (turn.duration_source === "legacy_lifecycle") return `旧版生命周期估算 ${duration(turn.duration_ms)}（不含完整准备阶段，非送达耗时）`;
  if (turn.duration_source === "running") {
    const basis = turn.received_at ? "接收输入起" : "旧版生命周期起";
    const delivered = turn.duration_ms == null ? "尚无确认送达计时" : `最后确认送达 ${duration(turn.duration_ms)}`;
    return `运行中已过 ${duration(turn.elapsed_ms)}（${basis}，非最终耗时） · ${delivered}`;
  }
  return turn.duration_source === "not_delivered" ? "总耗时未知（尚无确认送达）" : "总耗时未记录";
}
function status(message, error = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", error);
}
function showError(error) {
  if (error?.name !== "AbortError") status(error instanceof Error ? error.message : "操作失败", true);
}
const bridgeOrigin = new URL(document.URL).origin;
const inFrame = window.parent !== window;
const bridgeRequests = new Map();
let bridgeReady = !inFrame;
let pageActive = true;
let pageController = new AbortController();
let navigationController = new AbortController();

function abortError() { return new DOMException("读取已停止", "AbortError"); }
function apiUrl(path) {
  if (typeof path !== "string" || !path.startsWith("/")) throw new Error("无效会话请求路径");
  const url = new URL(`${apiBase}${path}`, bridgeOrigin);
  if (url.origin !== bridgeOrigin || !url.pathname.startsWith(`${apiBase}/`) || url.hash) throw new Error("无效会话请求路径");
  return url.pathname + url.search;
}
window.addEventListener("message", (event) => {
  if (!inFrame || event.source !== window.parent || event.origin !== bridgeOrigin) return;
  const message = event.data;
  if (!message || typeof message !== "object") return;
  if (message.type === "webui.ready") {
    bridgeReady = true;
    for (const pending of bridgeRequests.values()) pending.send();
    return;
  }
  const pending = bridgeRequests.get(message.id);
  if (!pending || !pending.sent) return;
  if (Object.hasOwn(message, "error")) pending.finish(new Error(String(message.error)));
  else if (Object.hasOwn(message, "result")) pending.finish(null, message.result);
});
function bridgeApi(url, options, signal) {
  if (signal.aborted || !pageActive) return Promise.reject(abortError());
  if (bridgeRequests.size >= 64) return Promise.reject(new Error("读取请求过多，请稍后重试。"));
  const id = crypto.randomUUID();
  const timeout = options.method === "GET" ? 30000 : 320000;
  return new Promise((resolve, reject) => {
    const onAbort = () => pending.finish(abortError());
    const pending = {
      sent: false,
      finish(error, result) {
        if (!bridgeRequests.delete(id)) return;
        clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        if (error) reject(error); else resolve(result);
      },
      send() {
        if (pending.sent || !bridgeReady || !bridgeRequests.has(id)) return;
        if (signal.aborted || !pageActive) return pending.finish(abortError());
        pending.sent = true;
        try {
          window.parent.postMessage({ id, method: "api", payload: {
            url, method: options.method, timeout,
            headers: { "Content-Type": "application/json", ...(options.headers || {}) },
            ...(options.body != null ? { data: JSON.parse(options.body) } : {}),
            ...(options.responseType === "blob" ? { responseType: "blob" } : {}),
          } }, bridgeOrigin);
        } catch (error) { pending.finish(error); }
      },
    };
    const timer = setTimeout(() => pending.finish(new Error(options.method === "GET"
      ? "WebUI 读取超时，请刷新后重试。"
      : "WebUI 操作等待超时；操作可能已执行，请刷新检查结果，不要重复提交。")), timeout + 10000);
    bridgeRequests.set(id, pending);
    signal.addEventListener("abort", onAbort, { once: true });
    pending.send();
  });
}
async function request(path, options = {}) {
  const url = apiUrl(path);
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "POST", "DELETE"].includes(method)) throw new Error("不支持的会话请求方法");
  const signal = AbortSignal.any([
    pageController.signal, ...(method === "GET" ? [navigationController.signal] : []),
    ...(options.signal ? [options.signal] : []),
  ]);
  if (!pageActive || signal.aborted) throw abortError();
  let data;
  if (inFrame) data = await bridgeApi(url, { ...options, method }, signal);
  else {
    const response = await fetch(url, {
      credentials: "same-origin", cache: "no-store", ...options, method, signal,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (options.responseType === "blob" && response.ok) data = await response.blob();
    else {
      data = await response.json().catch(() => ({ success: false, message: "服务器响应无法解析" }));
      if (!response.ok) throw new Error(data.message || `请求失败（${response.status}）`);
    }
  }
  if (signal.aborted || !pageActive) throw abortError();
  if (options.responseType === "blob") {
    if (!(data instanceof Blob)) throw new Error("服务器未返回图片附件");
  } else if (!data || typeof data !== "object" || data.success === false) {
    throw new Error(data?.message || "服务器响应无法解析");
  }
  return data;
}
function beginNavigation() {
  ++state.navigation;
  navigationController.abort();
  navigationController = new AbortController();
  disposePayload();
  if ($("payload-dialog").open) $("payload-dialog").close();
  disposeAttachments();
  return state.navigation;
}
function badge(value) {
  const item = node("span", "badge", label(value));
  if (["failed", "cancelled", "rejected"].includes(value)) item.classList.add("badge-danger");
  if (["pending", "running", "partial", "requested"].includes(value)) item.classList.add("badge-pending");
  if (["confirmed", "succeeded", "completed"].includes(value)) item.classList.add("badge-success");
  return item;
}
function section(title, note = "") {
  const item = node("section", "section");
  item.dataset.recordKey = title;
  item.append(node("h3", "", title));
  if (note) item.append(node("p", "muted", note));
  return item;
}
function disclosure(title, note = "", className = "") {
  const item = node("details", `section disclosure ${className}`.trim());
  item.dataset.detailKey = title;
  const summary = node("summary", "disclosure-summary");
  const heading = node("span", "disclosure-heading");
  heading.append(node("strong", "disclosure-title", title));
  if (note) heading.append(node("span", "disclosure-note", note));
  summary.append(heading);
  const content = node("div", "disclosure-content");
  item.append(summary, content);
  return { item, summary, content };
}
function rawDetails(title, value) {
  const item = node("details", "raw-details");
  item.append(node("summary", "", title), node("pre", "code", text(value)));
  return item;
}
function empty(target, message) { target.replaceChildren(node("p", "empty", message)); }
function usageText(usage) {
  const coverage = usage?.coverage?.complete === false ? "（部分统计）" : "";
  return `输入 ${number(usage?.input_tokens)} / 输出 ${number(usage?.output_tokens)} / 合计 ${number(usage?.total_tokens)} token${coverage}`;
}
function usageDetails(usage) {
  const item = node("details", "usage-details");
  item.dataset.recordKey = "usage";
  item.append(node("summary", "", usageText(usage)));
  item.append(node("p", "muted", `缓存输入 ${number(usage?.cached_input_tokens)} · 推理 ${number(usage?.reasoning_tokens)}（均为子集，不额外相加）`));
  const source = usage?.source === "model_response" ? "逐请求实测" : label(usage?.source || "not_recorded");
  item.append(node("p", "muted", `已测请求 ${number(usage?.measured_requests)} · 未知请求 ${number(usage?.unknown_requests)} · 来源：${source}`));
  if (usage?.coverage !== undefined) item.append(rawDetails("统计覆盖范围", usage.coverage));
  return item;
}
function personaName(persona) { return persona?.name || "人格未记录"; }
function eventByRef(ref) { return state.events.find((event) => event.event_ref === ref); }
function eventTitle(event) { return event?.title || event?.tool || label(event?.event_type); }
function eventPreview(event) { return typeof event?.preview === "string" ? event.preview : event?.preview?.text; }
function eventButton(ref, title, path = "") {
  const item = button(title, () => openPayload(ref, title, path));
  item.dataset.focusKey = `${ref}|${path}|${title}`;
  item.disabled = !ref;
  if (!ref) item.title = "本轮未记录对应负载";
  return item;
}

const relationshipAxes = {
  affection: "\u597d\u611f", trust: "\u4fe1\u4efb", dependence: "\u4f9d\u8d56", resentment: "\u6028\u5ff5", familiarity: "\u719f\u6089\u5ea6",
};
const actualOutcomeLabels = {
  silent: "\u81ea\u4e3b\u6c89\u9ed8", refusal: "\u5df2\u9001\u8fbe\u62d2\u7edd", media_only: "\u4ec5\u5a92\u4f53", text: "\u6587\u5b57\u56de\u590d", mixed: "\u6587\u5b57\u4e0e\u5a92\u4f53", unknown: "\u56de\u5e94\u9001\u8fbe\u672a\u8bb0\u5f55",
};
function affectNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "\u672a\u8bb0\u5f55";
}
function axisDelta(value) {
  return typeof value === "number" && Number.isFinite(value) ? `${value > 0 ? "+" : ""}${affectNumber(value)}` : "\u672a\u8bb0\u5f55";
}
function evaluationStatus(value) {
  return ({ pending: "\u8bc4\u4f30\u6392\u961f\u4e2d", running: "\u8bc4\u4f30\u8fd0\u884c\u4e2d", succeeded: "\u8bc4\u4f30\u5df2\u5b8c\u6210", failed: "\u8bc4\u4f30\u5931\u8d25" })[value] || label(value);
}
function outcomeLabel(decision, turnStatus) {
  if (decision?.actual_outcome === "silent" && turnStatus !== "silent") return actualOutcomeLabels.unknown;
  if (decision) return actualOutcomeLabels[decision.actual_outcome] || actualOutcomeLabels.unknown;
  return "\u81ea\u4e3b\u56de\u5e94\u672a\u8bb0\u5f55";
}
function affectIndicators(view, turnStatus) {
  const row = node("div", "affect-indicators");
  if (view.response_decision) row.append(node("span", "badge", outcomeLabel(view.response_decision, turnStatus)));
  else if (view.engagement) row.append(node("span", "badge", `\u5386\u53f2\u610f\u5411\uff1a${view.engagement.level_label || "\u672a\u8bb0\u5f55"}`));
  const evaluation = view.relationship_evaluation;
  if (evaluation) {
    const indicator = badge(evaluation.status);
    indicator.textContent = evaluationStatus(evaluation.status);
    row.append(indicator);
    const count = evaluation.evidence_count ?? evaluation.evidence_turn_ids?.length;
    if (count > 1) row.append(node("span", "badge", `${count} \u8f6e\u5408\u5e76\u6279\u6b21`));
  }
  if (view.relationship?.emotions?.length) {
    for (const emotion of view.relationship.emotions.slice(0, 4)) row.append(node("span", "badge emotion-badge", `${emotion.name || "\u60c5\u7eea\u672a\u8bb0\u5f55"} ${affectNumber(emotion.intensity)}`));
  } else if (view.relationship?.emotions) row.append(node("span", "badge", "\u65e0\u6d3b\u8dc3\u60c5\u7eea"));
  return row;
}
async function selectEvidenceTurn(evidence) {
  if (!evidence?.turn_ref || !evidence.session_ref) return;
  if (state.session?.session_ref !== evidence.session_ref) {
    const generation = state.navigation;
    const result = await request(`/sessions/${encodeURIComponent(evidence.session_ref)}`);
    if (generation !== state.navigation) return;
    if (!state.sessions.some((session) => session.session_ref === evidence.session_ref)) state.sessions.push(result.item);
    await selectSession(result.item, evidence.turn_ref);
  } else await selectTurn(state.turns.find((turn) => turn.turn_ref === evidence.turn_ref) || evidence);
}
function evidenceLinks(ids, recordKey) {
  const row = node("div", "actions evidence-links");
  for (const id of ids || []) {
    const evidence = state.inspection.evidence_turns?.find((turn) => turn.turn_id === id);
    const link = button(evidence ? `\u67e5\u770b\u8bc1\u636e #${id}` : `\u8bc1\u636e #${id}\uff08\u4e0d\u53ef\u7528\uff09`, () => selectEvidenceTurn(evidence));
    link.dataset.focusKey = `${recordKey}:evidence:${id}`;
    link.disabled = !evidence;
    row.append(link);
  }
  if (!ids?.length) row.append(node("p", "muted", "\u672a\u8bb0\u5f55\u8bc1\u636e\u5f15\u7528"));
  return row;
}
function emotionSnapshot(snapshot, title, key) {
  const panel = node("div", "emotion-snapshot");
  panel.append(node("h4", "", title));
  const emotions = snapshot?.emotions;
  if (!Array.isArray(emotions)) panel.append(node("p", "muted", "\u60c5\u7eea\u672a\u8bb0\u5f55"));
  else if (!emotions.length) panel.append(node("p", "muted", "\u5df2\u8bb0\u5f55\uff1a\u65e0\u6d3b\u8dc3\u60c5\u7eea"));
  for (const [index, emotion] of (emotions || []).entries()) {
    const item = node("div", "emotion-entry");
    item.append(node("strong", "", `${emotion.name || "\u672a\u547d\u540d"} \u00b7 ${affectNumber(emotion.intensity)}`));
    item.append(node("p", "", emotion.cause ?? "\u539f\u56e0\u672a\u8bb0\u5f55"));
    const evidence = disclosure("\u60c5\u7eea\u8bc1\u636e\u4e0e\u65f6\u95f4", "", "emotion-evidence");
    evidence.item.dataset.detailKey = `${key}:emotion:${index}`;
    evidence.content.append(evidenceLinks(emotion.evidence_turn_ids, `${key}:emotion:${index}`));
    const updated = typeof emotion.updated_at === "number" ? new Date(emotion.updated_at * 1000) : null;
    evidence.content.append(node("p", "muted", `\u66f4\u65b0\u65f6\u95f4\uff1a${updated && Number.isFinite(updated.getTime()) ? updated.toLocaleString() : "\u672a\u8bb0\u5f55"}`));
    item.append(evidence.item);
    panel.append(item);
  }
  return panel;
}
function snapshotDescription(snapshot, title) {
  const panel = node("div", "snapshot-description");
  panel.append(node("h4", "", title));
  panel.append(node("p", "", snapshot?.description ?? "\u5173\u7cfb\u63cf\u8ff0\u672a\u8bb0\u5f55"));
  panel.append(node("p", "", `\u5370\u8c61\uff1a${snapshot?.impression ?? "\u672a\u8bb0\u5f55"}`));
  panel.append(node("p", "muted", `\u7248\u672c ${affectNumber(snapshot?.version)} \u00b7 ${date(snapshot?.updated_at)}`));
  return panel;
}
function relationshipPanel() {
  const view = state.inspection;
  const panel = section("\u672c\u8f6e\u5173\u7cfb\u3001\u60c5\u7eea\u4e0e\u81ea\u4e3b\u56de\u5e94", "\u5c55\u793a\u672c\u8f6e\u5b9e\u9645\u4f7f\u7528\u7684\u5feb\u7167\uff0c\u4e0d\u4ece\u5f53\u524d\u914d\u7f6e\u5012\u63a8\u5386\u53f2\u3002");
  panel.classList.add("relationship-panel");
  panel.dataset.recordKey = "relationship";
  panel.append(affectIndicators(view, view.turn.status));
  const decision = view.response_decision;
  const outcome = node("div", "response-outcome");
  outcome.append(node("strong", "", outcomeLabel(decision, view.turn.status)));
  if (decision) {
    const delivery = decision.actual_delivery || {};
    outcome.append(node("p", "", `\u786e\u8ba4\u9001\u8fbe ${affectNumber(delivery.confirmed_deliveries)} \u00b7 \u6587\u5b57 ${affectNumber(delivery.text_messages)} \u00b7 \u5a92\u4f53 ${affectNumber(delivery.media_messages)}`));
    outcome.append(node("p", "muted", `${decision.source === "model" ? "\u6a21\u578b\u9009\u62e9" : decision.source === "runtime" ? "\u8fd0\u884c\u65f6\u8bb0\u5f55" : "\u6765\u6e90\u672a\u8bb0\u5f55"}\uff1a${decision.reason || "\u539f\u56e0\u672a\u8bb0\u5f55"}`));
    outcome.append(eventButton(decision.event_ref, "\u5b8c\u6574\u56de\u5e94\u51b3\u7b56"));
  }
  if (["failed", "partial", "cancelled", "superseded"].includes(view.turn.status)) outcome.append(node("p", "error", "\u5931\u8d25\u3001\u90e8\u5206\u9001\u8fbe\u6216\u88ab\u53d6\u6d88\u4e0d\u7b49\u4e8e\u81ea\u4e3b\u6c89\u9ed8\u3002"));
  panel.append(outcome);
  const snapshot = view.relationship;
  if (snapshot) {
    panel.append(node("h4", "", "\u751f\u6210\u524d\u5b9e\u9645\u4f7f\u7528\u7684\u5173\u7cfb"));
    const axes = node("div", "relationship-axes");
    for (const [key, title] of Object.entries(relationshipAxes)) {
      const axis = node("div", "relationship-axis");
      axis.append(node("span", "muted", title), node("strong", "", affectNumber(snapshot.axes?.[key])));
      axes.append(axis);
    }
    panel.append(axes, snapshotDescription(snapshot, "\u5bf9\u8be5\u6210\u5458\u7684\u5173\u7cfb\u4e0e\u5370\u8c61"), emotionSnapshot(snapshot, "\u751f\u6210\u65f6\u7684\u611f\u53d7\u4e0e\u539f\u56e0", "generation"));
  } else {
    panel.append(node("p", "muted", "\u672c\u8f6e\u672a\u8bb0\u5f55\u65b0\u7248\u5173\u7cfb\u4e0e\u60c5\u7eea\u5feb\u7167\uff1b\u4e0d\u8865\u5199\u6570\u503c\u3002"));
    const legacy = state.events.find((event) => event.event_type === "persona_state");
    if (legacy?.persona?.relation?.length) {
      const old = disclosure("\u5386\u53f2\u5173\u7cfb\u8bb0\u5f55");
      old.item.dataset.detailKey = "legacy-relation";
      for (const row of legacy.persona.relation) old.content.append(node("p", "", `${row.label}\uff1a${row.value}`));
      old.content.append(eventButton(legacy.event_ref, "\u5b8c\u6574\u5386\u53f2\u5feb\u7167"));
      panel.append(old.item);
    }
  }
  const evaluation = view.relationship_evaluation;
  if (!evaluation) panel.append(node("p", "muted", "\u672c\u8f6e\u5c1a\u65e0\u5173\u7cfb\u8bc4\u4f30\u8bb0\u5f55\uff1b\u4e0d\u4ee3\u8868\u5df2\u8bc4\u4f30\u6216\u53d8\u5316\u4e3a\u96f6\u3002"));
  else {
    const result = section(evaluationStatus(evaluation.status));
    result.dataset.recordKey = `evaluation:${evaluation.event_ref}`;
    result.classList.add("evaluation-result");
    const count = evaluation.evidence_turn_ids?.length || 0;
    result.append(node("p", "batch-notice", count > 1 ? `\u6b64\u7ed3\u679c\u5c5e\u4e8e ${count} \u8f6e\u5408\u5e76\u8bc4\u4f30\uff0c\u4e0d\u662f\u672c\u6761\u6d88\u606f\u7684\u5355\u72ec\u8d21\u732e\u3002` : "\u6b64\u7ed3\u679c\u5bf9\u5e94\u8bc4\u4f30\u6279\u6b21\uff0c\u751f\u6210\u524d\u5feb\u7167\u4e0e\u8bc4\u4f30\u524d\u72b6\u6001\u53ef\u80fd\u4e0d\u540c\u3002"));
    if (evaluation.error) result.append(node("p", "error", evaluation.error));
    if (evaluation.status !== "succeeded") result.append(node("p", "muted", "\u5c1a\u65e0\u6210\u529f\u5199\u5165\u7684\u8bc4\u4f30\u540e\u72b6\u6001\uff1b\u4e0d\u628a\u672a\u77e5\u53d8\u5316\u5f53\u4f5c\u96f6\u3002"));
    const table = node("table", "relationship-changes");
    const head = node("thead"), headers = node("tr");
    for (const title of ["\u5173\u7cfb\u8f74", "\u8bc4\u4f30\u524d", "\u2192 \u8bc4\u4f30\u540e", "\u53d8\u5316"]) { const cell = node("th", "", title); cell.scope = "col"; headers.append(cell); }
    head.append(headers); table.append(head);
    const body = node("tbody");
    for (const [key, title] of Object.entries(relationshipAxes)) {
      const row = node("tr"), change = evaluation.changes?.[key];
      const name = node("th", "", title); name.scope = "row";
      row.append(name, node("td", "", affectNumber(evaluation.before?.axes?.[key])), node("td", "", affectNumber(evaluation.after?.axes?.[key])), node("td", "axis-delta", axisDelta(change?.delta)));
      body.append(row);
    }
    table.append(body); result.append(table);
    const feelings = node("div", "affect-comparison");
    feelings.append(emotionSnapshot(evaluation.before, "\u8bc4\u4f30\u524d\u611f\u53d7", "before"), emotionSnapshot(evaluation.after, "\u8bc4\u4f30\u540e\u611f\u53d7", "after"));
    result.append(feelings);
    const evidence = disclosure("\u6279\u6b21\u8bc1\u636e\u4e0e\u8bc4\u4f30\u8be6\u60c5", `${count} \u8f6e\u8bc1\u636e`);
    evidence.item.dataset.detailKey = "evaluation-evidence";
    evidence.content.append(node("p", "muted", `\u8bc4\u4f30\u6807\u8bc6\uff1a${evaluation.evaluation_ref || "\u672a\u8bb0\u5f55"}`), node("p", "muted", `\u6a21\u578b\uff1a${evaluation.model || "\u672a\u8bb0\u5f55"}`));
    evidence.content.append(node("p", "muted", `\u6392\u961f ${date(evaluation.queued_at)} \u00b7 \u5f00\u59cb ${date(evaluation.started_at)} \u00b7 \u7ed3\u675f ${date(evaluation.finished_at)}`));
    evidence.content.append(evidenceLinks(evaluation.evidence_turn_ids, evaluation.event_ref));
    evidence.content.append(snapshotDescription(evaluation.before, "\u8bc4\u4f30\u524d\u5173\u7cfb\u4e0e\u5370\u8c61"), snapshotDescription(evaluation.after, "\u8bc4\u4f30\u540e\u5173\u7cfb\u4e0e\u5370\u8c61"));
    evidence.content.append(eventButton(evaluation.event_ref, "\u5b8c\u6574\u8bc4\u4f30\u8d1f\u8f7d"));
    result.append(evidence.item); panel.append(result);
  }
  if (view.engagement) {
    const history = disclosure("\u5386\u53f2\u56de\u5e94\u610f\u5411\uff08\u65e7\u7b56\u7565\uff09", view.engagement.level_label || "");
    history.item.dataset.detailKey = "legacy-engagement";
    history.content.append(node("p", "", view.engagement.tone || ""));
    for (const reason of view.engagement.reasons || []) history.content.append(node("p", "", reason));
    const event = state.events.find((item) => item.event_type === "engagement_decision");
    history.content.append(eventButton(event?.event_ref, "\u5b8c\u6574\u5386\u53f2\u610f\u5411"));
    panel.append(history.item);
  }
  return panel;
}

async function loadScopes() {
  const generation = ++state.scopeLoad;
  status("正在同步…");
  const result = await request("/scopes?limit=500");
  if (generation !== state.scopeLoad) return;
  const oldScope = state.scope?.scope_ref;
  state.scopes = result.items || [];
  $("scope-select").replaceChildren();
  for (const scope of state.scopes) {
    const option = node("option", "", scope.channel_name || scope.display_name || scope.channel_id || "未命名频道");
    option.value = scope.scope_ref;
    $("scope-select").append(option);
  }
  state.scope = state.scopes.find((scope) => scope.scope_ref === oldScope) || state.scopes[0] || null;
  $("scope-select").value = state.scope?.scope_ref || "";
  renderScope();
  await loadSessions(state.session?.session_ref);
  if (generation === state.scopeLoad) status(state.scopes.length ? "已同步" : "暂无聊天范围");
}
function renderScope() {
  const scope = state.scope;
  $("scope-meta").replaceChildren();
  if (scope) {
    $("scope-meta").append(node("p", "", `${scope.platform || ""}${scope.persona ? ` · 当前 ${personaName(scope.persona)}` : ""}`));
    $("scope-meta").append(rawDetails("范围标识", { channel_id: scope.channel_id, scope_ref: scope.scope_ref }));
  }
  const active = Boolean(scope) && state.session?.status === "active";
  $("new-session-button").disabled = !active || state.actionBusy;
  $("rollover-button").disabled = !active || state.actionBusy;
  $("hard-reset-button").disabled = !scope || state.actionBusy;
}
async function loadSessions(preferredRef = "") {
  stopPolling();
  const generation = ++state.sessionLoad;
  const scope = state.scope;
  const result = scope ? await request(`/scopes/${encodeURIComponent(scope.scope_ref)}/sessions?limit=500`) : { items: [] };
  if (generation !== state.sessionLoad || scope !== state.scope) return;
  state.sessions = result.items || [];
  const selected = state.sessions.find((item) => item.session_ref === preferredRef)
    || state.sessions.find((item) => item.status === "active") || state.sessions[0] || null;
  await selectSession(selected);
}
function renderSessions() {
  $("session-count").textContent = String(state.sessions.length);
  $("session-list").replaceChildren();
  if (!state.sessions.length) empty($("session-list"), "暂无会话");
  for (const session of state.sessions) {
    const item = button("", () => selectSession(session), "list-item");
    item.dataset.focusKey = session.session_ref;
    item.classList.toggle("is-active", session.session_ref === state.session?.session_ref);
    item.setAttribute("aria-current", String(session.session_ref === state.session?.session_ref));
    const heading = node("div", "item-heading");
    heading.append(node("strong", "", `会话 ${session.sequence}`), badge(session.status));
    item.append(heading, node("p", "snippet", `${personaName(session.persona)} · ${session.model || "模型未记录"}`));
    item.append(node("p", "muted", `${session.turn_count} 轮 · ${label(session.start_reason)}`), node("p", "muted", date(session.created_at)));
    $("session-list").append(item);
  }
}
async function selectSession(session, preferredTurnRef = "") {
  stopPolling();
  const generation = beginNavigation();
  state.session = session;
  state.detail = null;
  state.turn = null;
  state.turns = [];
  state.inspection = null;
  state.events = [];
  renderSessions(); renderScope(); renderSession(); renderTurns(); renderWorkspace();
  if (!session) return;
  const ref = encodeURIComponent(session.session_ref);
  const [detail, turns] = await Promise.all([request(`/sessions/${ref}`), request(`/sessions/${ref}/turns?limit=1000`)]);
  if (generation !== state.navigation) return;
  state.detail = detail.item;
  state.turns = turns.items || [];
  renderSession(); renderTurns();
  if (preferredTurnRef) await selectTurn(state.turns.find((turn) => turn.turn_ref === preferredTurnRef) || { turn_ref: preferredTurnRef });
  else if (state.turns.length) await selectTurn(state.turns[state.turns.length - 1]);
}
function renderSession() {
  const container = $("session-detail");
  container.replaceChildren();
  if (!state.detail) return empty(container, state.session ? "正在读取会话…" : "请选择一个会话。");
  const detail = state.detail;
  container.append(node("p", "", `${personaName(detail.persona)} · ${detail.model || "模型未记录"}`), usageDetails(detail.usage));
  const more = node("details");
  more.append(node("summary", "", "会话上下文与固定事件"));
  more.append(rawDetails("最近记录的上下文预算", detail.context ?? null));
  if (detail.handoff && Object.keys(detail.handoff).length) more.append(rawDetails("会话交接", detail.handoff));
  if (detail.anchors?.length) {
    for (const anchor of detail.anchors) {
      const row = node("div", "anchor-row");
      row.append(eventButton(anchor.event_ref, anchor.label || "固定事件"), button("取消固定", () => confirmUnpin(anchor)));
      more.append(row);
    }
  } else more.append(node("p", "muted", "无固定事件"));
  more.append(rawDetails("高级会话标识", detail));
  container.append(more);
}
function renderTurns() {
  $("turn-count").textContent = String(state.turns.length);
  $("turn-list").replaceChildren();
  if (!state.turns.length) empty($("turn-list"), "暂无轮次");
  for (const turn of state.turns) {
    const item = button("", () => selectTurn(turn), "list-item");
    item.dataset.focusKey = turn.turn_ref;
    item.classList.toggle("is-active", turn.turn_ref === state.turn?.turn_ref);
    item.setAttribute("aria-current", String(turn.turn_ref === state.turn?.turn_ref));
    const heading = node("div", "item-heading");
    heading.append(node("strong", "", turn.user_name || "用户"), badge(turn.status));
    item.append(heading, node("p", "snippet", turn.input_preview == null ? "用户输入摘要未记录" : short(turn.input_preview, 180)));
    item.append(node("p", "muted", date(turn.created_at)));
    item.append(node("p", "muted", `${turn.model || "模型未记录"} · 请求 ${number(turn.model_call_count)} · 工具 ${number(turn.tool_call_count)}`));
    item.append(node("p", "muted turn-duration", turnDuration(turn)));
    item.append(affectIndicators(turn, turn.status));
    if (turn.relationship?.axes) item.append(node("p", "muted", Object.entries(relationshipAxes).map(([key, title]) => `${title} ${affectNumber(turn.relationship.axes[key])}`).join(" \u00b7 ")));
    const changes = Object.entries(turn.relationship_evaluation?.changes || {}).filter(([, change]) => typeof change.delta === "number" && change.delta !== 0);
    if (changes.length) item.append(node("p", "muted", `\u6279\u6b21\u53d8\u5316\uff1a${changes.map(([key, change]) => `${relationshipAxes[key] || key} ${axisDelta(change.delta)}`).join(" \u00b7 ")}`));
    $("turn-list").append(item);
  }
}
async function fetchTurn(turnRef) {
  const ref = encodeURIComponent(turnRef);
  const [inspection, events] = await Promise.all([request(`/turns/${ref}/inspection`), request(`/turns/${ref}/events`)]);
  return { inspection: inspection.item, events: events.items || [] };
}
async function selectTurn(turn) {
  stopPolling();
  const generation = beginNavigation();
  state.turn = turn;
  state.inspection = null;
  state.events = [];
  renderTurns(); renderWorkspace();
  const result = await fetchTurn(turn.turn_ref);
  if (generation !== state.navigation) return;
  state.inspection = result.inspection;
  state.events = result.events;
  state.turn = result.inspection.turn;
  renderTurns(); renderWorkspace();
  startPolling();
}
function renderWorkspace() {
  const header = $("turn-header");
  header.replaceChildren();
  $("collapse-details-button").disabled = !state.inspection;
  if (!state.inspection) {
    header.append(node("p", "", state.turn ? "正在读取轮次…" : "请选择一个轮次。"));
    for (const id of ["timeline-view", "context-view", "io-view"]) empty($(id), state.turn ? "正在读取…" : "请选择一个轮次。");
    return;
  }
  const inspection = state.inspection;
  const heading = node("div", "item-heading");
  heading.append(node("h2", "", `${inspection.turn.user_name || "用户"} · ${date(inspection.turn.created_at)}`), badge(inspection.turn.status));
  const models = [...new Set((inspection.model_calls || []).map((call) => call.model).filter(Boolean))];
  header.append(heading, node("p", "muted", `${personaName(inspection.persona)} · ${models.length ? models.join(" / ") : inspection.turn.model || "模型未记录"}`), usageDetails(inspection.usage));
  header.append(node("p", "turn-duration", turnDuration(inspection.turn)));
  header.append(affectIndicators(inspection, inspection.turn.status));
  renderTimeline(); renderContext(); renderIO();
}
function callPanel(title, preview, ref, path = "") {
  const panel = node("div", "call-panel");
  const heading = node("div", "call-panel-heading");
  heading.append(node("h4", "", title), eventButton(ref, "查看完整内容", path));
  const value = !ref ? "本轮未记录" : preview == null ? "摘要未记录" : text(preview);
  panel.append(heading, node("pre", `preview ${typeof preview === "object" && preview !== null ? "code" : "prose"}`, value));
  return panel;
}
function modelCard(call, index) {
  const { item, summary, content } = disclosure(`模型请求 ${index + 1}`, call.model || "模型未记录", "record-card model-record");
  item.dataset.recordKey = `model:${call.request_id || call.request_event_ref || call.response_event_ref || index}`;
  item.dataset.detailKey = "record";
  const kind = node("span", "record-kind", "M");
  kind.setAttribute("aria-hidden", "true");
  summary.prepend(kind);
  const meta = node("span", "record-meta");
  meta.append(badge(call.status), node("span", "record-duration", duration(call.duration_ms)));
  summary.append(meta);
  content.append(node("p", "muted", `${usageText(call.usage)} · 尝试 ${call.attempt ?? "未知"} · ${label(call.capture_status)}`));
  const panels = node("div", "call-panels");
  panels.append(callPanel("模型输入", call.input_preview, call.request_event_ref), callPanel("模型输出", call.output_preview, call.response_event_ref));
  content.append(panels, rawDetails("请求标识与统计", { request_id: call.request_id, request_event_ref: call.request_event_ref, response_event_ref: call.response_event_ref, usage: call.usage }));
  return item;
}
function toolCard(call, index) {
  const deliveries = call.deliveries || [];
  const imageCount = deliveries.reduce((count, delivery) => count + (delivery.images?.length || 0), 0);
  const deliveryNote = deliveries.length ? `已确认送达 ${deliveries.length} 条消息${imageCount ? ` · ${imageCount} 张图片` : ""}` : "工具调用";
  const { item, summary, content } = disclosure(call.tool_name || "工具名未记录", deliveryNote, "record-card tool-record");
  item.dataset.recordKey = `tool:${call.execution_ref || call.call_event_ref || call.result_event_ref || index}`;
  item.dataset.detailKey = "record";
  const kind = node("span", "record-kind", "T");
  kind.setAttribute("aria-hidden", "true");
  summary.prepend(kind);
  const meta = node("span", "record-meta");
  meta.append(badge(call.status), node("span", "record-duration", duration(call.duration_ms)));
  summary.append(meta);
  content.append(node("p", "muted", `工具效果记录：${label(call.effect)}（与实际送达凭据分开）`));
  for (const delivery of deliveries) content.append(deliveryCard(delivery, `tool-delivery:${delivery.event_ref}`));
  if (call.delivery_source === "legacy_incomplete") content.append(node("p", "muted", "旧记录没有独立送达凭据；未留存的历史输出图片无法还原，不重新渲染历史 Markdown。"));
  const panels = node("div", "call-panels");
  const argumentsPanel = callPanel("调用参数", call.arguments_preview, call.call_event_ref, call.arguments_path || "");
  const resultPanel = callPanel("返回结果", call.result_preview, call.result_event_ref, call.result_path || "");
  argumentsPanel.append(node("p", "muted", `参数捕获：${label(call.arguments_capture_status || "not_recorded")}`));
  resultPanel.append(node("p", "muted", `结果捕获：${label(call.result_capture_status || "not_recorded")}`));
  panels.append(argumentsPanel, resultPanel);
  const records = node("div", "actions");
  records.append(eventButton(call.call_event_ref, "调用原始记录 / 脱敏说明"), eventButton(call.result_event_ref, "结果原始记录 / 脱敏说明"));
  content.append(panels, records);
  if (call.evidence != null) content.append(rawDetails("执行证据摘要", call.evidence));
  if (call.images?.length) content.append(node("p", "muted", "工具附件（可能含输入 / 参考图，不等同于已送达图片）"));
  appendImages(content, { event_ref: call.result_event_ref, images: call.images });
  content.append(rawDetails("执行标识", { execution_ref: call.execution_ref, call_event_ref: call.call_event_ref, result_event_ref: call.result_event_ref }));
  return item;
}
function renderTimeline() {
  const target = $("timeline-view");
  target.replaceChildren();
  const inspection = state.inspection;
  target.append(relationshipPanel());
  const calls = [
    ...(inspection.model_calls || []).map((call, index) => ({ ref: call.request_event_ref || call.response_event_ref, card: () => modelCard(call, index) })),
    ...(inspection.tool_calls || []).map((call, index) => ({ ref: call.call_event_ref || call.result_event_ref, card: () => toolCard(call, index) })),
  ];
  calls.sort((a, b) => (eventByRef(a.ref)?.sequence ?? Infinity) - (eventByRef(b.ref)?.sequence ?? Infinity));
  const overview = node("div", "timeline-overview");
  overview.append(node("h3", "", "执行记录"), node("p", "muted", `${inspection.model_calls?.length || 0} 次模型请求 · ${inspection.tool_calls?.length || 0} 次工具调用 · 点击记录展开详情`));
  target.append(overview);
  if (!calls.length) target.append(node("p", "empty", "\u672c\u8f6e\u672a\u8bb0\u5f55\u6a21\u578b\u8bf7\u6c42\u6216\u5de5\u5177\u8c03\u7528\uff1b\u4e0d\u4ee5\u751f\u6210\u5c1d\u8bd5\u63a8\u65ad\u5b9e\u9645\u8bf7\u6c42\u3002"));
  for (const call of calls) target.append(call.card());
  const audit = node("details", "audit-list");
  audit.append(node("summary", "", `原始审计事件（${state.events.length}）`));
  for (const event of state.events) {
    const row = node("div", "audit-row");
    row.append(eventButton(event.event_ref, eventTitle(event)), badge(event.status || event.effect), node("span", "muted", date(event.created_at)));
    audit.append(row);
  }
  target.append(audit);
}
function renderContext() {
  const target = $("context-view");
  target.replaceChildren();
  const context = state.inspection.context;
  if (!context?.captured) return empty(target, "本轮未记录上下文注入快照。旧审计记录无法还原实际请求，不使用当前配置代替。");
  const ref = context.event_ref;
  const overview = section("本轮注入快照", "保留本轮开始时的原始记录，不代表当前配置。修改人格后，请查看新轮次的快照。");
  const links = node("div", "actions snapshot-links");
  for (const [path, title] of [["persona", "本轮人格快照"], ["system", "完整系统指令"], ["messages", "实际选中消息"], ["", "完整快照"]]) links.append(eventButton(ref, title, path));
  overview.append(node("p", "", `${personaName(state.inspection.persona)} · ${label(context.capture_status || "not_recorded")}`), links);
  target.append(overview);
  const budgets = disclosure("上下文预算", "输入上限、预留额度与实际估算");
  const values = context.budgets;
  if (values && Object.keys(values).length) {
    const list = node("dl", "fields");
    for (const [key, value] of Object.entries(values)) list.append(node("dt", "", label(key)), node("dd", "", text(value)));
    budgets.content.append(list);
  } else budgets.content.append(node("p", "muted", "本轮未记录"));
  budgets.content.append(eventButton(ref, "预算原始记录", "budgets"));
  target.append(budgets.item);
  const selection = disclosure("历史选择", "选中与排除的轮次证据，不以当前历史重算");
  selection.content.append(rawDetails("选择证据", context.selection ?? null), eventButton(ref, "完整选择记录", "selection"));
  target.append(selection.item);
  const blockCount = Array.isArray(context.blocks) ? `${context.blocks.length} 个命名块` : "命名块未记录";
  const blocks = disclosure("注入内容", blockCount);
  if (!Array.isArray(context.blocks)) blocks.content.append(node("p", "muted", "本轮未记录命名块"));
  else if (!context.blocks.length) blocks.content.append(node("p", "muted", "已记录：无命名注入块"));
  else for (const [index, block] of context.blocks.entries()) {
    const row = node("div", "block-row");
    row.append(node("strong", "", label(block.name)), node("span", "muted", `${number(block.chars)} 字符`), eventButton(ref, "查看注入内容", `${block.path || `blocks.${index}`}.content`));
    if (block.preview != null) row.append(node("p", "snippet", short(block.preview)));
    blocks.content.append(row);
  }
  blocks.content.append(eventButton(ref, "全部注入块", "blocks"));
  target.append(blocks.item);
}
const attachmentCache = new Map();
const attachmentQueue = new Set();
const attachmentConsumers = new WeakMap();
const imageTypes = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
const maxImageBytes = 6 * 1024 * 1024;
let attachmentLoads = 0;
const attachmentVisibility = new IntersectionObserver((entries) => {
  for (const { target, isIntersecting } of entries) {
    if (!isIntersecting) continue;
    attachmentVisibility.unobserve(target);
    const entry = attachmentConsumers.get(target);
    if (entry && attachmentCache.get(entry.path) === entry && !entry.url && !entry.error && !entry.loading) attachmentQueue.add(entry);
  }
  pumpAttachments();
}, { rootMargin: "200px" });
const attachmentRemoval = new MutationObserver(() => {
  for (const entry of attachmentCache.values()) {
    for (const thumb of entry.consumers.keys()) {
      if (!thumb.isConnected) {
        attachmentVisibility.unobserve(thumb);
        entry.consumers.delete(thumb);
      }
    }
    if (!entry.consumers.size) releaseAttachment(entry);
  }
});
attachmentRemoval.observe(document.body, { childList: true, subtree: true });

function releaseAttachment(entry) {
  attachmentCache.delete(entry.path);
  attachmentQueue.delete(entry);
  entry.controller.abort();
  for (const thumb of entry.consumers.keys()) attachmentVisibility.unobserve(thumb);
  entry.consumers.clear();
  if (entry.url) {
    if ($("image-preview").getAttribute("src") === entry.url) {
      $("image-preview").removeAttribute("src");
      if ($("image-dialog").open) $("image-dialog").close();
    }
    URL.revokeObjectURL(entry.url);
    entry.url = null;
  }
}
function disposeAttachments() {
  for (const entry of attachmentCache.values()) releaseAttachment(entry);
}
function renderAttachment(entry, thumb, img) {
  thumb.disabled = !entry.url || entry.error;
  if (entry.error) img.replaceWith(node("span", "muted", "图片不可用"));
  else if (entry.url) img.src = entry.url;
}
async function loadAttachment(entry) {
  try {
    const blob = await request(entry.path, { responseType: "blob", signal: entry.controller.signal });
    if (!blob.size || blob.size > maxImageBytes || !imageTypes.has(blob.type)) throw new Error("无效图片附件");
    const bytes = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
    const prefix = String.fromCharCode(...bytes);
    const mime = prefix.startsWith("\x89PNG\r\n\x1a\n") ? "image/png"
      : bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? "image/jpeg"
      : prefix.startsWith("GIF87a") || prefix.startsWith("GIF89a") ? "image/gif"
      : prefix.startsWith("RIFF") && prefix.slice(8, 12) === "WEBP" ? "image/webp" : "";
    if (mime !== blob.type) throw new Error("图片附件类型不匹配");
    if (!pageActive || entry.controller.signal.aborted || attachmentCache.get(entry.path) !== entry) return;
    entry.url = URL.createObjectURL(blob);
  } catch (error) {
    if (attachmentCache.get(entry.path) !== entry || error?.name === "AbortError") return;
    entry.error = true;
  }
  for (const [thumb, img] of entry.consumers) if (thumb.isConnected) renderAttachment(entry, thumb, img);
}
function pumpAttachments() {
  while (pageActive && attachmentLoads < 4 && attachmentQueue.size) {
    const entry = attachmentQueue.values().next().value;
    attachmentQueue.delete(entry);
    if (attachmentCache.get(entry.path) !== entry || entry.loading || entry.url || entry.error) continue;
    entry.loading = true;
    ++attachmentLoads;
    loadAttachment(entry).finally(() => {
      entry.loading = false;
      --attachmentLoads;
      pumpAttachments();
    });
  }
}
function appendImages(target, event) {
  if (!event?.images?.length || typeof event.event_ref !== "string") return;
  const grid = node("div", "images");
  for (const image of event.images) {
    if (image.status === "unrecorded" || image.status === "unavailable") {
      const label = image.name || image.label || "Image";
      const status = image.status === "unavailable" ? "\u539f\u56fe\u4e0d\u53ef\u7528" : "\u56fe\u7247\u5ba1\u8ba1\u672a\u8bb0\u5f55";
      const placeholder = node("div", "image-button");
      placeholder.append(node("span", "muted", status), node("span", "muted", short(label, 40)));
      grid.append(placeholder);
      continue;
    }
    let source;
    try {
      if (typeof image.url !== "string") continue;
      source = new URL(image.url, document.URL);
      const prefix = `${apiBase}/events/${encodeURIComponent(event.event_ref)}/attachments/`;
      if (source.origin !== bridgeOrigin || source.username || source.password || source.search || source.hash
        || !source.pathname.startsWith(prefix) || !/^(?:input|reference|output)_[0-9a-f]{32}$/.test(source.pathname.slice(prefix.length))) continue;
      if (image.bytes > maxImageBytes || (image.mime && !imageTypes.has(image.mime))) continue;
    } catch { continue; }
    const path = source.pathname.slice(apiBase.length);
    let entry = attachmentCache.get(path);
    if (!entry) {
      entry = { path, controller: new AbortController(), consumers: new Map(), url: null, error: false, loading: false };
      attachmentCache.set(path, entry);
    }
    const imageLabel = image.name || image.label || image.meaning || "事件关联图片";
    const thumb = button("", () => {
      if (!entry.url || entry.error || attachmentCache.get(path) !== entry || !thumb.isConnected) return;
      $("image-title").textContent = imageLabel;
      $("image-preview").src = entry.url;
      $("image-preview").alt = imageLabel;
      $("image-caption").textContent = image.text || image.meaning || "";
      $("image-dialog").showModal();
    }, "image-button");
    thumb.dataset.focusKey = `image:${target.closest("[data-record-key]")?.dataset.recordKey || ""}:${source.pathname}`;
    const img = node("img");
    img.alt = imageLabel; img.loading = "lazy";
    img.addEventListener("error", () => { thumb.disabled = true; img.replaceWith(node("span", "muted", "图片不可用")); });
    thumb.append(img, node("span", "muted", short(imageLabel, 40)));
    entry.consumers.set(thumb, img);
    attachmentConsumers.set(thumb, entry);
    renderAttachment(entry, thumb, img);
    if (!entry.url && !entry.error) attachmentVisibility.observe(thumb);
    grid.append(thumb);
  }
  target.append(grid);
}
function ioEvent(event, title, recordKey) {
  const preview = eventPreview(event);
  const card = disclosure(title, preview == null ? "摘要未记录" : short(preview, 110), "message-record");
  card.item.dataset.recordKey = recordKey;
  card.item.dataset.detailKey = "record";
  card.content.append(node("pre", "prose preview", preview ?? "摘要未记录"), eventButton(event.event_ref, "查看完整记录"));
  appendImages(card.content, event);
  return card;
}
function deliveryCard(output, recordKey) {
  const confirmed = output.source === "message_delivery";
  const legacyTool = !confirmed && output.event_type === "tool_result";
  const card = section(confirmed ? "确认送达消息" : legacyTool ? "工具输出附件（旧版记录）" : "旧版回复摘要（审计不完整）");
  card.classList.add("delivery-record");
  if (!confirmed) card.classList.add("legacy-output");
  card.dataset.recordKey = recordKey;
  const content = typeof output.content === "string" && output.content ? output.content : confirmed ? "" : eventPreview(output);
  if (content) card.append(node("pre", "prose delivery-text", content));
  appendImages(card, output);
  for (const media of output.media || []) card.append(node("p", "muted", `${media.label || media.kind || "媒体"} · ${label(media.capture_status)}`));
  const legacyNote = legacyTool && output.effect !== "confirmed"
    ? "工具输出附件，送达状态未记录。生成成功不代表发送成功；未捕获的历史图片不可查看。"
    : "仅有旧版摘要 / 工具效果记录，缺少独立送达凭据，不能完整还原实际发送消息；未捕获的历史图片不可查看。";
  card.append(node("p", "muted", confirmed ? `${label(output.status)} · 捕获：${label(output.capture_status)} · ${date(output.confirmed_at)}` : legacyNote));
  card.append(eventButton(output.event_ref, "查看完整记录"), rawDetails(confirmed ? "送达记录" : "旧版输出记录", output));
  return card;
}
function renderIO() {
  const target = $("io-view");
  target.replaceChildren();
  const decision = state.inspection.response_decision;
  if (decision) target.append(section("\u81ea\u4e3b\u56de\u5e94\u4e0e\u5b9e\u9645\u9001\u8fbe", outcomeLabel(decision, state.turn.status)));
  const inputs = state.events.filter((event) => event.event_type === "user_input");
  if (!inputs.length) target.append(section("用户输入", "本轮未记录"));
  for (const [index, event] of inputs.entries()) target.append(ioEvent(event, "用户输入", `input:${event.event_ref || index}`).item);
  const outputs = state.inspection.outputs || [];
  if (!outputs.length) target.append(section("确认输出", "本轮没有独立确认输出记录；不把模型响应当作已送达消息。未捕获的历史图片无法还原。"));
  for (const [index, output] of outputs.entries()) {
    const event = { ...eventByRef(output.event_ref), ...output };
    target.append(deliveryCard(event, `output:${output.event_ref || index}`));
  }
  const advanced = node("details", "raw-details");
  advanced.append(node("summary", "", "高级轮次信息"), node("pre", "code", text(state.inspection.turn)));
  target.append(advanced);
}

function refreshPanels() {
  const detailKey = (item) => [
    item.closest(".view, #session-detail, #turn-header")?.id,
    item.closest("[data-record-key]")?.dataset.recordKey,
    item.dataset.detailKey || (item.classList.contains("usage-details") ? "usage" : item.querySelector("summary")?.textContent),
  ].join("|");
  const opened = new Set([...document.querySelectorAll(".workspace details[open]")].map(detailKey));
  const focused = document.activeElement;
  const focusKey = focused?.dataset.focusKey;
  const focusedDetail = focused?.tagName === "SUMMARY" ? detailKey(focused.parentElement) : null;
  const scrollPositions = [...document.querySelectorAll(".workspace .scroll, #session-detail")].map((item) => [item, item.scrollTop, item.scrollLeft]);
  const pageScroll = { top: window.scrollY, left: window.scrollX, behavior: "instant" };
  renderTurns(); renderSession(); renderWorkspace();
  for (const item of document.querySelectorAll(".workspace details")) {
    item.open = opened.has(detailKey(item));
    if (focusedDetail === detailKey(item)) item.querySelector("summary")?.focus({ preventScroll: true });
  }
  if (focusKey) {
    [...document.querySelectorAll(".workspace [data-focus-key]")]
      .find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
  }
  for (const [item, top, left] of scrollPositions) {
    item.scrollTop = top;
    item.scrollLeft = left;
  }
  window.scrollTo(pageScroll);
}

function stopPolling() {
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  $("auto-refresh").classList.add("is-hidden");
}
function startPolling() {
  stopPolling();
  const evaluation = state.inspection?.relationship_evaluation;
  const awaitingAffect = ["pending", "running", "failed"].includes(evaluation?.status)
    || (!evaluation && state.inspection?.relationship && ["completed", "silent", "declined"].includes(state.turn?.status));
  if (document.hidden || (state.turn?.status !== "running" && !awaitingAffect)) return;
  $("auto-refresh").classList.remove("is-hidden");
  state.pollTimer = setTimeout(pollTurn, 3000);
}
async function pollTurn() {
  if (state.pollBusy) { startPolling(); return; }
  const generation = state.navigation;
  const turnRef = state.turn?.turn_ref;
  const sessionRef = state.session?.session_ref;
  if (!turnRef || !sessionRef || document.hidden) return stopPolling();
  state.pollBusy = true;
  try {
    const [result, turns, detail] = await Promise.all([
      fetchTurn(turnRef), request(`/sessions/${encodeURIComponent(sessionRef)}/turns?limit=1000`), request(`/sessions/${encodeURIComponent(sessionRef)}`),
    ]);
    if (generation !== state.navigation) return;
    state.inspection = result.inspection; state.events = result.events; state.turn = result.inspection.turn;
    state.turns = turns.items || []; state.detail = detail.item;
    refreshPanels();
    if (state.turn.status !== "running") status(`本轮${label(state.turn.status)}`);
  } catch (error) { showError(error); }
  finally {
    state.pollBusy = false;
    if (generation === state.navigation) startPolling();
  }
}

function disposePayload() {
  if (state.payload) { state.payload.all = false; state.payload.controller?.abort(); }
  state.payload = null;
}
function openPayload(ref, title, path = "") {
  if (!ref) return;
  disposePayload();
  state.payload = { ref, title, path, text: "", next: 0, total: null, loaded: 0, busy: false, all: false, controller: null, hasPage: false };
  $("payload-title").textContent = title;
  $("payload-path").value = path;
  $("payload-error").textContent = "";
  $("payload-output").textContent = "";
  $("pin-event-button").textContent = "固定事件";
  $("pin-event-button").disabled = !state.scope || eventByRef(ref)?.model_visible === false;
  if (!$("payload-dialog").open) $("payload-dialog").showModal();
  loadPayloadPage().catch(payloadError);
}
function payloadError(error) {
  if (error?.name !== "AbortError" && state.payload) $("payload-error").textContent = error instanceof Error ? error.message : "读取失败，可重试。";
}
function renderPayloadProgress() {
  const payload = state.payload;
  if (!payload) return;
  const complete = payload.hasPage && payload.next === null;
  $("payload-meta").textContent = `${payload.busy ? "正在读取 · " : ""}${complete ? "完整" : "部分"} · 已加载 ${number(payload.loaded)} / ${number(payload.total)} 字符`;
  $("payload-next-button").disabled = payload.busy || payload.all || complete;
  $("payload-all-button").disabled = payload.busy || payload.all || complete;
  $("payload-stop-button").hidden = !payload.busy && !payload.all;
  $("payload-load-button").disabled = payload.busy || payload.all;
  $("payload-path").disabled = payload.busy || payload.all;
  $("payload-copy-button").disabled = !payload.hasPage;
  $("payload-copy-button").textContent = complete ? "复制完整内容" : "复制已加载（部分）";
}
async function loadPayloadPage() {
  const payload = state.payload;
  if (!payload || payload.busy || payload.next === null) return;
  payload.busy = true;
  payload.controller = new AbortController();
  $("payload-error").textContent = "";
  renderPayloadProgress();
  try {
    const offset = payload.next;
    const query = new URLSearchParams({ path: payload.path, offset: String(offset), limit: "16000" });
    const result = await request(`/events/${encodeURIComponent(payload.ref)}/payload?${query}`, { signal: payload.controller.signal });
    if (payload !== state.payload) return;
    const item = result.item;
    if (!["json", "text"].includes(item.format) || item.stored === true) throw new Error("服务器未提供可分页的负载，请升级后端；已加载内容保留。");
    if (item.offset !== offset || (item.next_offset !== null && (!Number.isInteger(item.next_offset) || item.next_offset <= offset))) throw new Error("负载分页位置异常，已停止加载并保留已有内容。");
    if (payload.total !== null && payload.total !== item.total_chars) throw new Error("记录已更新，请重新读取以避免混合不同版本。");
    if (item.format === "json" && (offset !== 0 || item.next_offset !== null)) throw new Error("JSON 分页格式异常。");
    const chunk = item.format === "json" ? JSON.stringify(item.data, null, 2) : item.data;
    if (typeof chunk !== "string") throw new Error("负载文本格式异常。");
    payload.text += chunk;
    payload.next = item.next_offset;
    payload.total = item.total_chars;
    payload.loaded = item.next_offset ?? item.total_chars;
    payload.hasPage = true;
    $("payload-output").textContent = payload.text;
  } finally {
    payload.busy = false;
    if (payload === state.payload) renderPayloadProgress();
  }
}
async function loadAllPayload() {
  const payload = state.payload;
  if (!payload || payload.busy || payload.all) return;
  payload.all = true;
  try {
    while (payload === state.payload && payload.all && payload.next !== null && $("payload-dialog").open) await loadPayloadPage();
  } catch (error) { payloadError(error); }
  finally { payload.all = false; if (payload === state.payload) renderPayloadProgress(); }
}
function confirmAction({ title, message, dangerous = false, requireToken = false, action }) {
  if (state.actionBusy) return;
  state.action = { action, requireToken };
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("confirmation-field").hidden = !requireToken;
  $("confirmation-input").value = "";
  $("confirm-error").textContent = "";
  $("confirm-action-button").className = dangerous ? "danger" : "primary";
  $("confirm-dialog").showModal();
  $("confirm-cancel-button").focus();
}
async function runAction() {
  if (!state.action || state.actionBusy) return;
  if (state.action.requireToken && $("confirmation-input").value !== "CONFIRM") {
    $("confirm-error").textContent = "必须输入 CONFIRM";
    $("confirmation-input").focus();
    return;
  }
  const action = state.action.action;
  state.action = null;
  state.actionBusy = true;
  $("confirm-dialog").close();
  renderScope();
  status("正在执行…");
  try { await action(); status("操作完成"); }
  finally { state.actionBusy = false; renderScope(); }
}
function confirmRollover(carry) {
  const scope = state.scope, session = state.session;
  if (!scope || session?.status !== "active") return;
  confirmAction({
    title: carry ? "续接会话" : "创建新会话",
    message: carry ? "生成交接并关闭当前会话，创建继续会话。" : "关闭当前话题，不携带会话交接。关系、画像、长期记忆和审计记录保留。",
    action: async () => {
      const result = await request(`/scopes/${encodeURIComponent(scope.scope_ref)}/sessions/${encodeURIComponent(session.session_ref)}/rollover`, { method: "POST", body: JSON.stringify({ carry_handoff: carry }) });
      if (state.scope === scope) await loadSessions(result.item.session_ref);
    },
  });
}
function confirmUnpin(anchor) {
  const scope = state.scope;
  if (!scope) return;
  confirmAction({ title: "取消固定", message: "此事件将不再作为固定锚点注入后续上下文，审计记录保留。", action: async () => {
    await request(`/scopes/${encodeURIComponent(scope.scope_ref)}/events/${encodeURIComponent(anchor.event_ref)}/pin`, { method: "DELETE" });
    if (state.scope === scope) await loadSessions(state.session?.session_ref);
  } });
}

const tabs = [...document.querySelectorAll("[role=tab]")];
function selectTab(selected) {
  for (const tab of tabs) {
    const active = selected === tab;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    tab.classList.toggle("is-active", active);
    $(`${tab.dataset.tab}-view`).hidden = !active;
  }
  document.querySelector(".inspector-body").scrollTop = 0;
}
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault(); selectTab(tabs[next]); tabs[next].focus();
  });
}
$("scope-select").addEventListener("change", () => {
  ++state.scopeLoad;
  beginNavigation();
  state.scope = state.scopes.find((scope) => scope.scope_ref === $("scope-select").value) || null;
  state.session = null;
  renderScope(); loadSessions().catch(showError);
});
$("refresh-button").addEventListener("click", () => loadScopes().catch(showError));
$("collapse-details-button").addEventListener("click", () => {
  for (const item of document.querySelectorAll(".inspector details[open]")) item.open = false;
});
$("new-session-button").addEventListener("click", () => confirmRollover(false));
$("rollover-button").addEventListener("click", () => confirmRollover(true));
$("hard-reset-button").addEventListener("click", () => {
  const scope = state.scope;
  if (!scope) return;
  confirmAction({ title: "硬重置会话", message: "所有旧会话将封存并从模型访问路径移除。审计事件不会删除。", dangerous: true, requireToken: true, action: async () => {
    const result = await request(`/scopes/${encodeURIComponent(scope.scope_ref)}/hard-reset`, { method: "POST", body: JSON.stringify({ confirmation: "CONFIRM" }) });
    if (state.scope === scope) await loadSessions(result.item.session_ref);
  } });
});
$("confirm-action-button").addEventListener("click", () => runAction().catch(showError));
$("confirm-dialog").addEventListener("close", () => { state.action = null; });
$("payload-load-button").addEventListener("click", () => {
  const payload = state.payload;
  if (payload && !payload.busy && !payload.all) openPayload(payload.ref, payload.title, $("payload-path").value.trim());
});
$("payload-next-button").addEventListener("click", () => loadPayloadPage().catch(payloadError));
$("payload-all-button").addEventListener("click", () => loadAllPayload());
$("payload-stop-button").addEventListener("click", () => {
  if (!state.payload) return;
  state.payload.all = false; state.payload.controller?.abort(); renderPayloadProgress();
});
$("payload-copy-button").addEventListener("click", async () => {
  const payload = state.payload;
  if (!payload?.hasPage) return;
  const loadedText = payload.text;
  const complete = payload.next === null;
  try { await navigator.clipboard.writeText(loadedText); if (payload === state.payload) $("payload-copy-button").textContent = complete ? "已复制完整内容" : "已复制部分内容"; }
  catch { $("payload-error").textContent = "剪贴板不可用，请选中已加载文本手动复制。"; }
});
$("payload-dialog").addEventListener("close", () => { disposePayload(); $("payload-output").textContent = ""; });
$("image-dialog").addEventListener("close", () => $("image-preview").removeAttribute("src"));
$("pin-event-button").addEventListener("click", () => {
  const scope = state.scope, payload = state.payload;
  if (!scope || !payload) return;
  confirmAction({ title: "固定事件", message: "此事件将作为固定锚点进入后续上下文。", action: async () => {
    await request(`/scopes/${encodeURIComponent(scope.scope_ref)}/events/${encodeURIComponent(payload.ref)}/pin`, { method: "POST", body: JSON.stringify({ label: payload.title.slice(0, 200) }) });
    if (state.payload === payload) { $("pin-event-button").disabled = true; $("pin-event-button").textContent = "已固定"; }
    if (state.scope === scope) await loadSessions(state.session?.session_ref);
  } });
});
document.addEventListener("visibilitychange", () => document.hidden ? stopPolling() : startPolling());
window.addEventListener("pagehide", () => {
  pageActive = false;
  stopPolling();
  pageController.abort();
  beginNavigation();
  attachmentVisibility.disconnect();
  attachmentRemoval.disconnect();
});
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  pageActive = true;
  pageController = new AbortController();
  attachmentRemoval.observe(document.body, { childList: true, subtree: true });
  loadScopes().catch(showError);
});
loadScopes().catch(showError);
