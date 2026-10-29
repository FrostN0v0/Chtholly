"use strict";

(function defineSessionCompanion(global) {
  const PURPOSES = Object.freeze({
    chat: "\u666e\u901a\u804a\u5929", reminder: "\u63d0\u9192", companion: "\u4e3b\u52a8\u8054\u7cfb", companion_reflection: "\u966a\u4f34\u53cd\u601d",
  });
  const TRIGGERS = Object.freeze({
    message: "\u6d88\u606f\u89e6\u53d1", scheduled: "\u5b9a\u65f6\u89e6\u53d1", autonomous: "\u81ea\u4e3b\u89e6\u53d1", reflection: "\u53cd\u601d\u89e6\u53d1",
  });
  const LABELS = Object.freeze({
    normal: "\u666e\u901a\u6a21\u5f0f", companion: "\u966a\u4f34\u6a21\u5f0f", active: "\u8fdb\u884c\u4e2d", completed: "\u5df2\u5b8c\u6210", committed: "\u5df2\u63d0\u4ea4",
    succeeded: "\u6210\u529f", failed: "\u5931\u8d25", cancelled: "\u5df2\u53d6\u6d88", interrupted: "\u5df2\u4e2d\u65ad", blocked: "\u5bbf\u4e3b\u963b\u6b62",
    unknown: "\u7ed3\u679c\u672a\u77e5", missing: "\u5386\u53f2\u672a\u8bb0\u5f55", not_recorded: "\u5386\u53f2\u672a\u8bb0\u5f55", not_proposed: "\u672a\u63d0\u8bae\u8054\u7cfb",
    reserved: "\u5df2\u9884\u7559\u8054\u7cfb", confirmed: "\u5df2\u786e\u8ba4\u9001\u8fbe", sent: "\u5df2\u53d1\u9001", silent: "\u4e3b\u52a8\u6c89\u9ed8", pending: "\u7b49\u5f85\u4e2d",
    delivered: "\u5df2\u786e\u8ba4\u9001\u8fbe", expired: "\u5df2\u8fc7\u671f",
    queued: "\u5df2\u6392\u5165\u4f1a\u8bdd\u673a\u4f1a",
    recorded: "\u5df2\u8bb0\u5f55", invalid: "\u8bb0\u5f55\u65e0\u6548",
    planned: "\u8ba1\u5212\u4e2d", paused: "\u5df2\u6682\u505c", open: "\u6301\u7eed\u5173\u6ce8", waiting_reply: "\u7b49\u5f85\u56de\u590d", dormant: "\u6682\u65f6\u4f11\u7720", resolved: "\u5df2\u89e3\u51b3",
    concern: "\u5173\u5207", topic: "\u8bdd\u9898", connection: "\u8054\u7cfb", check_in: "\u95ee\u5019", share: "\u5206\u4eab", continue: "\u7ee7\u7eed\u8bdd\u9898", free_chat: "\u95f2\u804a",
    focus: "\u751f\u6d3b\u91cd\u5fc3", interests: "\u5174\u8da3", plans: "\u8ba1\u5212", recent_events: "\u8fd1\u671f\u865a\u6784\u4e8b\u4ef6",
    request_timeout: "\u8bf7\u6c42\u8d85\u65f6 (\u79d2)", reflection_interval_minutes: "\u53cd\u601d\u95f4\u9694 (\u5206\u949f)", daily_reflections: "\u6bcf\u65e5\u53cd\u601d\u4e0a\u9650", daily_contacts: "\u6bcf\u65e5\u8054\u7cfb\u4e0a\u9650",
    contact_interval_minutes: "\u8054\u7cfb\u95f4\u9694 (\u5206\u949f)", member_idle_days: "\u6210\u5458\u6d3b\u8dc3\u7a97\u53e3 (\u5929)", input_chars: "\u8f93\u5165\u5b57\u7b26\u4e0a\u9650", output_tokens: "\u8f93\u51fa Token \u4e0a\u9650",
    opted_out: "\u62d2\u7edd\u4e3b\u52a8\u8054\u7cfb", quiet_start: "\u514d\u6253\u6270\u5f00\u59cb", quiet_end: "\u514d\u6253\u6270\u7ed3\u675f", timezone: "\u65f6\u533a", enabled: "\u5df2\u542f\u7528", allow_contact: "\u5141\u8bb8\u8054\u7cfb",
    daily_limit: "\u6bcf\u65e5\u9650\u5236", updated_at: "\u66f4\u65b0\u65f6\u95f4", source: "\u6765\u6e90", status: "\u72b6\u6001", reason: "\u539f\u56e0", checked_at: "\u68c0\u67e5\u65f6\u95f4",
  });
  const REASONS = Object.freeze({
    no_contact_proposed: "\u6a21\u578b\u672a\u63d0\u8bae\u8054\u7cfb", mode_changed: "\u6a21\u5f0f\u5df2\u53d8\u66f4", participant_active: "\u53c2\u4e0e\u8005\u6b63\u5728\u6d3b\u52a8", service_stopped: "\u670d\u52a1\u5df2\u505c\u6b62",
    state_not_committed: "\u53cd\u601d\u72b6\u6001\u672a\u63d0\u4ea4", account_unavailable: "\u8d26\u53f7\u4e0d\u53ef\u7528", contact_inflight_or_unknown: "\u8054\u7cfb\u53d1\u9001\u4e2d\u6216\u7ed3\u679c\u672a\u77e5",
    quiet_hours: "\u514d\u6253\u6270\u65f6\u6bb5", daily_contact_limit: "\u8fbe\u5230\u6bcf\u65e5\u8054\u7cfb\u9650\u5236", recent_user_activity: "\u7528\u6237\u521a\u521a\u6d3b\u52a8", member_inactive: "\u6210\u5458\u4e0d\u5728\u6d3b\u8dc3\u7a97\u53e3",
    contact_interval: "\u8054\u7cfb\u95f4\u9694\u672a\u5230", duplicate_contact: "\u91cd\u590d\u8054\u7cfb", review_changed: "\u53cd\u601d\u5df2\u53d8\u66f4", member_preference: "\u6210\u5458\u8054\u7cfb\u504f\u597d\u9650\u5236",
    contact_reserved: "\u901a\u8fc7\u5bbf\u4e3b\u68c0\u67e5\u5e76\u9884\u7559", contact_claim_failed: "\u8054\u7cfb\u9884\u7559\u5931\u8d25", reflection_interrupted: "\u590d\u76d8\u88ab\u4e2d\u65ad",
    dialogue_opportunity_queued: "\u5df2\u4ea4\u7ed9\u5f53\u524d\u9891\u9053\u4f1a\u8bdd\u51b3\u5b9a\u65f6\u673a",
    dialogue_opportunity_pending: "\u9891\u9053\u5df2\u6709\u5f85\u5904\u7406\u673a\u4f1a",
  });
  const missing = "\u5386\u53f2\u672a\u8bb0\u5f55";
  function purpose(value) { return PURPOSES[value] || value || "\u7528\u9014\u672a\u8bb0\u5f55"; }
  function trigger(value) { return TRIGGERS[value] || value || "\u89e6\u53d1\u672a\u8bb0\u5f55"; }
  function label(value) { return LABELS[value] || value || missing; }
  function reason(value) { return REASONS[value] ? `${REASONS[value]} (${value})` : value || missing; }
  function scalar(value) { return value == null ? missing : typeof value === "boolean" ? value ? "\u662f" : "\u5426" : String(value); }

  function create(hooks) {
    const { $, node, button, section, disclosure, metric, date, number, short, request, status, showError, eventButton, selectEvidenceTurn } = hooks;
    const root = $("companion-workspace");
    const state = {
      scope: null, active: false, overview: null, items: [], detail: null, selected: "", persona: "", filter: "",
      cursor: "", previous: [], next: null, generation: 0, detailGeneration: 0, controller: null, detailController: null,
      timer: null, pollRemaining: 0, busy: false, detailBusy: false, overviewError: "", historyError: "", detailError: "",
    };
    function badge(value) {
      const item = node("span", "badge companion-status", label(value));
      item.dataset.status = value || "missing";
      if (["failed", "cancelled", "interrupted", "blocked"].includes(value)) item.classList.add("badge-danger");
      if (["active", "pending", "queued", "reserved", "unknown"].includes(value)) item.classList.add("badge-pending");
      if (["confirmed", "succeeded", "completed", "committed", "sent"].includes(value)) item.classList.add("badge-success");
      return item;
    }
    function fields(values) {
      const item = node("dl", "fields companion-fields");
      for (const [key, value] of Object.entries(values || {})) {
        item.append(node("dt", "", label(key)), node("dd", "", value && typeof value === "object" ? JSON.stringify(value) : scalar(value)));
      }
      if (!Object.keys(values || {}).length) item.append(node("dt", "", "\u8bb0\u5f55"), node("dd", "", missing));
      return item;
    }
    function stableDetails(title, key, value, note = "") {
      const panel = disclosure(title, note);
      panel.item.dataset.detailKey = key;
      panel.content.append(SessionContent.value(value == null ? missing : value));
      return panel;
    }
    function life(value, title, key) {
      const panel = section(title, "\u89d2\u8272\u865a\u6784\u751f\u6d3b\uff0c\u4e0d\u662f\u73b0\u5b9e\u7ecf\u5386\u6216\u5e73\u53f0\u6d3b\u52a8\u3002");
      panel.dataset.recordKey = key;
      if (!value) { panel.append(node("p", "muted", missing)); return panel; }
      panel.append(node("p", "", `${label("focus")}\uff1a${value.focus || "\u5df2\u8bb0\u5f55\uff1a\u65e0"}`));
      panel.append(node("p", "", `${label("interests")}\uff1a${Array.isArray(value.interests) ? value.interests.join(" / ") || "\u65e0" : missing}`));
      for (const [field, titleText] of [["plans", "\u865a\u6784\u8ba1\u5212"], ["recent_events", "\u865a\u6784\u4e8b\u4ef6"]]) {
        const detail = disclosure(titleText, Array.isArray(value[field]) ? `${value[field].length} \u9879` : missing);
        detail.item.dataset.detailKey = `${key}:${field}`;
        if (!Array.isArray(value[field])) detail.content.append(node("p", "muted", missing));
        else if (!value[field].length) detail.content.append(node("p", "muted", "\u5df2\u8bb0\u5f55\uff1a\u65e0"));
        for (const entry of value[field] || []) {
          const row = node("div", "companion-entry");
          row.append(node("strong", "", entry.title || entry.summary || entry.key || missing));
          if (entry.status) row.append(badge(entry.status));
          if (entry.progress) row.append(node("p", "", entry.progress));
          detail.content.append(row);
        }
        panel.append(detail.item);
      }
      return panel;
    }
    function preserve(render) {
      const opened = new Set([...root.querySelectorAll("details[open]")].map((item) => detailKey(item)));
      const focused = document.activeElement;
      const focus = root.contains(focused) ? { id: focused.id, key: focused.dataset.focusKey, detail: focused.tagName === "SUMMARY" ? detailKey(focused.parentElement) : null } : null;
      const scroll = [...root.querySelectorAll(".scroll")].map((item) => [item.id, item.scrollTop, item.scrollLeft]);
      const page = { top: window.scrollY, left: window.scrollX, behavior: "instant" };
      render();
      for (const item of root.querySelectorAll("details")) {
        item.open = opened.has(detailKey(item));
        if (focus?.detail === detailKey(item)) item.querySelector("summary")?.focus({ preventScroll: true });
      }
      if (focus?.id) $(focus.id)?.focus({ preventScroll: true });
      else if (focus?.key) [...root.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focus.key)?.focus({ preventScroll: true });
      for (const [id, top, left] of scroll) { const item = $(id); if (item) { item.scrollTop = top; item.scrollLeft = left; } }
      window.scrollTo(page);
    }
    function detailKey(item) {
      const records = [];
      for (let parent = item; parent && parent !== root; parent = parent.parentElement) if (parent.dataset.recordKey) records.push(parent.dataset.recordKey);
      return [...records.reverse(), item.dataset.detailKey || item.querySelector("summary")?.textContent].join("|");
    }
    function renderOverview() {
      const target = $("companion-overview");
      target.replaceChildren();
      const data = state.overview;
      if (!data) { target.append(node("p", state.overviewError ? "error" : "empty", state.overviewError || (state.scope ? "\u6b63\u5728\u8bfb\u53d6\u966a\u4f34\u6982\u89c8\u2026" : "\u8bf7\u9009\u62e9\u804a\u5929\u8303\u56f4"))); return; }
      const overview = section("\u5f53\u524d\u966a\u4f34\u72b6\u6001", "\u4ec5\u8bfb\u89c6\u56fe\uff1b\u5f53\u524d\u72b6\u6001\u548c\u9650\u5236\u4e0d\u7528\u4e8e\u8865\u5168\u5386\u53f2\u51b3\u7b56\u3002");
      const row = node("div", "actions");
      row.append(badge(data.mode?.mode), node("span", "", data.mode?.persona_key || missing));
      if (data.mode?.recorded === false) overview.append(node("p", "muted", "\u672a\u6301\u4e45\u5316\u6a21\u5f0f\u8bb0\u5f55\uff1b\u5f53\u524d\u4f7f\u7528\u666e\u901a\u6a21\u5f0f\u9ed8\u8ba4\u7b56\u7565\u3002"));
      overview.append(row, node("p", "muted", `\u6a21\u5f0f\u7248\u672c ${number(data.mode?.revision)} / \u4e0a\u4e0b\u6587\u7248\u672c ${number(data.mode?.context_revision)} \u00b7 ${date(data.mode?.updated_at)}`), node("p", "muted", `\u4e0b\u6b21\u8ba1\u5212\u53cd\u601d\uff1a${date(data.mode?.next_review_at)}`));
      const daily = data.daily;
      const totals = node("div", "companion-metrics");
      const counts = (values) => values && typeof values === "object" ? Object.entries(values).map(([key, count]) => `${label(key)} ${number(count)}`).join(" / ") || "\u5df2\u8bb0\u5f55\uff1a0" : missing;
      totals.append(metric("\u4eca\u65e5\u53cd\u601d", counts(daily?.reviews)), metric("\u4eca\u65e5\u8054\u7cfb\u5c1d\u8bd5", counts(daily?.contacts)), metric("\u786e\u8ba4\u9001\u8fbe\u6d88\u606f", number(daily?.confirmed_messages)));
      overview.append(totals, node("p", "muted", `${daily?.day || missing} \u00b7 ${daily?.timezone || missing}`));
      const limits = disclosure("\u5f53\u524d\u8fd0\u884c\u9650\u5236", "\u5f53\u524d\u914d\u7f6e\uff0c\u975e\u5386\u53f2\u8054\u7cfb\u9650\u5236");
      limits.item.dataset.detailKey = "current-limits";
      limits.content.append(fields(data.current_limits));
      overview.append(limits.item);
      target.append(overview, life(data.life?.state, "\u5f53\u524d\u865a\u6784\u751f\u6d3b", "current-life"));
      if (data.life) target.append(node("p", "muted companion-version", `\u751f\u6d3b\u7248\u672c ${number(data.life.version)} \u00b7 ${data.life.persona_key || missing} \u00b7 ${date(data.life.updated_at)}`));
      if (data.life?.truncated) target.append(node("p", "partial-notice", "\u5f53\u524d\u751f\u6d3b\u663e\u793a\u5df2\u622a\u65ad\u3002"));
      const members = disclosure("\u5f53\u524d\u6210\u5458\u5173\u6ce8\u4e0e\u8054\u7cfb\u504f\u597d", `${data.members?.length || 0} \u4f4d\u6210\u5458`);
      members.item.dataset.detailKey = "current-members";
      if (!data.members?.length) members.content.append(node("p", "muted", "\u6682\u65e0\u5df2\u8bb0\u5f55\u6210\u5458"));
      for (const member of data.members || []) {
        const card = node("article", "companion-member");
        card.dataset.recordKey = member.member_ref;
        card.append(node("h4", "", member.display_name || member.member_ref), node("p", "muted", `\u6700\u8fd1\u7528\u6237\u6d3b\u52a8 ${date(member.last_user_at)} \u00b7 \u6700\u8fd1\u8054\u7cfb ${date(member.last_contact_at)}`));
        const preferences = disclosure("\u5f53\u524d\u5b9e\u9645\u8054\u7cfb\u504f\u597d");
        preferences.item.dataset.detailKey = "preference";
        preferences.content.append(fields(member.preference));
        card.append(preferences.item);
        for (const attention of member.attention || []) {
          const value = attention.state;
          const row = node("div", "companion-entry");
          row.append(node("strong", "", label(value?.kind)), badge(value?.status), node("p", "", value?.summary || missing), node("p", "muted", `\u7248\u672c ${number(attention.version)} \u00b7 ${date(attention.updated_at)}`));
          card.append(row);
        }
        if (!member.attention?.length) card.append(node("p", "muted", "\u5df2\u8bb0\u5f55\uff1a\u65e0\u5173\u6ce8\u9879"));
        if (member.attention_truncated || member.attention?.some((item) => item.truncated)) card.append(node("p", "partial-notice", "\u5173\u6ce8\u9879\u663e\u793a\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u8bb0\u5f55\u3002"));
        members.content.append(card);
      }
      if (data.members_truncated) members.content.append(node("p", "partial-notice", "\u6210\u5458\u5217\u8868\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u6210\u5458\u3002"));
      target.append(members.item);
    }
    function renderFilters() {
      const select = $("companion-persona-filter");
      select.replaceChildren(node("option", "", "\u5168\u90e8\u4eba\u683c"));
      select.firstElementChild.value = "";
      const personas = new Set([...(state.overview?.personas || []), ...state.items.map((item) => item.persona_key), state.persona].filter(Boolean));
      for (const key of personas) { const item = node("option", "", key); item.value = key; select.append(item); }
      select.value = state.persona;
      $("companion-status-filter").value = state.filter;
    }
    function proposalStatus(item) { return item.contact_proposed === true ? "\u63d0\u8bae\u8054\u7cfb" : item.contact_proposed === false ? label("not_proposed") : "\u63d0\u8bae\u672a\u8bb0\u5f55"; }
    function renderList() {
      const target = $("companion-review-list");
      target.replaceChildren();
      $("companion-review-count").textContent = String(state.items.length);
      $("companion-previous-button").disabled = state.busy || !state.previous.length;
      $("companion-next-button").disabled = state.busy || !state.next;
      $("companion-page-status").textContent = `\u7b2c ${state.previous.length + 1} \u9875`;
      if (state.historyError) target.append(node("p", "error", state.historyError));
      if (!state.items.length) target.append(node("p", "empty", state.busy ? "\u6b63\u5728\u8bfb\u53d6\u53cd\u601d\u2026" : "\u5f53\u524d\u7b5b\u9009\u65e0\u53cd\u601d\u8bb0\u5f55"));
      for (const review of state.items) {
        const item = button("", () => selectReview(review.review_ref), "list-item companion-review");
        item.dataset.focusKey = `review:${review.review_ref}`;
        item.setAttribute("aria-current", String(state.selected === review.review_ref));
        item.classList.toggle("is-active", state.selected === review.review_ref);
        const head = node("div", "item-heading");
        head.append(node("strong", "", review.persona_key || missing), badge(review.status));
        item.append(head, node("p", "snippet", review.contact?.topic || reason(review.decision?.reason || review.reason)), node("p", "muted", proposalStatus(review)));
        const gate = node("div", "actions companion-review-gate");
        gate.append(node("span", "muted", "\u5bbf\u4e3b\u51b3\u7b56"), badge(review.decision?.status || "missing"));
        item.append(gate, node("p", "muted", `${date(review.started_at)} \u00b7 ${review.model || "\u6a21\u578b\u672a\u8bb0\u5f55"}`));
        const count = review.contacts?.some((contact) => contact.confirmed_count == null) ? null
          : (review.contacts || []).reduce((sum, contact) => sum + contact.confirmed_count, 0);
        item.append(node("p", "muted", `\u8054\u7cfb\u5c1d\u8bd5 ${review.contacts?.length ?? "?"} \u00b7 \u5df2\u663e\u793a\u786e\u8ba4\u6d88\u606f ${number(count)} \u00b7 Token ${number(review.usage?.total_tokens)}`));
        if (review.audit_truncated || review.contacts_truncated) item.append(node("p", "partial-notice", "\u8bb0\u5f55\u5df2\u622a\u65ad\uff0c\u7edf\u8ba1\u53ef\u80fd\u4e0d\u5b8c\u6574"));
        if (review.contacts?.some((contact) => contact.status === "unknown")) item.append(badge("unknown"));
        target.append(item);
      }
    }
    function navigationButton(evidence, title, key) {
      const item = button(title, () => selectEvidenceTurn(evidence));
      item.dataset.focusKey = key;
      item.disabled = !evidence?.turn_ref || !evidence?.session_ref;
      if (item.disabled) item.title = missing;
      return item;
    }
    function contactCard(contact) {
      const panel = section("\u5b9e\u9645\u8054\u7cfb\u5c1d\u8bd5");
      panel.dataset.recordKey = contact.attempt_ref;
      const row = node("div", "actions");
      const destination = contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : contact.display_name || missing;
      row.append(node("strong", "", destination), badge(contact.status));
      const subject = contact.target === "channel" ? "\u9891\u9053\u8bdd\u9898" : contact.member_ref || missing;
      panel.append(row, node("p", "muted", `${subject} \u00b7 \u8054\u7cfb\u5f00\u59cb ${date(contact.created_at)} \u00b7 \u7ed3\u675f ${date(contact.finished_at)}`), node("p", "", reason(contact.reason)), metric("\u786e\u8ba4\u9001\u8fbe\u6d88\u606f", number(contact.confirmed_count)));
      if (contact.status === "unknown") panel.append(node("p", "partial-notice", "\u53d1\u9001\u7ed3\u679c\u672a\u77e5\uff1b\u4e0d\u7b49\u4e8e\u672a\u53d1\u9001\uff0c\u4e0d\u63a8\u65ad\u9001\u8fbe\u3002"));
      const transport = stableDetails("\u4f20\u8f93\u4e0e\u6536\u636e\u6355\u83b7", "contact-transport", {
        transport_started_at: contact.transport_started_at, transport_inflight: contact.transport_inflight,
        previous_contact_at: contact.previous_contact_at, receipt_capture: contact.receipt_capture,
      }, "\u53ea\u663e\u793a\u5386\u53f2\u5ba1\u8ba1\uff0c\u4e0d\u4ee5\u5f53\u524d\u72b6\u6001\u8865\u5168");
      panel.append(navigationButton(contact.native_turn, "\u67e5\u770b\u8054\u7cfb\u8f6e\u6b21\u4e0e\u9001\u8fbe\u8bc1\u636e", `contact:${contact.attempt_ref}:turn`));
      const more = stableDetails("\u5f53\u65f6\u8054\u7cfb\u63d0\u8bae\u4e0e\u9650\u5236", "contact-facts", { proposal: contact.proposal, limits: contact.limits }, "\u5386\u53f2\u5feb\u7167\uff0c\u975e\u5f53\u524d\u914d\u7f6e");
      panel.append(transport.item, more.item);
      return panel;
    }
    function renderDetail() {
      const target = $("companion-review-detail");
      target.replaceChildren();
      if (!state.detail) { target.append(node("p", state.detailError ? "error" : "empty", state.detailError || (state.detailBusy ? "\u6b63\u5728\u8bfb\u53d6\u53cd\u601d\u8be6\u60c5\u2026" : "\u8bf7\u9009\u62e9\u4e00\u6b21\u53cd\u601d"))); return; }
      const item = state.detail;
      const overview = section("\u53cd\u601d\u51b3\u7b56\u4e0e\u7ed3\u679c", "\u6a21\u578b\u63d0\u8bae\u3001\u5bbf\u4e3b\u95e8\u7981\u548c\u5b9e\u9645\u9001\u8fbe\u662f\u4e09\u4e2a\u4e0d\u540c\u9636\u6bb5\u3002");
      overview.dataset.recordKey = item.review_ref;
      const heading = node("div", "item-heading");
      heading.append(node("h3", "", item.persona_key || missing), badge(item.status));
      overview.append(heading);
      if (item.reason || item.status !== "completed") overview.append(node("p", "", reason(item.reason)));
      overview.append(node("p", "muted", `${date(item.started_at)} \u2192 ${date(item.finished_at)} \u00b7 ${hooks.duration(item.duration_ms)}`));
      const stages = node("div", "companion-stages");
      stages.append(metric("1. \u6a21\u578b\u63d0\u8bae", proposalStatus(item)), metric("2. \u5bbf\u4e3b\u51b3\u7b56", label(item.decision?.status || "missing")), metric("3. \u5b9e\u9645\u8054\u7cfb\u5c1d\u8bd5", number(item.contacts?.length)));
      overview.append(stages);
      const application = item.changes?.committed === true ? "\u5df2\u5e94\u7528" : item.transaction_committed === true ? "\u4e8b\u52a1\u5df2\u63d0\u4ea4\uff0c\u53d8\u66f4\u5feb\u7167\u4e0d\u53ef\u7528" : "\u672a\u5e94\u7528";
      overview.append(node("p", "muted", `\u6570\u636e\u5e93\u4e8b\u52a1\uff1a${item.transaction_committed === true ? "\u5df2\u63d0\u4ea4" : item.transaction_committed === false ? "\u672a\u63d0\u4ea4" : "\u5386\u53f2\u672a\u8bb0\u5f55"} \u00b7 \u5feb\u7167\u53d8\u66f4\uff1a${application}`));
      if (item.contact) {
        const destination = item.contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : item.contact.display_name || item.contact.member_ref || missing;
        overview.append(node("p", "", `${destination} \u00b7 ${label(item.contact.purpose)}\uff1a${item.contact.topic || missing}`), node("p", "", `\u7ed3\u6784\u5316\u6a21\u578b\u7406\u7531\uff1a${item.contact.reason || missing}`));
      }
      overview.append(node("p", "muted", "\u663e\u793a\u7684\u7406\u7531\u6765\u81ea\u7ed3\u6784\u5316\u6a21\u578b\u8f93\u51fa\uff0c\u4e0d\u662f\u9690\u85cf\u601d\u7ef4\u94fe\u3002"));
      const decisionSummary = item.decision_summary ?? item.proposal?.decision_summary;
      overview.append(node("p", "companion-decision-summary", `\u590d\u76d8\u7ed3\u8bba\uff1a${decisionSummary || "\u590d\u76d8\u7ed3\u8bba\u672a\u8bb0\u5f55"}`));
      if (item.decision) overview.append(node("p", "", `\u5bbf\u4e3b\u539f\u56e0\uff1a${reason(item.decision.reason)}`), node("p", "muted", `${date(item.decision.checked_at)} \u00b7 ${item.decision.source || missing}`));
      else overview.append(node("p", "partial-notice", "\u5bbf\u4e3b\u51b3\u7b56\u5386\u53f2\u672a\u8bb0\u5f55\uff1b\u4e0d\u6839\u636e\u65e0\u53d1\u9001\u63a8\u65ad\u514d\u6253\u6270\u6216\u5176\u4ed6\u539f\u56e0\u3002"));
      const audit = node("div", "actions");
      audit.append(navigationButton(item.native_turn, "\u67e5\u770b\u53cd\u601d\u8f6e\u6b21", `review:${item.review_ref}:turn`));
      for (const [field, title] of [["state_event_ref", "\u539f\u59cb\u8f93\u5165"], ["proposal_event_ref", "\u539f\u59cb\u63d0\u8bae"], ["decision_event_ref", "\u5bbf\u4e3b\u51b3\u7b56"], ["context_event_ref", "\u5f53\u65f6\u4e0a\u4e0b\u6587"]]) audit.append(eventButton(item.events?.[field], title));
      overview.append(audit, node("p", "muted", `\u8f93\u5165\u6355\u83b7 ${label(item.capture?.state)} \u00b7 \u63d0\u8bae\u6355\u83b7 ${label(item.capture?.proposal)} \u00b7 \u51b3\u7b56\u6355\u83b7 ${label(item.capture?.decision)}`));
      if (item.audit_truncated || item.contacts_truncated || Object.values(item.capture?.truncated || {}).some(Boolean)) overview.append(node("p", "partial-notice", "\u672c\u6b21\u663e\u793a\u5df2\u622a\u65ad\uff1b\u53ef\u6309\u9700\u67e5\u770b\u539f\u59cb\u8bb0\u5f55\u3002"));
      target.append(overview);
      const usage = disclosure("\u53cd\u601d\u6a21\u578b\u4e0e\u7528\u91cf", item.model || "\u5386\u53f2\u6a21\u578b\u672a\u8bb0\u5f55");
      usage.item.dataset.detailKey = "review-usage";
      usage.content.append(SessionCharts.usage(item.usage));
      target.append(usage.item);
      const changes = item.changes;
      const changePanel = disclosure("\u751f\u6210\u53d8\u5316\u4e0e\u4e8b\u52a1\u63d0\u4ea4", changes?.committed === true ? "\u5df2\u63d0\u4ea4\u5230\u6301\u4e45\u72b6\u6001" : "\u672a\u63d0\u4ea4\uff1b\u751f\u6210\u63d0\u8bae\u4e0d\u7b49\u4e8e\u5df2\u5e94\u7528");
      changePanel.item.dataset.detailKey = "review-changes";
      changePanel.content.append(node("p", "muted", `\u8f93\u5165\u751f\u6d3b\u7248\u672c ${number(item.life_version)} \u00b7 \u5df2\u63d0\u4ea4\u7248\u672c ${number(item.committed_life_version)}`));
      const comparison = node("div", "companion-comparison");
      comparison.append(life(changes?.life_before, "\u53cd\u601d\u524d\u865a\u6784\u751f\u6d3b", "life-before"), life(changes?.life_proposed, "\u6a21\u578b\u63d0\u8bae\u7684\u865a\u6784\u751f\u6d3b", "life-proposed"));
      changePanel.content.append(comparison);
      changePanel.content.append(stableDetails("\u5173\u6ce8\u9879\uff1a\u53cd\u601d\u524d / \u6a21\u578b\u63d0\u8bae", "attention-changes", { before: changes?.attention_before, proposed: changes?.attention_proposed }).item);
      target.append(changePanel.item);
      const input = stableDetails("\u5f53\u65f6\u53cd\u601d\u8f93\u5165\u72b6\u6001", "review-input", item.state, "\u53ea\u4f7f\u7528\u5f53\u6b21\u5df2\u6355\u83b7\u7684\u8f93\u5165\uff0c\u4e0d\u4ee5\u5f53\u524d\u72b6\u6001\u66ff\u4ee3");
      const proposal = stableDetails("\u7ed3\u6784\u5316\u6a21\u578b\u63d0\u8bae", "review-proposal", item.proposal, "\u4e0d\u662f\u9690\u85cf\u601d\u7ef4\u94fe");
      target.append(input.item, proposal.item);
      const evidence = disclosure("\u5df2\u9a8c\u8bc1\u7684\u804a\u5929\u8bc1\u636e", `${item.evidence_turns?.length || 0} \u8f6e`);
      evidence.item.dataset.detailKey = "review-evidence";
      for (const turn of item.evidence_turns || []) evidence.content.append(navigationButton(turn, `\u67e5\u770b\u8bc1\u636e #${turn.turn_id} / \u8f6e\u6b21 ${turn.sequence ?? "?"}`, `review:${item.review_ref}:evidence:${turn.turn_id}`));
      if (!item.evidence_turns?.length) evidence.content.append(node("p", "muted", "\u65e0\u53ef\u7528\u8bc1\u636e\u5f15\u7528"));
      target.append(evidence.item);
      if (!item.contacts?.length) target.append(section("\u5b9e\u9645\u8054\u7cfb", "\u672c\u6b21\u53cd\u601d\u65e0\u5df2\u8bb0\u5f55\u8054\u7cfb\u5c1d\u8bd5\uff1b\u8fd9\u4e0d\u8865\u5168\u7f3a\u5931\u7684\u5bbf\u4e3b\u51b3\u7b56\u3002"));
      for (const contact of item.contacts || []) target.append(contactCard(contact));
    }
    function render() { renderOverview(); renderFilters(); renderList(); renderDetail(); }
    function stopPolling() { if (state.timer !== null) clearTimeout(state.timer); state.timer = null; $("companion-poll-status").textContent = ""; }
    function startPolling() {
      stopPolling();
      if (!state.active || !hooks.isPageActive() || document.hidden || !state.scope || state.busy) return;
      if (state.pollRemaining <= 0) {
        $("companion-poll-status").textContent = "\u81ea\u52a8\u5237\u65b0\u5df2\u6682\u505c\uff0c\u53ef\u624b\u52a8\u5237\u65b0";
        return;
      }
      const active = state.items.some((item) => ["active", "pending", "running"].includes(item.status)) || state.detail?.contacts?.some((item) => item.status === "active");
      const due = state.overview?.mode?.mode === "companion" ? Date.parse(state.overview.mode.next_review_at) : NaN;
      if (!active && !Number.isFinite(due)) return;
      const delay = active ? 5000 : Math.max(10000, Math.min(86400000, due - Date.now() + 3000));
      $("companion-poll-status").textContent = active ? "\u53ef\u89c1\u65f6\u6709\u754c\u81ea\u52a8\u5237\u65b0" : "\u7b49\u5f85\u4e0b\u6b21\u590d\u76d8\u8bb0\u5f55";
      state.timer = setTimeout(() => { --state.pollRemaining; refresh({ poll: true }).catch(showError); }, delay);
    }
    function cancel() {
      stopPolling(); ++state.generation; ++state.detailGeneration;
      state.controller?.abort(); state.detailController?.abort(); state.busy = false; state.detailBusy = false;
    }
    function setMobileView(view) {
      root.dataset.mobileView = view;
      $("companion-overview-button").setAttribute("aria-pressed", String(view === "overview"));
      $("companion-history-button").setAttribute("aria-pressed", String(view === "list"));
    }
    function setScope(scope) {
      if (state.scope?.scope_ref === scope?.scope_ref) { state.scope = scope; return; }
      cancel(); state.scope = scope; state.overview = null; state.items = []; state.detail = null; state.selected = "";
      state.persona = ""; state.filter = ""; state.cursor = ""; state.previous = []; state.next = null;
      state.overviewError = ""; state.historyError = ""; state.detailError = "";
      setMobileView("list");
      render();
    }
    function path(suffix = "") { return `/scopes/${encodeURIComponent(state.scope.scope_ref)}/companion${suffix}`; }
    async function readDetail(ref, signal) { return (await request(path(`/reviews/${encodeURIComponent(ref)}`), { signal })).item; }
    async function refresh({ poll = false } = {}) {
      if (!state.active || !state.scope || !hooks.isPageActive()) return;
      cancel(); const generation = state.generation; const selected = state.selected;
      state.busy = true; state.controller = new AbortController();
      if (!poll) state.pollRemaining = 20;
      const query = new URLSearchParams({ limit: "30" });
      if (state.cursor) query.set("cursor", state.cursor);
      if (state.persona) query.set("persona_key", state.persona);
      if (state.filter) query.set("status", state.filter);
      const signal = state.controller.signal;
      if (!poll) status("\u6b63\u5728\u540c\u6b65\u966a\u4f34\u8bb0\u5f55\u2026");
      try {
        const [overview, history, detail] = await Promise.allSettled([
          request(path(), { signal }), request(path(`/reviews?${query}`), { signal }), selected ? readDetail(selected, signal) : Promise.resolve(null),
        ]);
        if (generation !== state.generation || !state.active || signal.aborted) return;
        state.overviewError = overview.status === "rejected" ? overview.reason.message : "";
        state.historyError = history.status === "rejected" ? history.reason.message : "";
        if (state.selected === selected) state.detailError = detail.status === "rejected" ? detail.reason.message : "";
        if (overview.status === "fulfilled") state.overview = overview.value.item;
        if (history.status === "fulfilled") { state.items = history.value.items || []; state.next = history.value.next_cursor || null; }
        if (selected && state.selected === selected) state.detail = detail.status === "fulfilled" ? detail.value : null;
        state.busy = false;
        preserve(render);
        if (!poll) status(state.overviewError || state.historyError || state.detailError || "\u966a\u4f34\u8bb0\u5f55\u5df2\u540c\u6b65", Boolean(state.overviewError || state.historyError || state.detailError));
      } finally { if (generation === state.generation) { state.busy = false; startPolling(); } }
    }
    async function selectReview(ref) {
      if (!state.active || !state.scope) return;
      ++state.detailGeneration; state.detailController?.abort();
      const generation = state.detailGeneration;
      state.selected = ref; state.detail = null; state.detailError = ""; state.detailBusy = true; state.detailController = new AbortController();
      setMobileView("detail");
      $("companion-detail-scroll").scrollTop = 0;
      renderList(); renderDetail();
      try {
        const detail = await readDetail(ref, state.detailController.signal);
        if (generation !== state.detailGeneration || state.selected !== ref || !state.active) return;
        state.detail = detail; state.detailBusy = false;
        preserve(renderDetail);
      } catch (error) {
        if (generation !== state.detailGeneration || error?.name === "AbortError") return;
        state.detailError = error.message; state.detailBusy = false; renderDetail();
      } finally { if (generation === state.detailGeneration) { state.detailBusy = false; startPolling(); } }
    }
    function resetFilter() {
      cancel(); state.persona = $("companion-persona-filter").value; state.filter = $("companion-status-filter").value;
      state.cursor = ""; state.previous = []; state.next = null; state.selected = ""; state.detail = null; state.items = [];
      setMobileView("list"); renderList(); renderDetail(); refresh().catch(showError);
    }
    $("companion-persona-filter").addEventListener("change", resetFilter);
    $("companion-status-filter").addEventListener("change", resetFilter);
    $("companion-next-button").addEventListener("click", () => {
      if (!state.next || state.busy) return;
      state.previous.push(state.cursor); state.cursor = state.next; refresh().catch(showError);
      $("companion-review-list").scrollTop = 0;
    });
    $("companion-previous-button").addEventListener("click", () => {
      if (!state.previous.length || state.busy) return;
      state.cursor = state.previous.pop(); refresh().catch(showError); $("companion-review-list").scrollTop = 0;
    });
    $("companion-back-button").addEventListener("click", () => {
      setMobileView("list");
      $("companion-review-list").querySelector("[aria-current=true]")?.focus({ preventScroll: true });
    });
    $("companion-overview-button").addEventListener("click", () => setMobileView("overview"));
    $("companion-history-button").addEventListener("click", () => setMobileView("list"));
    $("companion-collapse-button").addEventListener("click", () => { for (const detail of root.querySelectorAll("details[open]")) detail.open = false; });
    render();
    return Object.freeze({
      setScope, refresh, visibility() { if (document.hidden) cancel(); else startPolling(); },
      suspend() { state.active = false; cancel(); },
      activate(scope) { state.active = true; setScope(scope); return refresh(); },
    });
  }

  global.SessionCompanion = Object.freeze({ create, purpose, trigger });
})(window);
