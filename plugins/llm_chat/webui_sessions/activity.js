"use strict";

(function defineSessionActivity(global) {
  const PAGE_SIZE = 40;
  const CONCURRENCY = 3;

  function create(hooks) {
    const { node, button, disclosure, badge, date, request, fetchTurn, renderNative, renderReview, inspect, changed, showError } = hooks;
    let session = null;
    let target = null;
    let generation = 0;
    let controller = new AbortController();
    let pageJob = null;
    let nextCursor = null;
    let gapCursor = null;
    let loaded = false;
    let pageError = "";
    let active = 0;
    const queue = [];
    const records = new Map();
    const bundles = new Map();
    let controls = null;
    let list = null;
    let latestKeys = new Set();

    function cancelled() { return new DOMException("\u8bfb\u53d6\u5df2\u505c\u6b62", "AbortError"); }
    function cancel() {
      ++generation;
      controller.abort();
      controller = new AbortController();
      for (const job of queue.splice(0)) job.reject(cancelled());
      pageJob = null;
      for (const record of records.values()) { record.pending = false; record.job = null; }
      bundles.clear();
      renderControls();
    }
    function pump() {
      while (active < CONCURRENCY && queue.length) {
        const job = queue.shift();
        if (job.version !== generation || job.signal?.aborted) { job.reject(cancelled()); continue; }
        ++active;
        Promise.resolve().then(() => {
          const signal = AbortSignal.any([job.controllerSignal, ...(job.signal ? [job.signal] : [])]);
          if (signal.aborted) throw cancelled();
          return job.run(signal);
        })
          .then(job.resolve, job.reject).finally(() => { --active; pump(); });
      }
    }
    function schedule(run, signal) {
      return new Promise((resolve, reject) => { queue.push({ run, signal, resolve, reject, version: generation, controllerSignal: controller.signal }); pump(); });
    }
    function bundle(turnRef, signal, fresh = false) {
      if (!turnRef) return Promise.reject(new Error("\u539f\u751f\u8f6e\u6b21\u5f15\u7528\u672a\u8bb0\u5f55"));
      const cached = bundles.get(turnRef);
      if (cached && (!fresh || cached.pending)) return cached.promise;
      const entry = { pending: true, promise: null };
      const version = generation;
      entry.promise = schedule((signal) => fetchTurn(turnRef, { signal }), signal).then((value) => {
        if (version !== generation) throw cancelled();
        entry.pending = false;
        entry.value = value;
        return value;
      }).catch((error) => { if (bundles.get(turnRef) === entry) bundles.delete(turnRef); throw error; });
      bundles.set(turnRef, entry);
      return entry.promise;
    }
    function keyOf(element, root) {
      const parts = [];
      for (let current = element; current && current !== root; current = current.parentElement) {
        if (current.dataset.recordKey) parts.unshift(current.dataset.recordKey);
      }
      return [...parts, element.dataset.detailKey || element.querySelector("summary")?.textContent || element.tagName].join("|");
    }
    function preserve(root, update) {
      const details = new Map([...root.querySelectorAll("details")].map((item) => [keyOf(item, root), item.open]));
      const focus = root.contains(document.activeElement) ? document.activeElement : null;
      const focusKey = focus?.dataset.focusKey;
      const summaryKey = focus?.tagName === "SUMMARY" ? keyOf(focus.parentElement, root) : null;
      const contentScroll = new Map([...root.querySelectorAll(".invocation-content, .content-code, .invocation-body")].map((item, index) => [`${keyOf(item, root)}|${index}`, [item.scrollTop, item.scrollLeft]]));
      const scroller = target?.closest(".inspector");
      const top = scroller?.scrollTop;
      const anchor = [...(list?.children || [])].find((item) => item.getBoundingClientRect().bottom > (scroller?.getBoundingClientRect().top || 0));
      const offset = anchor?.getBoundingClientRect().top;
      update();
      for (const item of root.querySelectorAll("details")) {
        const key = keyOf(item, root);
        if (details.has(key)) item.open = details.get(key);
        if (key === summaryKey) item.querySelector("summary")?.focus({ preventScroll: true });
      }
      if (focusKey) [...root.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
      for (const [index, item] of [...root.querySelectorAll(".invocation-content, .content-code, .invocation-body")].entries()) {
        const position = contentScroll.get(`${keyOf(item, root)}|${index}`);
        if (position) { item.scrollTop = position[0]; item.scrollLeft = position[1]; }
      }
      if (scroller && anchor?.isConnected) scroller.scrollTop = top + anchor.getBoundingClientRect().top - offset;
      else if (scroller) scroller.scrollTop = top;
    }
    function destination(item) {
      const value = item.destination || item.reminder?.destination;
      if (!value || value.is_source_scope !== false) return "";
      return value.type === "dm"
        ? `\u53d1\u5f80\u79c1\u804a ${value.platform_user_id || ""}\uff0c\u975e\u672c\u9891\u9053\u9001\u8fbe`
        : `\u53d1\u5f80\u5176\u4ed6\u9891\u9053 ${value.channel_id || ""}\uff0c\u975e\u672c\u9891\u9053\u9001\u8fbe`;
    }
    function metadata(item) {
      if (item.kind === "review") return "\u590d\u76d8";
      if (item.kind === "reminder") return `\u63d0\u9192\u8ba1\u5212 \u00b7 ${item.reminder?.goal || ""}`;
      if (item.kind === "reminder_occurrence") return `\u63d0\u9192\u6267\u884c #${item.occurrence?.sequence ?? "?"}`;
      if (item.relation === "reminder") return `\u63d0\u9192\u6267\u884c \u00b7 ${item.reminder?.goal || ""}`;
      if (item.relation === "contact") return "\u4e3b\u52a8\u8054\u7cfb";
      return `\u5bf9\u8bdd #${item.turn?.sequence ?? "?"}`;
    }
    function renderControls() {
      if (!controls) return;
      const focus = controls.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      controls.replaceChildren();
      const older = button(pageJob ? "\u6b63\u5728\u8bfb\u53d6\u2026" : gapCursor ? "\u7ee7\u7eed\u540c\u6b65\u4e2d\u95f4\u8bb0\u5f55" : "\u52a0\u8f7d\u66f4\u65e9\u8bb0\u5f55", () => loadPage(gapCursor ? "gap" : "older"));
      older.dataset.focusKey = "activity:older";
      older.disabled = Boolean(pageJob) || (!nextCursor && !gapCursor && loaded);
      const refresh = button("\u5237\u65b0\u5bf9\u8bdd", () => refreshFeed());
      refresh.dataset.focusKey = "activity:refresh";
      refresh.disabled = Boolean(pageJob);
      controls.append(older, refresh);
      if (pageError) controls.append(node("p", "error", pageError));
      else if (gapCursor) controls.append(node("p", "muted", "\u5c1a\u6709\u672a\u540c\u6b65\u8bb0\u5f55"));
      else if (loaded && !nextCursor) controls.append(node("span", "muted", "\u5df2\u5230\u4f1a\u8bdd\u8d77\u70b9"));
      if (focus) [...controls.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focus)?.focus({ preventScroll: true });
    }
    function renderReminder(item) {
      const body = node("div", "activity-reminder-state");
      const reminder = item.reminder || {};
      body.append(node("p", "", reminder.goal || "\u63d0\u9192\u76ee\u6807\u672a\u8bb0\u5f55"));
      body.append(node("p", "activity-reminder-meta", `${date(reminder.due_at)} \u00b7 ${reminder.delivery === "dm" ? "\u79c1\u804a" : reminder.delivery === "channel" ? "\u9891\u9053" : "\u76ee\u7684\u5730\u672a\u8bb0\u5f55"}`));
      if (destination(item)) body.append(node("p", "partial-notice", destination(item)));
      if (reminder.paused_reason) body.append(node("p", "partial-notice", reminder.paused_reason));
      if (reminder.source_turn_ref) body.append(button("\u67e5\u770b\u521b\u5efa\u8f6e\u6b21", () => hooks.evidence({ turn_ref: reminder.source_turn_ref, session_ref: item.session_ref })));
      return body;
    }
    function renderBody(record) {
      const item = record.item;
      preserve(record.root, () => {
        record.body.replaceChildren();
        if (record.error) {
          record.body.append(node("p", "error", record.error), button("\u91cd\u8bd5\u8bfb\u53d6", () => ensure(record, true)));
          return;
        }
        if (item.kind === "reminder") { record.body.append(renderReminder(item)); return; }
        if (item.kind === "reminder_occurrence") {
          record.body.append(node("p", "partial-notice", item.recording_status === "invalid"
            ? "\u539f\u751f\u6267\u884c\u8bb0\u5f55\u5173\u8054\u65e0\u6548"
            : "\u672c\u6b21\u6267\u884c\u672a\u7559\u5b58\u539f\u751f\u8f6e\u6b21\u8bb0\u5f55"));
          if (destination(item)) record.body.append(node("p", "partial-notice", destination(item)));
          const outcome = node("p", "activity-occurrence-outcome");
          outcome.append(badge(item.occurrence?.state));
          record.body.append(outcome, renderReminder(item));
          return;
        }
        if (item.kind === "review" && record.review) record.body.append(renderReview(record.review, null, record.bundle));
        if (record.bundle && item.kind !== "review") {
          record.body.append(renderNative(record.bundle, { background: item.relation === "reminder" || item.relation === "contact" || item.kind === "review", includeReview: false, destination: destination(item) }));
          if (record.fold) {
            const choose = button("\u68c0\u67e5\u672c\u8f6e\u4e0a\u4e0b\u6587\u4e0e\u5173\u7cfb", () => inspect(record.bundle));
            choose.dataset.focusKey = `${item.key}:inspect`;
            record.body.append(choose);
          }
        } else if (record.pending && (item.kind !== "review" || !record.review)) record.body.append(node("p", "activity-loading", "\u6b63\u5728\u8bfb\u53d6\u539f\u751f\u8bb0\u5f55\u2026"));
        if (record.nativeError) record.body.append(node("p", "error", record.nativeError), button("\u91cd\u8bd5\u8bfb\u53d6\u8bf7\u6c42\u4e0e\u5de5\u5177", () => ensure(record, true)));
      });
    }
    async function ensure(record, fresh = false) {
      if (record.pending) return record.job;
      const item = record.item;
      if (!["turn", "review"].includes(item.kind)) return;
      if (!fresh && (record.bundle || (record.review && !record.review.native_turn))) return;
      record.pending = true;
      record.error = "";
      record.nativeError = "";
      const version = generation;
      renderBody(record);
      record.job = (async () => {
        if (item.kind === "review") {
          try {
            record.review = await review(item.scope_ref, item.review.review_ref);
            if (version !== generation) return;
          } catch (error) { if (version === generation && error?.name !== "AbortError") record.error = error.message; return; }
        }
        const turnRef = item.turn?.turn_ref || record.review?.native_turn?.turn_ref;
        if (turnRef) {
          try {
            const value = await bundle(turnRef, undefined, fresh);
            if (version !== generation) return;
            record.bundle = { ...value, activity: record.item };
            changed?.(record.bundle);
          } catch (error) { if (version === generation && error?.name !== "AbortError") record.nativeError = error.message; }
        }
      })().finally(() => {
        if (version !== generation) return;
        record.pending = false;
        record.job = null;
        renderBody(record);
      });
      return record.job;
    }
    async function review(scopeRef, reviewRef, signal) {
      const result = await schedule((signal) => request(`/scopes/${encodeURIComponent(scopeRef)}/companion/reviews/${encodeURIComponent(reviewRef)}`, { signal }), signal);
      return result.item;
    }
    function makeRecord(item) {
      const root = node("article", `activity-item activity-${item.kind}`);
      root.dataset.recordKey = item.key;
      root.dataset.activityKey = item.key;
      const folded = item.kind !== "turn" || item.relation === "reminder";
      let body;
      let fold;
      if (folded) {
        fold = disclosure(metadata(item), "", "record-card activity-disclosure");
        fold.item.dataset.detailKey = "activity-record";
        fold.summary.append(badge(item.review?.status || item.occurrence?.state || item.turn?.status || item.reminder?.status));
        fold.summary.append(node("span", "activity-record-time", date(item.review?.started_at || item.created_at)));
        body = fold.content;
        root.append(fold.item);
      } else {
        const heading = node("div", "activity-turn-heading");
        heading.append(node("span", "muted", `${metadata(item)} \u00b7 ${date(item.created_at)}`));
        const choose = button("\u68c0\u67e5\u672c\u8f6e", () => record.bundle ? inspect(record.bundle) : ensure(record).then(() => record.bundle && inspect(record.bundle)), "quiet-button");
        choose.dataset.focusKey = `${item.key}:inspect`;
        heading.append(choose);
        body = node("div", "activity-turn-body");
        root.append(heading, body);
      }
      const record = { item, root, body, fold, signature: JSON.stringify(item), bundle: null, review: null, pending: false, error: "", nativeError: "", job: null };
      fold?.item.addEventListener("toggle", () => { if (fold.item.open) ensure(record).catch(showError); });
      renderBody(record);
      if (!folded) ensure(record).catch(showError);
      return record;
    }
    function merge(items, mode, cursor) {
      const existing = new Set(records.keys());
      const overlaps = items.some((item) => existing.has(item.key));
      if (mode === "refresh") latestKeys = new Set(items.map((item) => item.key));
      preserve(list, () => {
        for (const placeholder of list.querySelectorAll(".empty")) placeholder.remove();
        for (const item of items) {
          if (!item || typeof item.key !== "string" || !["turn", "review", "reminder", "reminder_occurrence"].includes(item.kind)) continue;
          let record = records.get(item.key);
          if (!record) { record = makeRecord(item); records.set(item.key, record); }
          else if (record.signature !== JSON.stringify(item)) {
            record.item = item;
            record.signature = JSON.stringify(item);
            if (record.fold) {
              record.fold.summary.querySelector(".disclosure-title").textContent = metadata(item);
              const status = record.fold.summary.querySelector(".badge");
              status?.replaceWith(badge(item.review?.status || item.occurrence?.state || item.turn?.status || item.reminder?.status));
              record.fold.summary.querySelector(".activity-record-time").textContent = date(item.review?.started_at || item.created_at);
            }
            if (record.bundle || record.review || !record.fold || record.fold.item.open) ensure(record, true).catch(showError);
            else renderBody(record);
          }
        }
        const instant = (value) => Date.parse(value) || 0;
        const ordered = [...records.values()].sort((a, b) => instant(a.item.created_at) - instant(b.item.created_at) || a.item.key.localeCompare(b.item.key));
        let previous = null;
        for (const record of ordered) {
          const expected = previous ? previous.nextElementSibling : list.firstElementChild;
          if (record.root !== expected) list.insertBefore(record.root, expected);
          previous = record.root;
        }
      });
      if (mode === "refresh" && loaded && existing.size) {
        if (!gapCursor) gapCursor = !overlaps && cursor ? cursor : null;
      } else if (mode === "gap") gapCursor = !overlaps && cursor ? cursor : null;
      else nextCursor = cursor;
      loaded = true;
      if (!records.size) list.replaceChildren(node("p", "empty", "\u672c\u9875\u6682\u65e0\u6d3b\u52a8\u8bb0\u5f55"));
    }
    async function loadPage(mode = "refresh") {
      if (!session || document.hidden) return;
      if (pageJob) return pageJob;
      const cursor = mode === "gap" ? gapCursor : mode === "older" ? nextCursor : null;
      if (mode !== "refresh" && !cursor && loaded) return;
      const version = generation;
      const source = session.session_ref;
      pageError = "";
      pageJob = (async () => {
        try {
          const query = new URLSearchParams({ view: "activity", limit: String(PAGE_SIZE), ...(cursor ? { cursor } : {}) });
          const result = await request(`/sessions/${encodeURIComponent(source)}/turns?${query}`, { signal: controller.signal });
          if (version !== generation || source !== session?.session_ref) return;
          const next = result.next_cursor || null;
          if (next && next === cursor) throw new Error("\u6d3b\u52a8\u5206\u9875\u6e38\u6807\u672a\u524d\u8fdb\uff0c\u5df2\u505c\u6b62\u8bfb\u53d6\u3002");
          merge(result.items || [], mode, next);
        } catch (error) { if (version === generation && error?.name !== "AbortError") pageError = error.message; }
        finally { if (version === generation) { pageJob = null; renderControls(); } }
      })();
      renderControls();
      return pageJob;
    }
    async function refreshFeed() {
      await loadPage("refresh");
      if (document.hidden) return;
      const jobs = [];
      for (const record of records.values()) {
        if (!latestKeys.has(record.item.key)) continue;
        const evaluation = record.bundle?.inspection?.relationship_evaluation;
        if (record.item.turn?.status === "running" || ["active", "pending"].includes(record.item.review?.status)
          || ["pending", "running"].includes(evaluation?.status)) {
          if (!record.fold || record.fold.item.open || record.bundle) jobs.push(ensure(record, true));
        }
      }
      await Promise.all(jobs);
    }
    async function open(element, selected) {
      cancel();
      session = selected;
      target = element;
      records.clear();
      nextCursor = null; gapCursor = null; loaded = false; pageError = "";
      latestKeys.clear();
      controls = node("div", "actions activity-controls");
      list = node("div", "activity-feed");
      target.replaceChildren(controls, list);
      if (!session) { list.append(node("p", "empty", "\u8bf7\u9009\u62e9\u4e00\u4e2a\u666e\u901a\u4f1a\u8bdd")); return; }
      await loadPage();
    }
    async function reveal(turnRef) {
      const record = [...records.values()].find((entry) => entry.item.turn?.turn_ref === turnRef
        || entry.item.review?.native_turn?.turn_ref === turnRef || entry.review?.native_turn?.turn_ref === turnRef);
      if (!record) return false;
      if (record.fold) record.fold.item.open = true;
      await ensure(record);
      record.root.scrollIntoView({ block: "center" });
      const focus = record.fold?.summary || record.root.querySelector("button");
      focus?.focus({ preventScroll: true });
      if (record.bundle) inspect(record.bundle);
      return true;
    }
    function resume() {
      for (const record of records.values()) if (!record.fold || record.fold.item.open) ensure(record).catch(showError);
    }
    return Object.freeze({ open, cancel, resume, refresh: refreshFeed, reveal, bundle, review });
  }
  global.SessionActivity = Object.freeze({ create });
})(window);
