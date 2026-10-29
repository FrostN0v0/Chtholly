"use strict";

(function defineSessionTree(global) {
  const PURPOSES = ["chat", "companion", "reminder", "companion_reflection"];

  function create(hooks) {
    const { node, button, date, label, purposeLabel, personaName, request, showError, renderTurn, selectSession, selectReview } = hooks;
    let current = null;
    let root = null;
    let previousSession = "";
    let previousPurpose = "";
    let scopeRef = "";
    let generation = 0;
    let controller = null;
    let unlinked = { items: [], cursor: "", previous: [], next: null, loaded: false, busy: false, error: "" };

    function cancel() {
      ++generation;
      controller?.abort();
      controller = null;
      unlinked.busy = false;
      renderUnlinked();
    }
    async function readUnlinked(cursor = unlinked.cursor, direction = "refresh") {
      if (!scopeRef || unlinked.busy) return;
      cancel();
      const version = generation;
      const activeScope = scopeRef;
      controller = new AbortController();
      unlinked.busy = true;
      unlinked.error = "";
      renderUnlinked();
      try {
        const query = new URLSearchParams({ limit: "30", cursor });
        const page = await request(`/scopes/${encodeURIComponent(activeScope)}/companion/reviews?${query}`, { signal: controller.signal });
        if (version !== generation || activeScope !== scopeRef) return;
        if (direction === "next") unlinked.previous.push(unlinked.cursor);
        if (direction === "previous") unlinked.previous.pop();
        unlinked.cursor = cursor;
        unlinked.items = (page.items || []).filter((item) => !item.native_turn);
        unlinked.next = page.next_cursor || null;
        unlinked.loaded = true;
      } catch (error) {
        if (version !== generation || error?.name === "AbortError") return;
        unlinked.error = error.message;
      } finally {
        if (version === generation) { unlinked.busy = false; renderUnlinked(); }
      }
    }
    function renderUnlinked() {
      const target = root?.querySelector(".unlinked-reviews-body");
      if (!target) return;
      target.replaceChildren();
      if (unlinked.error) target.append(node("p", "error", unlinked.error));
      if (!unlinked.loaded) target.append(node("p", "muted", unlinked.busy ? "\u6b63\u5728\u8bfb\u53d6\u2026" : "\u8bfb\u53d6\u6ca1\u6709\u5173\u8054\u6a21\u578b\u8f6e\u6b21\u7684\u590d\u76d8\u8bb0\u5f55"));
      else if (!unlinked.items.length) target.append(node("p", "muted", unlinked.next || unlinked.previous.length
        ? "\u672c\u9875\u6ca1\u6709\u672a\u5173\u8054\u590d\u76d8\uff1b\u53ef\u7ee7\u7eed\u67e5\u770b\u5176\u4ed6\u9875"
        : "\u6ca1\u6709\u672a\u5173\u8054\u8f6e\u6b21\u7684\u590d\u76d8"));
      for (const review of unlinked.items) {
        const item = button("", () => selectReview(review.review_ref), "list-item unlinked-review");
        item.dataset.focusKey = `review:${review.review_ref}`;
        item.classList.toggle("is-active", current?.reviewRef === review.review_ref);
        item.setAttribute("aria-current", String(current?.reviewRef === review.review_ref));
        const heading = node("div", "item-heading");
        heading.append(node("strong", "", review.persona_key || "\u4eba\u683c\u672a\u8bb0\u5f55"), node("span", "badge", label(review.status)));
        item.append(heading, node("p", "snippet", review.reason || "\u672a\u5173\u8054\u6a21\u578b\u8f6e\u6b21\uff0c\u7ed3\u679c\u4ee5\u590d\u76d8\u8bb0\u5f55\u4e3a\u51c6"), node("span", "muted", date(review.started_at)));
        target.append(item);
      }
      const controls = node("div", "actions tree-pagination");
      const previous = button("\u4e0a\u4e00\u9875", () => readUnlinked(unlinked.previous.at(-1), "previous"));
      const next = button("\u4e0b\u4e00\u9875", () => readUnlinked(unlinked.next, "next"));
      previous.disabled = unlinked.busy || !unlinked.previous.length;
      next.disabled = unlinked.busy || !unlinked.next;
      const refresh = button(unlinked.loaded ? "\u5237\u65b0" : "\u8bfb\u53d6", () => readUnlinked());
      refresh.disabled = unlinked.busy;
      controls.append(previous, node("span", "muted", `\u590d\u76d8\u8bb0\u5f55\u7b2c ${unlinked.previous.length + 1} \u9875`), next, refresh);
      target.append(controls);
    }
    function render(target, data) {
      if (scopeRef !== (data.scope?.scope_ref || "")) {
        cancel();
        scopeRef = data.scope?.scope_ref || "";
        previousSession = "";
        unlinked = { items: [], cursor: "", previous: [], next: null, loaded: false, busy: false, error: "" };
        target.replaceChildren();
      }
      const opened = new Set([...target.querySelectorAll("details[open]")].map((item) => item.dataset.treeKey));
      const hadGroups = Boolean(target.querySelector(".session-tree-group"));
      const selectedRef = data.session?.session_ref || "";
      const selectionChanged = previousSession !== selectedRef;
      const filterChanged = previousPurpose !== data.purpose;
      const focusKey = target.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      const scrollTop = target.scrollTop;
      current = data;
      root = target;
      previousPurpose = data.purpose;
      target.replaceChildren();
      if (!scopeRef) { target.append(node("p", "empty", "\u8bf7\u9009\u62e9\u9891\u9053")); return; }
      const purposes = [...new Set([...PURPOSES, ...data.sessions.map((session) => session.purpose || "chat")])];
      const visible = purposes.filter((purpose) => !data.purpose || purpose === data.purpose);
      for (const purpose of visible) {
        const sessions = data.sessions.filter((session) => (session.purpose || "chat") === purpose)
          .sort((a, b) => data.newestFirst ? b.sequence - a.sequence : a.sequence - b.sequence);
        if (!sessions.length && purpose !== "companion_reflection") continue;
        const group = node("details", "session-tree-group");
        group.dataset.treeKey = purpose;
        group.dataset.recordKey = `purpose:${purpose}`;
        group.dataset.detailKey = "tree-group";
        group.open = opened.has(purpose) || (selectionChanged && purpose === data.session?.purpose)
          || (filterChanged && purpose === data.purpose) || (!hadGroups && (purpose === "chat" || Boolean(data.purpose)));
        const heading = node("summary", "session-tree-heading");
        heading.dataset.focusKey = `purpose:${purpose}`;
        heading.append(node("strong", "", purposeLabel(purpose)), node("span", "muted", `${sessions.length} \u4e2a\u4f1a\u8bdd`));
        group.append(heading);
        for (const session of sessions) {
          const selected = session.session_ref === selectedRef;
          const branch = node("details", "session-tree-branch");
          branch.dataset.treeKey = session.session_ref;
          branch.dataset.recordKey = session.session_ref;
          branch.dataset.detailKey = "tree-session";
          branch.open = selected && (selectionChanged || opened.has(session.session_ref));
          const summary = node("summary", "session-tree-session");
          summary.dataset.focusKey = `session:${session.session_ref}`;
          summary.append(node("strong", "", `#${session.sequence} \u00b7 ${personaName(session.persona)}`), node("span", "badge", label(session.status)), node("span", "muted", date(session.created_at)));
          summary.addEventListener("click", (event) => {
            if (selected) return;
            event.preventDefault();
            selectSession(session).catch(showError);
          });
          branch.append(summary);
          if (selected) {
            const turns = [...data.turns].sort((a, b) => data.newestFirst ? b.sequence - a.sequence : a.sequence - b.sequence);
            const entries = node("div", "session-tree-turns");
            if (!turns.length) entries.append(node("p", "muted", data.loading ? "\u6b63\u5728\u8bfb\u53d6\u8f6e\u6b21\u2026" : "\u6682\u65e0\u8f6e\u6b21"));
            for (const turn of turns) entries.append(renderTurn(turn));
            branch.append(entries);
          }
          group.append(branch);
        }
        if (purpose === "companion_reflection") {
          const missing = node("details", "unlinked-reviews");
          missing.dataset.treeKey = "unlinked-reviews";
          missing.dataset.recordKey = "unlinked-reviews";
          missing.dataset.detailKey = "tree-unlinked";
          missing.open = opened.has("unlinked-reviews");
          const summary = node("summary", "session-tree-session", "\u672a\u5173\u8054\u8f6e\u6b21\u7684\u590d\u76d8");
          summary.dataset.focusKey = "unlinked-reviews";
          missing.append(summary, node("div", "unlinked-reviews-body"));
          missing.addEventListener("toggle", () => { if (missing.open && !unlinked.loaded && !unlinked.busy) readUnlinked().catch(showError); });
          group.append(missing);
        }
        target.append(group);
      }
      if (!target.children.length) target.append(node("p", "empty", "\u5f53\u524d\u7b5b\u9009\u6ca1\u6709\u4f1a\u8bdd"));
      previousSession = selectedRef;
      renderUnlinked();
      if (focusKey) [...target.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
      target.scrollTop = scrollTop;
    }
    return Object.freeze({
      render, cancel,
      refresh() {
        unlinked.loaded = false;
        if (root?.querySelector(".unlinked-reviews[open]")) return readUnlinked();
      },
    });
  }
  global.SessionTree = Object.freeze({ create });
})(window);
