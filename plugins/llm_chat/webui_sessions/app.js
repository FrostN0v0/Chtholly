"use strict";

const apiBase = "/api/llm-chat/sessions";

const EVENT_TYPE_LABELS = {
  user_input: "用户输入",
  assistant_output: "最终回复",
  assistant_tool_call: "工具调用",
  tool_result: "工具结果",
  model_attempt: "模型调用",
  context_selection: "上下文选择",
  persona_state: "人格与记忆",
};

const PERSONA_ROW_GROUPS = [
  ["relation", "关系轴"],
  ["state", "当前状态"],
  ["budgets", "Token 预算"],
  ["retrieval", "检索概况"],
  ["thresholds", "阈值"],
];

const PERSONA_CARD_GROUPS = [
  ["profile_facts", "画像事实", "检索到的画像事实，尚未必然进入 prompt。"],
  ["memories", "命中记忆", "检索命中的长期记忆，尚未必然进入 prompt。"],
];

const TURN_POLL_INTERVAL_MS = 3000;

const STATUS_LABELS = {
  active: "进行中",
  closed: "已关闭",
  sealed: "已封存",
  confirmed: "已确认",
  requested: "已发起",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  partial: "部分完成",
  rejected: "已拒绝",
  completed: "已完成",
  recorded: "已记录",
  running: "生成中",
  none: "无副作用",
};

const TAG_VARIANTS = {
  active: "success",
  succeeded: "success",
  confirmed: "success",
  completed: "success",
  failed: "danger",
  cancelled: "danger",
  rejected: "danger",
  partial: "warning",
  sealed: "warning",
  requested: "primary",
  closed: "primary",
  running: "primary",
};

const REASON_LABELS = {
  initial: "首次创建",
  idle: "空闲超时",
  turn_limit: "轮次上限",
  runtime_change: "运行时变更",
  hard_reset: "硬重置",
  webui_new: "新会话",
  webui_rollover: "续接会话",
  webui_hard_reset: "硬重置",
  legacy_import: "历史导入",
};

const FIELD_LABELS = {
  text: "文本",
  content: "内容",
  query: "查询",
  image: "图片",
  images: "图片",
  path: "路径",
  arguments: "调用参数",
  result: "返回结果",
  duration: "耗时",
  duration_ms: "耗时",
  tool: "工具",
  model: "模型",
  meaning: "含义",
  reason: "原因",
  error: "错误",
  speaker: "发言人",
  estimated_tokens: "预估 token",
  full_session_tokens: "会话总 token",
};

const state = {
  scopes: [],
  scope: null,
  sessions: [],
  session: null,
  sessionDetail: null,
  turns: [],
  turn: null,
  events: [],
  event: null,
  payloadPath: "",
  payloadOffset: 0,
  payloadNextOffset: null,
  pollTimer: null,
  pollTurnRef: "",
  pollBusy: false,
  confirmAction: null,
};

const elements = {
  status: document.querySelector("#status"),
  refresh: document.querySelector("#refresh-button"),
  scopeSelect: document.querySelector("#scope-select"),
  scopeMeta: document.querySelector("#scope-meta"),
  newSession: document.querySelector("#new-session-button"),
  rollover: document.querySelector("#rollover-button"),
  hardReset: document.querySelector("#hard-reset-button"),
  sessionCount: document.querySelector("#session-count"),
  sessionList: document.querySelector("#session-list"),
  sessionDetail: document.querySelector("#session-detail"),
  turnCount: document.querySelector("#turn-count"),
  turnList: document.querySelector("#turn-list"),
  eventList: document.querySelector("#event-list"),
  eventDetail: document.querySelector("#event-detail"),
  contextOutput: document.querySelector("#context-output"),
  personaOutput: document.querySelector("#persona-output"),
  autoRefresh: document.querySelector("#auto-refresh"),
  payloadDialog: document.querySelector("#payload-dialog"),
  payloadTitle: document.querySelector("#payload-title"),
  payloadPath: document.querySelector("#payload-path"),
  payloadLoad: document.querySelector("#payload-load-button"),
  payloadNext: document.querySelector("#payload-next-button"),
  payloadOutput: document.querySelector("#payload-output"),
  payloadMeta: document.querySelector("#payload-meta"),
  pinEvent: document.querySelector("#pin-event-button"),
  imageDialog: document.querySelector("#image-dialog"),
  imageTitle: document.querySelector("#image-title"),
  imagePreview: document.querySelector("#image-preview"),
  imageCaption: document.querySelector("#image-caption"),
  confirmDialog: document.querySelector("#confirm-dialog"),
  confirmTitle: document.querySelector("#confirm-title"),
  confirmMessage: document.querySelector("#confirm-message"),
  confirmationField: document.querySelector("#confirmation-field"),
  confirmationInput: document.querySelector("#confirmation-input"),
  confirmAction: document.querySelector("#confirm-action-button"),
};

function createElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function clearNode(node, emptyText = "") {
  node.replaceChildren();
  node.classList.toggle("is-empty", Boolean(emptyText));
  if (emptyText) node.textContent = emptyText;
}

function setStatus(message, kind = "") {
  elements.status.textContent = message;
  elements.status.className = kind ? `status is-${kind}` : "status";
}

function showError(error) {
  console.error(error);
  setStatus(error instanceof Error ? error.message : String(error), "error");
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({ success: false, message: "服务器响应无法解析" }));
  if (!response.ok || payload.success === false) {
    throw new Error(payload.message || `请求失败（${response.status}）`);
  }
  return payload;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function truncate(value, maximum = 120) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function statusLabel(value) {
  const key = String(value ?? "").toLowerCase();
  return STATUS_LABELS[key] || value || "未知";
}

function reasonLabel(value) {
  const key = String(value ?? "").toLowerCase();
  return REASON_LABELS[key] || value || "—";
}

function eventTypeLabel(value) {
  const key = String(value ?? "");
  return EVENT_TYPE_LABELS[key] || key || "未知事件";
}

function fieldLabel(value) {
  const key = String(value ?? "");
  return FIELD_LABELS[key] || key;
}

function formatChars(value) {
  const count = Number(value);
  return Number.isFinite(count) ? `${count} 字符` : "";
}

function tagVariant(value) {
  return TAG_VARIANTS[String(value ?? "").toLowerCase()] || "";
}

function tag(value, variant = "") {
  const kind = variant || tagVariant(value);
  return createElement("span", kind ? `tag tag--${kind}` : "tag", statusLabel(value));
}

function scopeName(scope) {
  const raw = String(scope?.channel_name || scope?.display_name || "");
  const id = String(scope?.channel_id || "");
  const cleaned = Array.from(raw.replace(/\s/g, " "))
    .filter((character) => character >= " " && character !== "\u007f")
    .join("")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned === id ? "" : cleaned;
}

function scopeTitle(scope) {
  if (!scope) return "尚未选择聊天范围";
  const name = scopeName(scope);
  return name ? `${name}（${scope.channel_id}）` : String(scope.channel_id || "未命名频道");
}

function scopeOptionLabel(scope) {
  const name = scopeName(scope);
  return name ? `${name}（${scope.channel_id}）` : String(scope.channel_id || "未命名频道");
}

function itemRow(title, status) {
  const row = createElement("div", "list-item__row");
  row.append(createElement("span", "list-item__title", title), tag(status));
  return row;
}

function metaRow(parts) {
  return createElement("div", "list-item__meta", parts.filter(Boolean).join(" · "));
}

async function loadScopes(preferredRef = "") {
  setStatus("正在读取…");
  const payload = await request("/scopes?limit=500");
  state.scopes = payload.items || [];
  elements.scopeSelect.replaceChildren();
  if (!state.scopes.length) {
    const option = createElement("option", "", "暂无聊天范围");
    option.value = "";
    elements.scopeSelect.append(option);
    state.scope = null;
    renderScope();
    setStatus("暂无数据");
    return;
  }
  for (const scope of state.scopes) {
    const option = createElement("option", "", scopeOptionLabel(scope));
    option.value = scope.scope_ref;
    elements.scopeSelect.append(option);
  }
  const selected = state.scopes.find((item) => item.scope_ref === preferredRef) || state.scopes[0];
  elements.scopeSelect.value = selected.scope_ref;
  state.scope = selected;
  renderScope();
  await loadSessions();
  setStatus("已同步", "success");
}

function renderScope() {
  const scope = state.scope;
  clearNode(elements.scopeMeta);
  if (!scope) {
    elements.scopeMeta.textContent = "尚未选择聊天范围";
  } else {
    elements.scopeMeta.append(
      createElement("strong", "toolbar__scope-name", scopeTitle(scope)),
      createElement("span", "", `${scope.platform} · 更新于 ${formatDate(scope.updated_at)}`),
    );
  }
  const active = Boolean(scope) && state.session?.status === "active";
  elements.newSession.disabled = !active;
  elements.rollover.disabled = !active;
  elements.hardReset.disabled = !scope;
}

async function loadSessions(preferredRef = "") {
  if (!state.scope) return;
  const payload = await request(`/scopes/${encodeURIComponent(state.scope.scope_ref)}/sessions?limit=500`);
  state.sessions = payload.items || [];
  elements.sessionCount.textContent = String(state.sessions.length);
  renderSessions();
  const selected = state.sessions.find((item) => item.session_ref === preferredRef)
    || state.sessions.find((item) => item.status === "active")
    || state.sessions[0]
    || null;
  if (selected) {
    await selectSession(selected);
  } else {
    stopTurnPolling();
    state.session = null;
    state.sessionDetail = null;
    state.turns = [];
    state.events = [];
    state.event = null;
    renderSessionDetail();
    renderTurns();
    renderEvents();
    renderEventDetail();
    renderPersona();
  }
  renderScope();
}

function renderSessions() {
  clearNode(elements.sessionList, state.sessions.length ? "" : "暂无会话");
  for (const session of state.sessions) {
    const button = createElement("button", "list-item");
    button.type = "button";
    if (state.session?.session_ref === session.session_ref) button.classList.add("is-active");
    button.append(
      itemRow(`会话 #${session.sequence}`, session.status),
      createElement("div", "list-item__subtitle", `${reasonLabel(session.start_reason)} · ${session.turn_count} 轮`),
      metaRow([formatDate(session.created_at)]),
    );
    button.addEventListener("click", () => selectSession(session).catch(showError));
    elements.sessionList.append(button);
  }
}

async function selectSession(session) {
  stopTurnPolling();
  state.session = session;
  state.turn = null;
  state.events = [];
  state.event = null;
  renderSessions();
  renderScope();
  const [detailPayload, turnsPayload] = await Promise.all([
    request(`/sessions/${encodeURIComponent(session.session_ref)}`),
    request(`/sessions/${encodeURIComponent(session.session_ref)}/turns?limit=1000`),
  ]);
  state.sessionDetail = detailPayload.item;
  state.turns = turnsPayload.items || [];
  renderSessionDetail();
  renderTurns();
  renderEvents();
  renderEventDetail();
  renderPersona();
  if (state.turns.length) await selectTurn(state.turns[state.turns.length - 1]);
}

function renderSessionDetail() {
  const detail = state.sessionDetail;
  clearNode(elements.sessionDetail);
  if (!detail) {
    elements.sessionDetail.classList.add("is-empty");
    elements.sessionDetail.textContent = "请选择一个会话。";
    return;
  }
  elements.sessionDetail.classList.remove("is-empty");
  elements.sessionDetail.append(
    createElement("div", "session-detail__row", `${detail.model} · ${reasonLabel(detail.start_reason)} · ${detail.turn_count} 轮`),
  );
  if (detail.handoff && Object.keys(detail.handoff).length) {
    elements.sessionDetail.append(
      createElement("div", "session-detail__handoff", `交接：${truncate(detail.handoff.topic || JSON.stringify(detail.handoff), 180)}`),
    );
  }
  if (Array.isArray(detail.anchors) && detail.anchors.length) {
    const anchors = createElement("div", "anchors");
    for (const item of detail.anchors) {
      const button = createElement("button", "anchor", `${item.label} ×`);
      button.type = "button";
      button.title = "取消固定";
      button.addEventListener("click", () => unpinAnchor(item).catch(showError));
      anchors.append(button);
    }
    elements.sessionDetail.append(anchors);
  }
}

async function unpinAnchor(item) {
  if (!state.scope) return;
  await request(
    `/scopes/${encodeURIComponent(state.scope.scope_ref)}/events/${encodeURIComponent(item.event_ref)}/pin`,
    { method: "DELETE" },
  );
  setStatus("已取消固定", "success");
  await selectSession(state.session);
}

function isRunningTurn(turn) {
  return String(turn?.status ?? "").toLowerCase() === "running";
}

function renderTurns() {
  elements.turnCount.textContent = String(state.turns.length);
  clearNode(elements.turnList, state.turns.length ? "" : "暂无轮次");
  for (const turn of state.turns) {
    const running = isRunningTurn(turn);
    const button = createElement("button", "list-item list-item--turn");
    button.type = "button";
    if (state.turn?.turn_ref === turn.turn_ref) button.classList.add("is-active");
    if (running) button.classList.add("is-running");
    button.append(
      itemRow(`#${turn.sequence} · ${turn.user_name || turn.user_id}`, turn.status),
      createElement(
        "div",
        running && !turn.final_text ? "list-item__text list-item__text--pending" : "list-item__text",
        turn.final_text || (running ? "生成中…" : "无确认文本输出"),
      ),
      metaRow([formatDate(turn.created_at), turn.fresh_context ? "本轮忽略前文" : "继承会话上下文"]),
    );
    button.addEventListener("click", () => selectTurn(turn).catch(showError));
    elements.turnList.append(button);
  }
}

async function selectTurn(turn) {
  stopTurnPolling();
  state.turn = turn;
  state.event = null;
  renderTurns();
  const [eventsPayload, contextPayload] = await Promise.all([
    request(`/turns/${encodeURIComponent(turn.turn_ref)}/events`),
    request(`/turns/${encodeURIComponent(turn.turn_ref)}/context`),
  ]);
  state.events = eventsPayload.items || [];
  renderEvents();
  renderPersona();
  elements.contextOutput.textContent = JSON.stringify(contextPayload.item, null, 2);
  if (state.events.length) selectEvent(state.events[0]);
  else renderEventDetail();
  if (isRunningTurn(turn)) startTurnPolling(turn);
}

function setAutoRefresh(active) {
  elements.autoRefresh.classList.toggle("is-hidden", !active);
}

function stopTurnPolling() {
  if (state.pollTimer !== null) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  state.pollTurnRef = "";
  state.pollBusy = false;
  setAutoRefresh(false);
}

function startTurnPolling(turn) {
  stopTurnPolling();
  state.pollTurnRef = turn.turn_ref;
  setAutoRefresh(true);
  state.pollTimer = window.setInterval(() => {
    pollRunningTurn().catch(() => stopTurnPolling());
  }, TURN_POLL_INTERVAL_MS);
}

async function pollRunningTurn() {
  const turnRef = state.pollTurnRef;
  if (!turnRef || state.pollBusy) return;
  if (!state.turn || state.turn.turn_ref !== turnRef) {
    stopTurnPolling();
    return;
  }
  state.pollBusy = true;
  try {
    const [eventsPayload, turnsPayload] = await Promise.all([
      request(`/turns/${encodeURIComponent(turnRef)}/events`),
      state.session
        ? request(`/sessions/${encodeURIComponent(state.session.session_ref)}/turns?limit=1000`)
        : Promise.resolve(null),
    ]);
    if (state.pollTurnRef !== turnRef || state.turn?.turn_ref !== turnRef) return;
    const items = eventsPayload.items || [];
    if (items.length) {
      state.events = items;
      const pinned = state.event
        && items.find((item) => item.event_ref === state.event.event_ref);
      state.event = pinned || items[0];
      renderEvents();
      renderEventDetail();
      renderPersona();
    }
    const fresh = turnsPayload
      && (turnsPayload.items || []).find((item) => item.turn_ref === turnRef);
    if (fresh) {
      state.turns = turnsPayload.items || [];
      state.turn = fresh;
      renderTurns();
      if (!isRunningTurn(fresh)) {
        stopTurnPolling();
        setStatus("本轮已完成", "success");
      }
    }
  } finally {
    state.pollBusy = false;
  }
}

function personaState() {
  const event = state.events.find((item) => item.event_type === "persona_state");
  return event?.persona || null;
}

function personaRows(rows) {
  const list = createElement("dl", "persona__rows");
  for (const row of rows) {
    list.append(
      createElement("dt", "persona__key", String(row.label ?? "")),
      createElement("dd", "persona__value", String(row.value ?? "")),
    );
  }
  return list;
}

function personaCards(items) {
  const list = createElement("div", "persona__cards");
  for (const item of items) {
    const card = createElement("article", "persona__card");
    const label = String(item.label ?? "");
    if (label) card.append(createElement("span", "persona__card-label", label));
    card.append(createElement("p", "persona__card-text", String(item.text ?? "")));
    const scores = String(item.scores ?? "");
    if (scores) card.append(createElement("p", "persona__card-scores", scores));
    list.append(card);
  }
  return list;
}

function personaGroup(title, note = "") {
  const group = createElement("section", "persona__group");
  group.append(createElement("h3", "persona__label", title));
  if (note) group.append(createElement("p", "persona__note", note));
  return group;
}

function renderPersona() {
  const container = elements.personaOutput;
  clearNode(container);
  if (!state.turn) {
    container.classList.add("is-empty");
    container.textContent = "请选择一个轮次。";
    return;
  }
  const persona = personaState();
  if (!persona) {
    container.classList.add("is-empty");
    container.textContent = "本轮没有记录人格与记忆快照";
    return;
  }
  container.classList.remove("is-empty");

  for (const [key, title] of PERSONA_ROW_GROUPS) {
    const rows = Array.isArray(persona[key]) ? persona[key] : [];
    if (!rows.length) continue;
    const group = personaGroup(title);
    group.append(personaRows(rows));
    container.append(group);
  }

  for (const [key, title, note] of PERSONA_CARD_GROUPS) {
    const items = Array.isArray(persona[key]) ? persona[key] : [];
    if (!items.length) continue;
    const group = personaGroup(title, note);
    group.append(personaCards(items));
    container.append(group);
  }

  const injectedProfile = Array.isArray(persona.injected_profile) ? persona.injected_profile : [];
  const injectedMemories = Array.isArray(persona.injected_memories) ? persona.injected_memories : [];
  if (injectedProfile.length || injectedMemories.length) {
    const group = personaGroup("实际注入 prompt", "以下内容真正进入了本轮 prompt。");
    group.classList.add("persona__group--injected");
    if (injectedProfile.length) {
      group.append(createElement("h4", "persona__sublabel", "注入画像"));
      group.append(personaCards(injectedProfile));
    }
    if (injectedMemories.length) {
      group.append(createElement("h4", "persona__sublabel", "注入记忆"));
      const list = createElement("ul", "persona__memories");
      for (const memory of injectedMemories) {
        list.append(createElement("li", "persona__memory", String(memory ?? "")));
      }
      group.append(list);
    }
    container.append(group);
  }

  if (!container.childElementCount) {
    container.classList.add("is-empty");
    container.textContent = "本轮没有记录人格与记忆快照";
  }
}

function eventTitle(event) {
  if (event.title) return String(event.title);
  return event.tool || eventTypeLabel(event.event_type);
}

function previewText(event) {
  const preview = event.preview;
  if (typeof preview === "string") return preview;
  if (preview && typeof preview === "object" && typeof preview.text === "string") return preview.text;
  return "";
}

function renderEvents() {
  clearNode(elements.eventList, state.events.length ? "" : "该轮次没有事件");
  for (const event of state.events) {
    const button = createElement("button", "list-item list-item--event");
    button.type = "button";
    if (state.event?.event_ref === event.event_ref) button.classList.add("is-active");
    button.append(itemRow(`${event.sequence}. ${eventTitle(event)}`, event.status || event.effect || "recorded"));
    const preview = truncate(previewText(event), 90);
    if (preview) button.append(createElement("div", "list-item__text", preview));
    button.append(metaRow([
      eventTypeLabel(event.event_type),
      event.attempt ? `第 ${event.attempt} 次尝试` : "",
      event.duration_ms ? `${event.duration_ms} ms` : "",
    ]));
    button.addEventListener("click", () => selectEvent(event));
    elements.eventList.append(button);
  }
}

function selectEvent(event) {
  state.event = event;
  renderEvents();
  renderEventDetail();
}

function detailSection(title) {
  const section = createElement("section", "section");
  section.append(createElement("h3", "section__label", title));
  return section;
}

function structuredBlock(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "object" && value.stored === true) {
    return createElement("p", "section__note", `内容过大未内联（${formatChars(value.chars)}），请用原生 JSON 查看。`);
  }
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (!text || text === "{}" || text === "[]") return null;
  return createElement("pre", "code code--inline", text);
}

function imageThumb(image) {
  const figure = createElement("figure", "thumb");
  const label = String(image.path || image.name || image.url || "图片");
  const source = image.url || image.image_url;
  if (source) {
    const img = createElement("img", "thumb__image");
    img.src = source;
    img.alt = label;
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.replaceWith(createElement("span", "thumb__fallback", label.split("/").pop() || label));
    });
    const button = createElement("button", "thumb__button");
    button.type = "button";
    button.title = "放大查看";
    button.append(img);
    button.addEventListener("click", () => openImage(source, label, image.meaning || image.text || ""));
    figure.append(button);
  } else {
    figure.append(createElement("span", "thumb__fallback", label.split("/").pop() || label));
  }
  const caption = image.meaning || image.text || label.split("/").pop() || label;
  figure.append(createElement("figcaption", "thumb__caption", truncate(caption, 40)));
  return figure;
}

function renderEventDetail() {
  const event = state.event;
  clearNode(elements.eventDetail);
  if (!event) {
    elements.eventDetail.classList.add("is-empty");
    elements.eventDetail.textContent = "请选择一个事件。";
    return;
  }
  elements.eventDetail.classList.remove("is-empty");

  const head = createElement("div", "detail__head");
  const heading = createElement("div", "detail__title");
  heading.append(
    createElement("span", "detail__name", eventTitle(event)),
    tag(event.status || event.effect || "recorded"),
  );
  const actions = createElement("div", "detail__actions");
  const jsonButton = createElement("button", "button button--small", "原生 JSON");
  jsonButton.type = "button";
  jsonButton.addEventListener("click", () => openPayload(event));
  actions.append(jsonButton);
  head.append(heading, actions);
  elements.eventDetail.append(head);

  elements.eventDetail.append(metaRow([
    eventTypeLabel(event.event_type),
    event.role ? `角色 ${event.role}` : "",
    event.attempt ? `第 ${event.attempt} 次尝试` : "",
    event.duration_ms ? `${event.duration_ms} ms` : "",
    event.effect ? `副作用 ${statusLabel(event.effect)}` : "",
    formatChars(event.payload_chars),
    event.model_visible === false ? "模型不可见" : "",
    formatDate(event.created_at),
  ]));

  const preview = previewText(event);
  if (preview) {
    const section = detailSection("关键内容");
    section.append(createElement("p", "preview-text", preview));
    elements.eventDetail.append(section);
  }

  const fields = Array.isArray(event.details) ? event.details : event.preview?.fields;
  if (Array.isArray(fields) && fields.length) {
    const section = detailSection("关键字段");
    const list = createElement("dl", "fields");
    for (const field of fields) {
      list.append(
        createElement("dt", "fields__key", fieldLabel(field.label)),
        createElement("dd", "fields__value", truncate(field.value, 400)),
      );
    }
    section.append(list);
    elements.eventDetail.append(section);
  }

  const images = event.evidence?.images || event.preview?.images;
  if (Array.isArray(images) && images.length) {
    const section = detailSection("关联图片");
    const grid = createElement("div", "thumbs");
    for (const image of images) grid.append(imageThumb(image));
    section.append(grid);
    elements.eventDetail.append(section);
  }

  for (const [key, label] of [["arguments", "调用参数"], ["result", "返回结果"]]) {
    const block = structuredBlock(event[key]);
    if (block) {
      const section = detailSection(label);
      section.append(block);
      elements.eventDetail.append(section);
    }
  }

  if (Array.isArray(event.payload_keys) && event.payload_keys.length) {
    const section = detailSection("负载字段");
    const keys = createElement("div", "keys");
    for (const key of event.payload_keys) keys.append(createElement("code", "keys__key", key));
    section.append(keys);
    elements.eventDetail.append(section);
  }
}

function openImage(source, label, caption) {
  elements.imageTitle.textContent = label.split("/").pop() || label;
  elements.imagePreview.src = source;
  elements.imagePreview.alt = label;
  elements.imageCaption.textContent = caption || label;
  elements.imageDialog.showModal();
}

function openPayload(event) {
  state.event = event;
  state.payloadPath = "";
  state.payloadOffset = 0;
  state.payloadNextOffset = null;
  elements.payloadTitle.textContent = `原生 JSON · ${eventTitle(event)}`;
  elements.payloadPath.value = "";
  elements.payloadOutput.textContent = "正在读取…";
  elements.payloadMeta.textContent = "";
  elements.payloadNext.disabled = true;
  elements.pinEvent.disabled = !state.scope;
  elements.pinEvent.textContent = "固定事件";
  elements.payloadDialog.showModal();
  loadPayload(0).catch((error) => {
    elements.payloadOutput.textContent = error instanceof Error ? error.message : String(error);
  });
}

async function loadPayload(offset = 0) {
  if (!state.event) return;
  const path = elements.payloadPath.value.trim();
  const query = new URLSearchParams({ path, offset: String(offset), limit: "100000" });
  const payload = await request(`/events/${encodeURIComponent(state.event.event_ref)}/payload?${query}`);
  const item = payload.item;
  state.payloadPath = path;
  state.payloadOffset = item.offset || 0;
  state.payloadNextOffset = item.next_offset ?? null;
  if (item.stored === true) {
    elements.payloadOutput.textContent = `内容过大未内联（${formatChars(item.chars)}），请填写更精确的字段路径。`;
  } else if (typeof item.data === "string") {
    elements.payloadOutput.textContent = item.data || "（空字符串）";
  } else {
    elements.payloadOutput.textContent = JSON.stringify(item.data, null, 2);
  }
  elements.payloadMeta.textContent = [
    item.total_chars ? `共 ${formatChars(item.total_chars)}` : "",
    state.payloadOffset ? `偏移 ${state.payloadOffset}` : "",
  ].filter(Boolean).join(" · ");
  elements.payloadNext.disabled = state.payloadNextOffset === null;
}

function showConfirm({ title, message, dangerous = false, requireToken = false, action }) {
  state.confirmAction = action;
  elements.confirmTitle.textContent = title;
  elements.confirmMessage.textContent = message;
  elements.confirmationField.classList.toggle("is-hidden", !requireToken);
  elements.confirmationInput.value = "";
  elements.confirmAction.className = `button ${dangerous ? "button--danger" : "button--primary"}`;
  elements.confirmDialog.showModal();
}

async function runConfirmedAction() {
  if (!state.confirmAction) return;
  const action = state.confirmAction;
  state.confirmAction = null;
  const token = elements.confirmationInput.value;
  elements.confirmDialog.close();
  setStatus("正在执行…");
  await action(token);
  setStatus("操作完成", "success");
}

for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => {
    for (const tabButton of document.querySelectorAll(".tab")) {
      const active = tabButton === button;
      tabButton.classList.toggle("is-active", active);
      tabButton.setAttribute("aria-selected", active ? "true" : "false");
    }
    for (const view of document.querySelectorAll(".view")) {
      view.classList.toggle("is-active", view.id === `${button.dataset.tab}-view`);
    }
  });
}

elements.scopeSelect.addEventListener("change", () => {
  stopTurnPolling();
  state.scope = state.scopes.find((item) => item.scope_ref === elements.scopeSelect.value) || null;
  state.session = null;
  renderScope();
  loadSessions().catch(showError);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopTurnPolling();
  } else if (isRunningTurn(state.turn)) {
    startTurnPolling(state.turn);
  }
});

elements.refresh.addEventListener("click", () => loadScopes(state.scope?.scope_ref || "").catch(showError));
elements.payloadLoad.addEventListener("click", () => loadPayload(0).catch(showError));
elements.payloadNext.addEventListener("click", () => {
  if (state.payloadNextOffset !== null) loadPayload(state.payloadNextOffset).catch(showError);
});

elements.newSession.addEventListener("click", () => {
  if (!state.scope || !state.session) return;
  showConfirm({
    title: "创建新会话",
    message: "当前话题将关闭；关系、画像和长期记忆保留，但不携带会话交接。",
    action: async () => {
      const payload = await request(
        `/scopes/${encodeURIComponent(state.scope.scope_ref)}/sessions/${encodeURIComponent(state.session.session_ref)}/rollover`,
        { method: "POST", body: JSON.stringify({ carry_handoff: false }) },
      );
      await loadSessions(payload.item.session_ref);
    },
  });
});

elements.rollover.addEventListener("click", () => {
  if (!state.scope || !state.session) return;
  showConfirm({
    title: "续接会话",
    message: "系统将生成结构化交接，关闭当前会话并创建继续会话。",
    action: async () => {
      const payload = await request(
        `/scopes/${encodeURIComponent(state.scope.scope_ref)}/sessions/${encodeURIComponent(state.session.session_ref)}/rollover`,
        { method: "POST", body: JSON.stringify({ carry_handoff: true }) },
      );
      await loadSessions(payload.item.session_ref);
    },
  });
});

elements.hardReset.addEventListener("click", () => {
  if (!state.scope) return;
  showConfirm({
    title: "硬重置会话",
    message: "所有旧会话将被封存并从模型访问路径移除。审计事件不会删除。",
    dangerous: true,
    requireToken: true,
    action: async (token) => {
      if (token !== "CONFIRM") throw new Error("必须输入 CONFIRM");
      const payload = await request(`/scopes/${encodeURIComponent(state.scope.scope_ref)}/hard-reset`, {
        method: "POST",
        body: JSON.stringify({ confirmation: "CONFIRM" }),
      });
      await loadSessions(payload.item.session_ref);
    },
  });
});

elements.pinEvent.addEventListener("click", async () => {
  if (!state.scope || !state.event) return;
  const label = `${eventTitle(state.event)} · ${formatDate(state.event.created_at)}`;
  try {
    await request(
      `/scopes/${encodeURIComponent(state.scope.scope_ref)}/events/${encodeURIComponent(state.event.event_ref)}/pin`,
      { method: "POST", body: JSON.stringify({ label }) },
    );
    elements.pinEvent.textContent = "已固定";
    elements.pinEvent.disabled = true;
    setStatus("已固定事件", "success");
    await selectSession(state.session);
  } catch (error) {
    showError(error);
  }
});

elements.confirmAction.addEventListener("click", () => runConfirmedAction().catch(showError));

loadScopes().catch(showError);
