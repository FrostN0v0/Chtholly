"use strict";

(function defineSessionTree(global) {
  function create(hooks) {
    const { node, button, date, label, personaName, selectSession } = hooks;
    function render(target, data) {
      const focused = target.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      const scroll = target.scrollTop;
      target.replaceChildren();
      if (!data.scope) { target.append(node("p", "empty", "\u8bf7\u9009\u62e9\u9891\u9053")); return; }
      const sessions = data.sessions.filter((session) => (session.purpose || "chat") === "chat" && !session.reminder_task_ref)
        .sort((a, b) => data.newestFirst ? b.sequence - a.sequence : a.sequence - b.sequence);
      for (const session of sessions) {
        const item = button("", () => selectSession(session), "list-item session-entry");
        item.dataset.focusKey = `session:${session.session_ref}`;
        item.dataset.recordKey = session.session_ref;
        const selected = session.session_ref === data.session?.session_ref;
        item.classList.toggle("is-active", selected);
        item.setAttribute("aria-current", String(selected));
        const heading = node("div", "item-heading");
        heading.append(node("strong", "", `#${session.sequence} \u00b7 ${personaName(session.persona)}`), node("span", "badge", label(session.status)));
        item.append(heading, node("p", "muted", date(session.created_at)), node("span", "muted", `${session.turn_count ?? "?"} \u8f6e\u5bf9\u8bdd`));
        target.append(item);
      }
      if (!sessions.length) target.append(node("p", "empty", "\u6682\u65e0\u666e\u901a\u4f1a\u8bdd"));
      if (focused) [...target.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focused)?.focus({ preventScroll: true });
      target.scrollTop = scroll;
    }
    return Object.freeze({ render });
  }
  function createReviewArchive(hooks) {
    const { node, button, disclosure, badge, date, request, loadReview, loadBundle, renderReview, showError } = hooks;
    let scopeRef = "";
    let target = null;
    let root = null;
    let listing = null;
    let controls = null;
    let controller = new AbortController();
    let version = 0;
    let busy = false;
    let loaded = false;
    let cursor = "";
    let next = null;
    let previous = [];
    let items = [];
    let error = "";
    const rows = new Map();

    function cancel() {
      ++version; controller.abort(); controller = new AbortController(); busy = false;
      for (const row of rows.values()) row.pending = false;
      renderControls();
    }
    async function expand(row) {
      if (row.pending || row.loaded) return;
      const generation = version;
      const signal = controller.signal;
      row.pending = true;
      row.body.replaceChildren(node("p", "muted", "\u6b63\u5728\u8bfb\u53d6\u5386\u53f2\u8bb0\u5f55\u2026"));
      try {
        const review = await loadReview(scopeRef, row.item.review_ref, signal);
        if (generation !== version) return;
        row.review = review;
        row.body.replaceChildren(renderReview(review, null));
        if (review.native_turn?.turn_ref) {
          const bundle = await loadBundle(review.native_turn.turn_ref, signal);
          if (generation !== version) return;
          row.body.replaceChildren(renderReview(review, null, bundle));
        }
        row.loaded = true;
      } catch (error) {
        if (generation === version && error?.name !== "AbortError") row.body.append(node("p", "error", error.message), button("\u91cd\u8bd5", () => expand(row)));
      } finally { if (generation === version) row.pending = false; }
    }
    function renderControls() {
      if (!controls) return;
      const focus = controls.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      controls.replaceChildren();
      const before = button("\u4e0a\u4e00\u9875", () => load(previous.at(-1), "previous"));
      const after = button("\u4e0b\u4e00\u9875", () => load(next, "next"));
      const refresh = button("\u5237\u65b0\u5386\u53f2", () => load(cursor));
      before.dataset.focusKey = "archive:previous"; after.dataset.focusKey = "archive:next"; refresh.dataset.focusKey = "archive:refresh";
      before.disabled = busy || !previous.length; after.disabled = busy || !next; refresh.disabled = busy;
      controls.append(before, node("span", "muted", `\u7b2c ${previous.length + 1} \u9875`), after, refresh);
      if (error) controls.append(node("p", "error", error));
      if (focus) [...controls.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focus)?.focus({ preventScroll: true });
    }
    function renderItems() {
      listing.replaceChildren();
      for (const item of items) {
        let row = rows.get(item.review_ref);
        if (!row) {
          const root = node("details", "review-history-item");
          root.dataset.recordKey = item.review_ref;
          const heading = node("summary");
          heading.dataset.focusKey = `archive:${item.review_ref}`;
          heading.append(node("strong", "", item.decision_summary || item.reason || "\u672a\u5f52\u5c5e\u5386\u53f2\u590d\u76d8"), badge(item.status), node("span", "muted", date(item.started_at)));
          const body = node("div", "review-history-detail");
          row = { item, root, body, pending: false, loaded: false };
          root.append(heading, body);
          root.addEventListener("toggle", () => { if (root.open) expand(row).catch(showError); });
          rows.set(item.review_ref, row);
        }
        listing.append(row.root);
      }
      if (!items.length) listing.append(node("p", "archive-state", busy ? "\u6b63\u5728\u8bfb\u53d6\u2026" : "\u672c\u9875\u65e0\u672a\u5f52\u5c5e\u8bb0\u5f55"));
    }
    async function load(value = cursor, direction = "refresh") {
      if (!scopeRef || busy || document.hidden) return;
      const generation = version;
      const scope = scopeRef;
      busy = true; error = ""; renderControls();
      try {
        const query = new URLSearchParams({ limit: "30", ...(value ? { cursor: value } : {}) });
        const result = await request(`/scopes/${encodeURIComponent(scope)}/companion/reviews?${query}`, { signal: controller.signal });
        if (generation !== version || scope !== scopeRef) return;
        if (direction === "next") previous.push(cursor);
        else if (direction === "previous") previous.pop();
        cursor = value || "";
        next = result.next_cursor || null;
        items = (result.items || []).filter((item) => item.activity_association === "unassigned");
        loaded = true;
        renderItems();
      } catch (failure) { if (generation === version && failure?.name !== "AbortError") error = failure.message; }
      finally { if (generation === version) { busy = false; renderControls(); } }
    }
    function open(element, scope) {
      if (target !== element || scopeRef !== scope) {
        cancel(); target = element; scopeRef = scope; rows.clear();
        loaded = false; cursor = ""; next = null; previous = []; items = []; error = "";
        const panel = disclosure("\u672a\u5f52\u5c5e\u4f1a\u8bdd\u7684\u5386\u53f2\u590d\u76d8", "");
        root = panel.item;
        listing = node("div", "review-history-list"); controls = node("div", "actions review-history-controls");
        panel.content.append(listing, controls);
        root.addEventListener("toggle", () => { if (root.open && !loaded) load().catch(showError); });
        target.replaceChildren(root);
      }
      if (root.open && !loaded) load().catch(showError);
      for (const row of rows.values()) if (row.root.open) expand(row).catch(showError);
      renderControls();
    }
    return Object.freeze({ open, cancel });
  }
  global.SessionTree = Object.freeze({ create, createReviewArchive });
})(window);
