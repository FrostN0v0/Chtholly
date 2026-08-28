"use strict";

const apiBase = "/api/llm-chat/sessions";
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
  contextOutput: document.querySelector("#context-output"),
  payloadDialog: document.querySelector("#payload-dialog"),
  payloadTitle: document.querySelector("#payload-title"),
  payloadPath: document.querySelector("#payload-path"),
  payloadLoad: document.querySelector("#payload-load-button"),
  payloadNext: document.querySelector("#payload-next-button"),
  payloadOutput: document.querySelector("#payload-output"),
  pinEvent: document.querySelector("#pin-event-button"),
  confirmDialog: document.querySelector("#confirm-dialog"),
  confirmTitle: document.querySelector("#confirm-title"),
  confirmMessage: document.querySelector("#confirm-message"),
  confirmationField: document.querySelector("#confirmation-field"),
  confirmationInput: document.querySelector("#confirmation-input"),
  confirmAction: document.querySelector("#confirm-action-button"),
};

function setStatus(message, kind = "") {
  elements.status.textContent = message;
  elements.status.className = `status ${kind}`.trim();
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({ success: false, message: "Invalid server response" }));
  if (!response.ok || payload.success === false) {
    throw new Error(payload.message || `Request failed (${response.status})`);
  }
  return payload;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function truncate(value, maximum = 120) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function badge(value) {
  const node = document.createElement("span");
  node.className = `badge ${String(value || "").toLowerCase()}`;
  node.textContent = value || "unknown";
  return node;
}

function clearNode(node, emptyText = "") {
  node.replaceChildren();
  node.classList.toggle("empty-state", Boolean(emptyText));
  if (emptyText) node.textContent = emptyText;
}

function itemRow(title, status) {
  const row = document.createElement("div");
  row.className = "item-row";
  const label = document.createElement("span");
  label.className = "item-title";
  label.textContent = title;
  row.append(label, badge(status));
  return row;
}

async function loadScopes(preferredRef = "") {
  setStatus("正在读取…");
  const payload = await request("/scopes?limit=500");
  state.scopes = payload.items || [];
  elements.scopeSelect.replaceChildren();
  if (!state.scopes.length) {
    const option = document.createElement("option");
    option.textContent = "暂无聊天范围";
    option.value = "";
    elements.scopeSelect.append(option);
    state.scope = null;
    renderScope();
    setStatus("暂无数据");
    return;
  }
  for (const scope of state.scopes) {
    const option = document.createElement("option");
    option.value = scope.scope_ref;
    option.textContent = scope.display_name || `${scope.platform}:${scope.channel_id}`;
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
  elements.scopeMeta.textContent = scope
    ? `${scope.platform} · ${scope.channel_id} · 更新于 ${formatDate(scope.updated_at)}`
    : "尚未选择聊天范围";
  const enabled = Boolean(scope);
  elements.newSession.disabled = !enabled || !state.session || state.session.status !== "active";
  elements.rollover.disabled = !enabled || !state.session || state.session.status !== "active";
  elements.hardReset.disabled = !enabled;
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
    state.session = null;
    state.sessionDetail = null;
    state.turns = [];
    renderSessionDetail();
    renderTurns();
  }
  renderScope();
}

function renderSessions() {
  clearNode(elements.sessionList, state.sessions.length ? "" : "暂无会话");
  for (const session of state.sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `list-item${state.session?.session_ref === session.session_ref ? " active" : ""}`;
    button.append(itemRow(`会话 #${session.sequence}`, session.status));
    const subtitle = document.createElement("div");
    subtitle.className = "item-subtitle";
    subtitle.textContent = `${session.start_reason} · ${session.turn_count} 轮`;
    const meta = document.createElement("div");
    meta.className = "meta-row";
    meta.append(document.createTextNode(formatDate(session.created_at)));
    button.append(subtitle, meta);
    button.addEventListener("click", () => selectSession(session).catch(showError));
    elements.sessionList.append(button);
  }
}

async function selectSession(session) {
  state.session = session;
  state.turn = null;
  state.events = [];
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
  if (state.turns.length) await selectTurn(state.turns[state.turns.length - 1]);
}

function renderSessionDetail() {
  const detail = state.sessionDetail;
  clearNode(elements.sessionDetail);
  if (!detail) {
    elements.sessionDetail.textContent = "请选择一个会话。";
    elements.sessionDetail.classList.add("muted");
    return;
  }
  elements.sessionDetail.classList.remove("muted");
  const summary = document.createElement("div");
  summary.textContent = `${detail.model} · ${detail.start_reason} · ${detail.turn_count} 轮`;
  elements.sessionDetail.append(summary);
  if (detail.handoff && Object.keys(detail.handoff).length) {
    const handoff = document.createElement("div");
    handoff.className = "item-subtitle";
    handoff.textContent = `交接：${truncate(detail.handoff.topic || JSON.stringify(detail.handoff), 180)}`;
    elements.sessionDetail.append(handoff);
  }
  if (Array.isArray(detail.anchors) && detail.anchors.length) {
    const anchors = document.createElement("div");
    anchors.className = "anchor-list";
    for (const item of detail.anchors) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "anchor";
      button.textContent = `${item.label} ×`;
      button.title = "取消固定";
      button.addEventListener("click", async () => {
        try {
          await request(
            `/scopes/${encodeURIComponent(state.scope.scope_ref)}/events/${encodeURIComponent(item.event_ref)}/pin`,
            { method: "DELETE" },
          );
          await selectSession(state.session);
        } catch (error) {
          showError(error);
        }
      });
      anchors.append(button);
    }
    elements.sessionDetail.append(anchors);
  }
}

function renderTurns() {
  elements.turnCount.textContent = String(state.turns.length);
  clearNode(elements.turnList, state.turns.length ? "" : "暂无轮次");
  for (const turn of state.turns) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `turn-item${state.turn?.turn_ref === turn.turn_ref ? " active" : ""}`;
    button.append(itemRow(`#${turn.sequence} · ${turn.user_name || turn.user_id}`, turn.status));
    const text = document.createElement("div");
    text.className = "turn-text";
    text.textContent = turn.final_text || "无确认文本输出";
    const meta = document.createElement("div");
    meta.className = "meta-row";
    meta.append(
      document.createTextNode(formatDate(turn.created_at)),
      document.createTextNode(turn.fresh_context ? "本轮忽略前文" : "继承会话上下文"),
    );
    button.append(text, meta);
    button.addEventListener("click", () => selectTurn(turn).catch(showError));
    elements.turnList.append(button);
  }
}

async function selectTurn(turn) {
  state.turn = turn;
  renderTurns();
  const [eventsPayload, contextPayload] = await Promise.all([
    request(`/turns/${encodeURIComponent(turn.turn_ref)}/events`),
    request(`/turns/${encodeURIComponent(turn.turn_ref)}/context`),
  ]);
  state.events = eventsPayload.items || [];
  renderEvents();
  elements.contextOutput.textContent = JSON.stringify(contextPayload.item, null, 2);
}

function renderEvents() {
  clearNode(elements.eventList, state.events.length ? "" : "该轮次没有事件");
  for (const event of state.events) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "event-item";
    const title = event.tool ? `${event.sequence}. ${event.tool}` : `${event.sequence}. ${event.event_type}`;
    button.append(itemRow(title, event.status || event.effect || "recorded"));
    const body = document.createElement("div");
    body.className = "event-body";
    body.textContent = [
      event.attempt ? `attempt ${event.attempt}` : "",
      event.effect || "",
      event.duration_ms ? `${event.duration_ms} ms` : "",
      event.payload_keys?.length ? event.payload_keys.join(", ") : "",
    ].filter(Boolean).join(" · ");
    button.append(body);
    button.addEventListener("click", () => openPayload(event));
    elements.eventList.append(button);
  }
}

function openPayload(event) {
  state.event = event;
  state.payloadOffset = 0;
  state.payloadNextOffset = null;
  elements.payloadTitle.textContent = event.tool || event.event_type;
  elements.payloadPath.value = "";
  elements.payloadOutput.textContent = "选择字段路径，或直接读取完整事件。";
  elements.payloadNext.disabled = true;
  elements.pinEvent.disabled = !state.scope;
  elements.payloadDialog.showModal();
}

async function loadPayload(offset = 0) {
  if (!state.event) return;
  const path = elements.payloadPath.value.trim();
  const query = new URLSearchParams({ path, offset: String(offset), limit: "16000" });
  const payload = await request(`/events/${encodeURIComponent(state.event.event_ref)}/payload?${query}`);
  const item = payload.item;
  state.payloadPath = path;
  state.payloadOffset = item.offset || 0;
  state.payloadNextOffset = item.next_offset;
  elements.payloadOutput.textContent = typeof item.data === "string"
    ? item.data
    : JSON.stringify(item, null, 2);
  elements.payloadNext.disabled = item.next_offset === null || item.next_offset === undefined;
}

function showConfirm({ title, message, dangerous = false, requireToken = false, action }) {
  state.confirmAction = action;
  elements.confirmTitle.textContent = title;
  elements.confirmMessage.textContent = message;
  elements.confirmationField.classList.toggle("hidden", !requireToken);
  elements.confirmationInput.value = "";
  elements.confirmAction.className = `button ${dangerous ? "danger" : "primary"}`;
  elements.confirmDialog.showModal();
}

async function runConfirmedAction() {
  if (!state.confirmAction) return;
  const action = state.confirmAction;
  state.confirmAction = null;
  elements.confirmDialog.close();
  setStatus("正在执行…");
  await action();
  setStatus("操作完成", "success");
}

function showError(error) {
  console.error(error);
  setStatus(error instanceof Error ? error.message : String(error), "error");
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelectorAll(".tab-view").forEach((view) => {
      view.classList.toggle("active", view.id === `${tab.dataset.tab}-view`);
    });
  });
}

elements.scopeSelect.addEventListener("change", () => {
  state.scope = state.scopes.find((item) => item.scope_ref === elements.scopeSelect.value) || null;
  state.session = null;
  renderScope();
  loadSessions().catch(showError);
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
    action: async () => {
      if (elements.confirmationInput.value !== "CONFIRM") throw new Error("必须输入 CONFIRM");
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
  const label = `${state.event.tool || state.event.event_type} · ${formatDate(state.event.created_at)}`;
  try {
    await request(
      `/scopes/${encodeURIComponent(state.scope.scope_ref)}/events/${encodeURIComponent(state.event.event_ref)}/pin`,
      { method: "POST", body: JSON.stringify({ label }) },
    );
    elements.pinEvent.textContent = "已固定";
    elements.pinEvent.disabled = true;
    await selectSession(state.session);
  } catch (error) {
    showError(error);
  }
});

elements.confirmAction.addEventListener("click", () => runConfirmedAction().catch(showError));

loadScopes().catch(showError);
