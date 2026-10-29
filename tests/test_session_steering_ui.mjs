import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const app = readFileSync(new URL("../plugins/llm_chat/webui_sessions/app.js", import.meta.url), "utf8");
const content = readFileSync(new URL("../plugins/llm_chat/webui_sessions/content.js", import.meta.url), "utf8");
const companion = readFileSync(new URL("../plugins/llm_chat/webui_sessions/companion.js", import.meta.url), "utf8");
const navigation = readFileSync(new URL("../plugins/llm_chat/webui_sessions/navigation.js", import.meta.url), "utf8");
const activity = readFileSync(new URL("../plugins/llm_chat/webui_sessions/activity.js", import.meta.url), "utf8");

function page() {
  function descendants(element) {
    return element.children.flatMap((child) => [child, ...descendants(child)]);
  }
  function matches(element, selector) {
    const parts = selector.trim().split(/\s+/);
    const last = parts.pop();
    const own = (item, part) => {
      if (part === "[data-focus-key]") return Boolean(item.dataset.focusKey);
      if (part === "[data-record-key]") return Boolean(item.dataset.recordKey);
      if (part === "[role=tab]") return item.attributes.role === "tab";
      if (part.startsWith("#")) return item.id === part.slice(1);
      if (part.startsWith(".")) return item.classList.contains(part.slice(1));
      if (part === "details[open]") return item.tagName === "DETAILS" && item.open;
      return item.tagName === part.toUpperCase();
    };
    if (!own(element, last)) return false;
    for (const part of parts.reverse()) {
      element = element.parentElement;
      while (element && !own(element, part)) element = element.parentElement;
      if (!element) return false;
    }
    return true;
  }
  class Element extends EventTarget {
    constructor(tag) {
      super();
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.dataset = {};
      this.attributes = {};
      this.parentElement = null;
      this.className = "";
      this.open = false;
      this.scrollTop = 0;
      this.scrollLeft = 0;
      this.classList = {
        contains: (value) => this.className.split(/\s+/).includes(value),
        add: (...values) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...values])].join(" "); },
        toggle: (value, force) => {
          const enabled = force ?? !this.classList.contains(value);
          this.className = this.className.split(/\s+/).filter((item) => item && item !== value).join(" ");
          if (enabled) this.classList.add(value);
          return enabled;
        },
      };
    }
    get childNodes() { return this.children; }
    get firstElementChild() { return this.children[0] || null; }
    get lastElementChild() { return this.children.at(-1) || null; }
    get nextElementSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1] || null; }
    get isConnected() { return this === document.body || Boolean(this.parentElement?.isConnected); }
    get textContent() { return (this.ownText || "") + this.children.map((child) => child.textContent).join(""); }
    set textContent(value) { this.ownText = String(value); this.replaceChildren(); }
    append(...children) {
      for (const child of children) {
        if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((item) => item !== child);
        this.children.push(child);
        child.parentElement = this;
      }
    }
    insertBefore(child, reference) {
      if (child === reference) return child;
      if (reference !== null && !this.children.includes(reference)) throw new Error("Reference is not a child");
      if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((item) => item !== child);
      const index = reference === null ? this.children.length : this.children.indexOf(reference);
      this.children.splice(index, 0, child);
      child.parentElement = this;
      return child;
    }
    prepend(...children) {
      const reference = this.firstElementChild;
      for (const child of children) this.insertBefore(child, reference);
    }
    replaceChildren(...children) {
      for (const child of this.children) child.parentElement = null;
      this.children = [];
      this.append(...children);
    }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key]; }
    removeAttribute(key) { delete this.attributes[key]; }
    remove() {
      if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
      this.parentElement = null;
    }
    replaceWith(replacement) {
      if (!this.parentElement) return;
      this.parentElement.insertBefore(replacement, this);
      this.remove();
    }
    contains(element) { return element === this || descendants(this).includes(element); }
    getBoundingClientRect() { return { top: 0, bottom: 100 }; }
    scrollIntoView() { this.scrolledIntoView = true; }
    querySelectorAll(selector) {
      return descendants(this).filter((element) => selector.split(",").some((part) => matches(element, part)));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) {
      for (let element = this; element; element = element.parentElement) {
        if (selector.split(",").some((part) => matches(element, part))) return element;
      }
      return null;
    }
    focus() { document.activeElement = this; }
  }
  const document = new EventTarget();
  document.URL = "https://manage.example/api/llm-chat/sessions/page";
  document.body = new Element("body");
  document.createElement = (tag) => new Element(tag);
  document.createElementNS = (_namespace, tag) => new Element(tag);
  document.createTextNode = (value) => {
    const element = new Element("text");
    element.textContent = value;
    return element;
  };
  const workspace = new Element("div");
  workspace.className = "workspace";
  document.body.append(workspace);
  const elements = new Map();
  document.getElementById = (id) => {
    if (!elements.has(id)) {
      const element = new Element("div");
      element.id = id;
      if (id.endsWith("-view")) element.classList.add("view");
      workspace.append(element);
      elements.set(id, element);
    }
    return elements.get(id);
  };
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.querySelector = (selector) => document.body.querySelector(selector);
  const window = new EventTarget();
  window.parent = window;
  window.scrollTo = () => {};
  class Observer { observe() {} unobserve() {} disconnect() {} }
  const sandbox = createContext({
    window, document, URL, URLSearchParams, AbortController, AbortSignal, DOMException,
    IntersectionObserver: Observer, MutationObserver: Observer,
    fetch: () => new Promise(() => {}),
  });
  runInContext(content, sandbox);
  sandbox.SessionContent = window.SessionContent;
  runInContext(companion, sandbox);
  sandbox.SessionCompanion = window.SessionCompanion;
  runInContext(navigation, sandbox);
  sandbox.SessionTree = window.SessionTree;
  runInContext(activity, sandbox);
  sandbox.SessionActivity = window.SessionActivity;
  runInContext(app + `
    renderTurns = () => {};
    renderSession = () => {};
    const renderInputPanel = () => $("io-view").replaceChildren(renderNativeFlow(selectedBundle()));
    renderWorkspace = renderInputPanel;
    state.turn = { user_name: "Alice", status: "running" };
    state.inspection = { turn: state.turn, outputs: [] };
    globalThis.panel = {
      render(events) { state.events = events; renderInputPanel(); },
      refresh(events) { state.events = events; refreshPanels(); },
      formatDate: date,
      native: renderNativeFlow,
      review: (item, fallback, bundle) => companionRenderer.review(item, fallback, bundle),
      calls: (bundle) => timelineCalls(true, bundle).map((entry) => entry.card()),
      selected() { return state.inspection; },
      hooks: { node, button, disclosure, badge, date, showError },
    };
  `, sandbox);
  return { panel: sandbox.panel, document, Element, activity: window.SessionActivity, navigation: window.SessionTree, companion: window.SessionCompanion,
    cards: () => document.getElementById("io-view").querySelectorAll(".input-record") };
}

function input(ref, content, steering) {
  return {
    event_ref: ref, event_type: "user_input", preview: content,
    message: { speaker: "Alice", role: "participant", content, reply_to: null, forwarded_messages: [], images: [], mentions: [], truncated: false },
    ...(steering ? { steering } : {}),
  };
}

const receivedAt = "2026-10-13T00:00:01";
const appliedAt = "2026-10-13T00:00:04";
const receipt = {
  event_ref: "receipt-2", event_type: "steer_received", preview: "More detail",
  steering: { sequence: 2, status: "received", received_at: receivedAt, applied_at: null },
};
const applied = input("applied-2", "More detail", {
  sequence: 2, status: "applied", received_at: receivedAt, applied_at: appliedAt,
});

test("input panel keeps the original, replaces the receipt once, and shows actual application times", () => {
  const ui = page();
  const root = input("root", "Original question");
  ui.panel.render([root, receipt]);
  const [original, pending] = ui.cards();
  assert.ok(pending.textContent.includes(ui.panel.formatDate(receivedAt)));
  assert.ok(!pending.textContent.includes(ui.panel.formatDate(appliedAt)));
  original.querySelector("details").open = true;
  pending.querySelector("details").open = true;
  ui.panel.refresh([root, receipt, applied]);
  const cards = ui.cards();
  assert.equal(cards.length, 2);
  assert.ok(cards.every((card) => card.querySelector("details").open));
  assert.ok(cards[1].textContent.includes(ui.panel.formatDate(receivedAt)));
  assert.ok(cards[1].textContent.includes(ui.panel.formatDate(appliedAt)));
  assert.match(cards[1].textContent, /\u5df2\u5e94\u7528/);
  assert.ok(!cards[0].textContent.includes("\u66ff\u4ee3"));
  assert.equal(cards[1].querySelector(".content-message").textContent.match(/More detail/g).length, 1);
});

test("unapplied receipt uses its recorded checkpoint without claiming application", () => {
  const ui = page();
  ui.panel.render([input("root", "Original question"), receipt, {
    event_ref: "checkpoint-2", event_type: "steer_checkpoint",
    steering: { sequence: 2, status: "unapplied", received_at: receivedAt, applied_at: null, reason: "turn_failed" },
  }]);
  const supplement = ui.cards()[1];
  assert.match(supplement.textContent, /\u672a\u5e94\u7528/);
  assert.match(supplement.textContent, /turn_failed/);
  assert.ok(!supplement.textContent.includes(ui.panel.formatDate(appliedAt)));
});

test("legacy multiple input cards retain each recorded message", () => {
  const ui = page();
  ui.panel.render([input("legacy-a", "First old input"), input("legacy-b", "Second old input")]);
  const cards = ui.cards();
  assert.equal(cards.length, 2);
  assert.match(cards[0].textContent, /First old input/);
  assert.match(cards[1].textContent, /Second old input/);
});

async function flush() { for (let index = 0; index < 50; ++index) await Promise.resolve(); }
async function click(element) { element.dispatchEvent(new Event("click")); await flush(); }
function nativeBundle(ref, purpose = "chat") {
  return {
    inspection: {
      turn: { turn_ref: ref, purpose, status: "completed", trigger_kind: purpose === "chat" ? "message" : purpose },
      persona: { name: `Bot ${ref}` },
      model_calls: [{ model: `model-${ref}`, status: "succeeded", request_event_ref: `${ref}-request`, response_event_ref: `${ref}-response`, output_preview: "generated", input_preview: "context" }],
      tool_calls: [{ tool_name: "lookup", status: "succeeded", call_event_ref: `${ref}-tool`, result_event_ref: `${ref}-result`, arguments_preview: { query: ref }, result_preview: "result" }],
      outputs: [{ event_ref: `${ref}-delivery`, source: "message_delivery", content: `confirmed ${ref}`, capture_status: "captured" }],
    },
    events: [
      { ...input(`${ref}-input`, `question ${ref}`), sequence: 1 },
      { event_ref: `${ref}-request`, sequence: 2, event_type: "model_request" },
      { event_ref: `${ref}-tool`, sequence: 3, event_type: "assistant_tool_call" },
      { event_ref: `${ref}-delivery`, sequence: 4, event_type: "message_delivery" },
    ],
  };
}
function activityTurn(ref, timestamp = "2026-10-28T00:00:00Z", relation = "direct") {
  return { kind: "turn", key: `turn:${ref}`, created_at: timestamp, scope_ref: "scope", session_ref: "ordinary", relation,
    turn: { turn_ref: ref, sequence: 1, purpose: relation === "reminder" ? "reminder" : "chat", status: "completed" } };
}
function activityPage(ui, options = {}) {
  const target = ui.document.getElementById("activity-test");
  target.className = "inspector";
  const calls = [];
  const controller = ui.activity.create({
    ...ui.panel.hooks,
    request: async (path, requestOptions) => { calls.push(path); return options.request ? options.request(path, requestOptions) : { items: options.items || [], next_cursor: null }; },
    fetchTurn: options.fetchTurn || (async (ref) => nativeBundle(ref)),
    renderNative: ui.panel.native,
    renderReview: (review) => ui.panel.hooks.node("div", "review-detail", `review detail ${review.review_ref}`),
    inspect: options.inspect || (() => {}), evidence: options.evidence || (() => {}),
  });
  return { target, controller, calls };
}

test("native bundle rendering keeps explicit persona, event order and global selection", () => {
  const ui = page();
  const before = ui.panel.selected();
  const flow = ui.panel.native(nativeBundle("other"));
  assert.equal(ui.panel.selected(), before);
  assert.match(flow.querySelector(".delivery-record").textContent, /Bot other/);
  assert.equal(flow.children[0].classList.contains("input-record"), true);
  assert.equal(flow.children[1].classList.contains("model-record"), true);
  assert.equal(flow.children[2].classList.contains("tool-record"), true);
  assert.equal(flow.children[3].classList.contains("delivery-record"), true);
  assert.equal(flow.querySelector(".model-record").open, false);
  assert.equal(flow.querySelector(".tool-record").open, false);
});

test("ordinary session feed expands messages and lazily expands reviews and reminder executions inline", async () => {
  const ui = page();
  const review = { kind: "review", key: "review:r", scope_ref: "scope", session_ref: "ordinary", created_at: "2026-10-28T00:00:01Z", review: { review_ref: "r", status: "completed" } };
  const reminder = { ...activityTurn("scheduled", "2026-10-28T00:00:02Z", "reminder"), reminder: { goal: "remember", delivery: "dm", status: "active", destination: { type: "dm", platform_user_id: "user", is_source_scope: false } } };
  const uiFeed = activityPage(ui, { request(path) {
    if (path.includes("/companion/reviews/")) return { item: { review_ref: "r", native_turn: { turn_ref: "reflection" } } };
    return { items: [activityTurn("first"), review, reminder], next_cursor: null };
  }, fetchTurn: async (ref) => nativeBundle(ref, ref === "reflection" ? "companion_reflection" : ref === "scheduled" ? "reminder" : "chat") });
  await uiFeed.controller.open(uiFeed.target, { session_ref: "ordinary" }); await flush();
  assert.match(uiFeed.calls[0], /view=activity/);
  assert.equal(uiFeed.target.querySelector(".activity-turn").firstElementChild.tagName, "DIV");
  assert.equal(uiFeed.target.querySelectorAll(".delivery-record").length, 1);
  assert.equal(uiFeed.calls.some((path) => path.includes("/companion/reviews/")), false);
  const reviewDetails = uiFeed.target.querySelector(".activity-review details");
  reviewDetails.open = true; reviewDetails.dispatchEvent(new Event("toggle")); await flush();
  assert.match(reviewDetails.textContent, /review detail r/);
  assert.equal(reviewDetails.querySelectorAll(".model-record").length, 0);
  assert.equal(reviewDetails.querySelectorAll(".tool-record").length, 0);
  const scheduled = uiFeed.target.querySelectorAll(".activity-turn")[1].querySelector("details");
  scheduled.open = true; scheduled.dispatchEvent(new Event("toggle")); await flush();
  assert.match(scheduled.textContent, /\u975e\u672c\u9891\u9053\u9001\u8fbe/);
  assert.equal(scheduled.querySelector(".activity-preparation").open, false);
  assert.match(scheduled.querySelector(".delivery-record").textContent, /confirmed scheduled/);
});

test("older pagination traverses empty pages and preserves disclosures, focus and selected feed nodes", async () => {
  const ui = page();
  let refreshCount = 0;
  const feed = activityPage(ui, { request(path) {
    const cursor = new URL(path, "https://example.test").searchParams.get("cursor");
    if (cursor === "older-1") return { items: [], next_cursor: "older-2" };
    if (cursor === "older-2") return { items: [activityTurn("old", "2026-10-27T00:00:00Z")], next_cursor: null };
    return { items: [activityTurn("current")], next_cursor: ++refreshCount ? "older-1" : null };
  } });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  const record = feed.target.querySelector(".activity-turn");
  const model = record.querySelector(".model-record");
  model.open = true; model.querySelector("summary").focus();
  feed.target.scrollTop = 37;
  await click(feed.target.querySelector(".activity-controls button"));
  assert.equal(feed.target.querySelector(".activity-controls button").disabled, false);
  await click(feed.target.querySelector(".activity-controls button")); await flush();
  assert.equal(feed.target.querySelectorAll(".activity-turn").length, 2);
  await feed.controller.refresh(); await flush();
  assert.equal(feed.target.querySelectorAll(".activity-turn")[1], record);
  assert.equal(model.open, true);
  assert.equal(ui.document.activeElement, model.querySelector("summary"));
  assert.equal(feed.target.scrollTop, 37);
  assert.equal(await feed.controller.reveal("current"), true);
  assert.equal(record.scrolledIntoView, true);
});

test("native bundle loads are bounded and cancelled when leaving the feed", async () => {
  const ui = page();
  let active = 0;
  let peak = 0;
  let aborted = 0;
  const feed = activityPage(ui, {
    items: Array.from({ length: 12 }, (_, index) => activityTurn(`turn-${index}`)),
    fetchTurn(_ref, { signal }) {
      ++active; peak = Math.max(peak, active);
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => {
        --active; ++aborted; reject(new DOMException("aborted", "AbortError"));
      }, { once: true }));
    },
  });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  assert.equal(peak, 3);
  feed.controller.cancel(); await flush();
  assert.equal(aborted, 3);
  assert.equal(active, 0);
  assert.equal(feed.target.querySelectorAll(".delivery-record").length, 0);
});

test("navigation contains only ordinary sessions and unassigned history stays inside channel info", async () => {
  const ui = page();
  const target = ui.document.getElementById("navigation-test");
  ui.navigation.create({ ...ui.panel.hooks, label: String, personaName: () => "Persona", selectSession() {} }).render(target, {
    scope: { scope_ref: "scope" }, newestFirst: true,
    sessions: [{ session_ref: "chat", purpose: "chat", sequence: 1 }, { session_ref: "review", purpose: "companion_reflection", sequence: 2 }, { session_ref: "task", purpose: "chat", reminder_task_ref: "task", sequence: 3 }],
  });
  assert.equal(target.querySelectorAll("button").length, 1);
  assert.equal(target.querySelectorAll("details").length, 0);
  const archiveTarget = ui.document.getElementById("archive-test");
  let pages = 0;
  const archive = ui.navigation.createReviewArchive({
    ...ui.panel.hooks, request: async () => (++pages === 1
      ? { items: [{ review_ref: "assigned", activity_association: "assigned" }], next_cursor: "next" }
      : { items: [{ review_ref: "orphan", activity_association: "unassigned", native_turn: { turn_ref: "orphan-turn" } }], next_cursor: null }),
    loadReview: async () => ({ review_ref: "orphan", native_turn: { turn_ref: "orphan-turn" } }),
    loadBundle: async (ref) => nativeBundle(ref, "companion_reflection"),
    renderReview: () => ui.panel.hooks.node("p", "", "orphan review"), renderNative: ui.panel.native,
  });
  archive.open(archiveTarget, "scope");
  const disclosure = archiveTarget.querySelector("details");
  disclosure.open = true; disclosure.dispatchEvent(new Event("toggle")); await flush();
  assert.equal(archiveTarget.querySelectorAll(".review-history-item").length, 0);
  const next = archiveTarget.querySelectorAll("button").find((item) => item.dataset.focusKey === "archive:next");
  assert.equal(next.disabled, false);
  await click(next);
  const orphan = archiveTarget.querySelector(".review-history-item");
  assert.ok(orphan);
  orphan.open = true; orphan.dispatchEvent(new Event("toggle")); await flush();
  assert.match(orphan.textContent, /orphan review/);
  assert.equal(orphan.querySelectorAll(".model-record").length, 0);
  archive.cancel();
});

test("changed native bundle refresh preserves expanded requests, focus and scroll", async () => {
  const ui = page();
  let revision = 0;
  const feed = activityPage(ui, {
    request: async () => ({ items: [{ ...activityTurn("changing"), turn: { ...activityTurn("changing").turn, revision: ++revision } }], next_cursor: null }),
    fetchTurn: async (ref) => {
      const bundle = nativeBundle(ref);
      bundle.inspection.outputs[0].content = `confirmed revision ${revision}`;
      return bundle;
    },
  });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  const model = feed.target.querySelector(".model-record");
  model.open = true; model.querySelector("summary").focus(); feed.target.scrollTop = 63;
  await feed.controller.refresh(); await flush();
  const refreshed = feed.target.querySelector(".model-record");
  assert.equal(refreshed.open, true);
  assert.equal(ui.document.activeElement, refreshed.querySelector("summary"));
  assert.equal(feed.target.scrollTop, 63);
  assert.match(feed.target.querySelector(".delivery-record").textContent, /confirmed revision 2/);
});

test("unrecorded reminder occurrence remains a collapsed safe state without fabricated messages", async () => {
  const ui = page();
  let nativeReads = 0;
  const feed = activityPage(ui, {
    items: [{ kind: "reminder_occurrence", key: "occurrence:task:1", scope_ref: "scope", session_ref: "ordinary", created_at: "2026-10-28T00:00:00Z", recording_status: "missing",
      reminder: { task_ref: "task", goal: "remember", delivery: "channel", status: "active" }, occurrence: { sequence: 1, state: "pending" } }],
    fetchTurn: async () => { ++nativeReads; return nativeBundle("never"); },
  });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  const occurrence = feed.target.querySelector("details");
  assert.equal(occurrence.open, false);
  occurrence.open = true; occurrence.dispatchEvent(new Event("toggle")); await flush();
  assert.match(occurrence.textContent, /\u672a\u7559\u5b58\u539f\u751f\u8f6e\u6b21\u8bb0\u5f55/);
  assert.equal(feed.target.querySelectorAll(".delivery-record").length, 0);
  assert.equal(nativeReads, 0);
});

test("refresh gaps remain traversable across repeated polls without dropping older records", async () => {
  const ui = page();
  let latest = false;
  const feed = activityPage(ui, { request(path) {
    const cursor = new URL(path, "https://example.test").searchParams.get("cursor");
    if (cursor === "bridge") return { items: [activityTurn("middle", "2026-10-28T12:00:00Z"), activityTurn("original")], next_cursor: null };
    return latest ? { items: [activityTurn("newest", "2026-10-29T00:00:00Z")], next_cursor: "bridge" }
      : { items: [activityTurn("original")], next_cursor: null };
  } });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  latest = true;
  await feed.controller.refresh(); await flush();
  await feed.controller.refresh(); await flush();
  const bridge = feed.target.querySelector(".activity-controls button");
  assert.equal(bridge.disabled, false);
  assert.match(bridge.textContent, /\u7ee7\u7eed\u540c\u6b65/);
  await click(bridge);
  assert.equal(feed.target.querySelectorAll(".activity-turn").length, 3);
});

test("chat rows use recorded roles for left participants and stable right bot bubbles", () => {
  const ui = page();
  const bundle = nativeBundle("roles");
  bundle.events[0].message.speaker = "Bot roles";
  const flow = ui.panel.native(bundle);
  const participant = flow.querySelector(".input-record");
  const bot = flow.querySelector(".delivery-record");
  assert.ok(participant.classList.contains("message-left"));
  assert.ok(participant.classList.contains("message-participant"));
  assert.ok(bot.classList.contains("message-right"));
  assert.ok(bot.classList.contains("message-bot"));
  assert.equal(participant.querySelector(".message-avatar").textContent, "B");
  assert.equal(bot.querySelector(".message-avatar").textContent, "B");
  assert.equal(bot.querySelector(".message-avatar").dataset.role, "assistant");
  assert.ok(participant.querySelector(".message-bubble"));
  assert.ok(bot.querySelector(".message-bubble"));
  bundle.events[0].message.role = "assistant";
  assert.ok(ui.panel.native(bundle).querySelector(".input-record").classList.contains("message-right"));
  bundle.events[0].message = { content: "scheduled context" };
  const host = ui.panel.native(bundle, { background: true }).querySelector(".input-record");
  assert.ok(host.classList.contains("message-system"));
  assert.match(host.querySelector(".message-speaker").textContent, /\u5bbf\u4e3b/);
});

test("reply target is compact and immediately precedes its own reply text inside the message bubble", () => {
  const ui = page();
  const event = input("reply", "My answer");
  event.message.reply_to = { speaker: "Bob", role: "participant", content: "Original question" };
  ui.panel.render([event]);
  const bubble = ui.cards()[0].querySelector(".message-bubble");
  const message = bubble.querySelector(".content-message");
  const reply = message.querySelector(".message-reply-preview");
  assert.equal(reply.parentElement, message);
  assert.match(reply.textContent, /Bob.*Original question/);
  assert.match(reply.nextElementSibling.textContent, /My answer/);
  assert.equal(reply.open, false);
  reply.open = true;
  reply.querySelector("summary").focus();
  assert.match(reply.querySelector(".message-reply-body").textContent, /Original question/);
  ui.panel.refresh([event]);
  const refreshed = ui.cards()[0].querySelector(".message-reply-preview");
  assert.equal(refreshed.open, true);
  assert.equal(ui.document.activeElement, refreshed.querySelector("summary"));
  assert.equal(bubble.querySelector(".content-fields"), null);
  assert.equal(bubble.querySelector(".content-quotes"), null);
});

test("merged chats remain collapsed cards and expand to role-directed ordinary nested messages", () => {
  const ui = page();
  const event = input("forward", "Read this record");
  event.message.forwarded_messages = [
    { speaker: "Bot", role: "participant", content: "A participant with the same display name" },
    { speaker: "Recorded assistant", role: "assistant", content: "A bot reply",
      reply_to: { speaker: "Bot", role: "participant", content: "Previous message" },
      forwarded_messages: [{ speaker: "Nested member", role: "participant", content: "Inner record" }] },
  ];
  ui.panel.render([event]);
  const card = ui.cards()[0].querySelector(".message-forward");
  assert.equal(card.open, false);
  assert.match(card.querySelector("summary").textContent, /\u804a\u5929\u8bb0\u5f55/);
  assert.match(card.querySelector(".message-forward-preview").textContent, /A participant/);
  card.open = true; card.dispatchEvent(new Event("toggle"));
  const rows = card.querySelectorAll(".forwarded-message");
  assert.equal(rows.length, 3);
  assert.ok(rows[0].classList.contains("message-left"));
  assert.ok(rows[1].classList.contains("message-right"));
  assert.ok(rows[2].classList.contains("message-left"));
  assert.ok(rows.every((row) => row.querySelector(".message-avatar") && row.querySelector(".message-bubble")));
  assert.equal(rows[1].querySelector(".message-reply-preview").nextElementSibling.className, "content-message-body");
  const nested = rows[1].querySelector(".message-forward");
  nested.open = true;
  nested.querySelector("summary").focus();
  ui.panel.refresh([event]);
  const refreshed = ui.cards()[0].querySelectorAll(".message-forward");
  assert.ok(refreshed.every((item) => item.open));
  assert.equal(ui.document.activeElement, refreshed[1].querySelector("summary"));
  event.message.forwarded_messages = [event.message.forwarded_messages[0]];
  ui.panel.render([event]);
  assert.ok(ui.cards()[0].querySelector(".message-forward"));
  assert.equal(ui.cards()[0].querySelectorAll(".forwarded-message").length, 1);
  event.message.content = "";
  ui.panel.render([event]);
  const forwardOnly = ui.cards()[0].querySelector(".content-message");
  assert.equal(forwardOnly.querySelector(".content-message-body").textContent, "");
  assert.ok(forwardOnly.querySelector(".message-forward"));
});

test("legacy quotes never guess a merged forward from display names or quote count", () => {
  const ui = page();
  ui.panel.render([{ event_ref: "legacy-quote", event_type: "user_input", message: {
    speaker: "Alice", content: "Legacy reply", quotes: [
      { speaker: "Bot", role: "participant", content: "First quote" },
      { speaker: "Bob", role: "assistant", content: "Second quote" },
    ],
  } }]);
  const message = ui.cards()[0].querySelector(".content-message");
  assert.equal(message.querySelectorAll(".message-reply-preview").length, 2);
  assert.equal(message.querySelector(".message-forward"), null);
  assert.match(message.querySelector(".message-reply-speaker").textContent, /\u5f15\u7528/);
});

test("nested reply and forward images use their recorded association without replaying the event image catalog", () => {
  const ui = page();
  const image = (letter) => ({ name: `image-${letter}`, status: "captured", mime: "image/png", bytes: 128,
    url: `https://manage.example/api/llm-chat/sessions/events/image-input/attachments/input_${letter.repeat(32)}` });
  const [direct, quoted, forwarded] = [image("a"), image("b"), image("c")];
  const event = input("image-input", "Current message");
  event.images = [direct, quoted, forwarded];
  event.message.images = [direct];
  event.message.reply_to = { speaker: "Bob", role: "participant", content: "Quoted image", images: [quoted] };
  event.message.forwarded_messages = [{ speaker: "Carol", role: "participant", content: "Forwarded image", images: [forwarded] }];
  ui.panel.render([event]);
  const message = ui.cards()[0].querySelector(".content-message");
  const ownBody = message.children.find((child) => child.classList.contains("content-message-body"));
  assert.equal(ownBody.querySelectorAll(".image-button").length, 1);
  assert.equal(message.querySelector(".message-reply-preview").querySelectorAll(".image-button").length, 1);
  assert.equal(message.querySelector(".message-forward").querySelectorAll(".image-button").length, 1);
  assert.equal(message.querySelectorAll(".image-button").length, 3);
  assert.ok(message.querySelectorAll("img").every((item) => !item.src));
});

test("expanded review contains only a concise conclusion, contact outcome, commit state and real evidence links", () => {
  const ui = page();
  const review = ui.panel.review({
    review_ref: "review", status: "completed", decision_summary: "Wait until the participant is available",
    contact_proposed: true, contact: { target: "member", display_name: "Alice", purpose: "check_in", topic: "Ask about the project" },
    decision: { status: "reserved", reason: "contact_reserved", source: "RAW_GATE_METADATA" },
    transaction_committed: true, contacts: [{ attempt_ref: "contact", target: "member", display_name: "Alice", status: "confirmed", confirmed_count: 1,
      native_turn: { turn_ref: "contact-turn", session_ref: "contact-session" }, proposal: "RAW_CONTACT_SNAPSHOT" }],
    native_turn: { turn_ref: "native", session_ref: "native-session" },
    evidence_turns: [{ turn_ref: "evidence", session_ref: "ordinary", sequence: 2 }, { turn_ref: "invalid" }],
    events: { state_event_ref: "state", proposal_event_ref: null, decision_event_ref: "decision" },
    state: "RAW_STATE_SNAPSHOT", proposal: { secret: "RAW_PROPOSAL_SNAPSHOT" }, changes: "RAW_CHANGE_SNAPSHOT", life_version: 77,
  });
  assert.match(review.querySelector(".companion-decision-summary").textContent, /Wait until/);
  assert.match(review.querySelector(".companion-proposed-contact").textContent, /Alice.*Ask about the project/);
  assert.match(review.querySelector(".companion-contact-outcome").textContent, /1/);
  assert.match(review.querySelector(".companion-transaction").textContent, /\u5df2\u63d0\u4ea4/);
  assert.equal(review.querySelectorAll("button").length, 3);
  assert.ok(review.querySelectorAll("button").every((item) => !item.disabled));
  assert.equal(review.querySelectorAll("details").length, 0);
  assert.equal(review.querySelector(".companion-stages"), null);
  assert.equal(review.querySelector(".muted"), null);
  assert.doesNotMatch(review.textContent, /RAW_|life_version|1\. |2\. |3\. /);
});

test("review preserves explicit unknown and missing outcomes without disabled source buttons or invented zero receipts", () => {
  const ui = page();
  const review = ui.panel.review({ review_ref: "missing", status: "unknown", contacts_truncated: true,
    contacts: [{ attempt_ref: "unknown", status: "unknown", confirmed_count: null }] });
  assert.match(review.textContent, /\u672a\u77e5/);
  assert.match(review.textContent, /\u672a\u8bb0\u5f55/);
  assert.match(review.textContent, /\u539f\u751f\u8f6e\u6b21\u672a\u5173\u8054/);
  assert.ok(review.querySelectorAll(".partial-notice").length >= 3);
  assert.equal(review.querySelectorAll("button").length, 0);
  assert.equal(review.querySelector(".companion-contact-outcome"), null);
  const fallback = ui.panel.review(null, { decision_summary: "Captured conclusion" });
  assert.match(fallback.textContent, /\u72ec\u7acb\u590d\u76d8\u8bb0\u5f55\u7f3a\u5931/);
  assert.match(fallback.querySelector(".companion-transaction").textContent, /\u672a\u8bb0\u5f55/);
});

test("conversation suppresses only successful send calls matched to displayed confirmed deliveries", () => {
  const ui = page();
  const bundle = nativeBundle("send");
  const delivery = bundle.inspection.outputs[0];
  const matched = { tool_name: "send_msg", status: "succeeded", call_event_ref: "send-tool", deliveries: [delivery] };
  bundle.inspection.tool_calls = [matched,
    { ...matched, call_event_ref: "unmatched", deliveries: [{ ...delivery, event_ref: "not-displayed" }] },
    { ...matched, call_event_ref: "failed", status: "failed" },
    { ...matched, call_event_ref: "not-send", tool_name: "lookup" },
  ];
  const flow = ui.panel.native(bundle);
  assert.equal(flow.querySelectorAll(".tool-record").length, 3);
  assert.equal(flow.querySelectorAll(".delivery-record").length, 1);
  assert.ok(flow.querySelectorAll(".tool-record").every((item) => !item.open));
  assert.equal(ui.panel.calls(bundle).filter((item) => item.classList.contains("tool-record")).length, 4);
});

test("activity sorts numeric UTC instants and displays a review timestamp once", async () => {
  const ui = page();
  const reviewAt = "2026-10-29T04:02:40+00:00";
  const feed = activityPage(ui, { items: [
    { kind: "reminder", key: "reminder:a", session_ref: "ordinary", created_at: "2026-10-29T04:02:39.400000+00:00", reminder: { goal: "Later than its source" } },
    activityTurn("z-source", "2026-10-29T04:02:39+00:00"),
    { kind: "review", key: "review:r", scope_ref: "scope", session_ref: "ordinary", created_at: reviewAt, review: { review_ref: "r", started_at: reviewAt, status: "completed" } },
  ] });
  await feed.controller.open(feed.target, { session_ref: "ordinary" }); await flush();
  assert.deepEqual(feed.target.querySelectorAll(".activity-item").map((item) => item.dataset.recordKey), ["turn:z-source", "reminder:a", "review:r"]);
  const heading = feed.target.querySelector(".activity-review summary");
  const timestamp = ui.panel.formatDate(reviewAt);
  assert.equal(heading.textContent.split(timestamp).length - 1, 1);
});

test("confirmed output uses recorded speaker and keeps source-owned reply images inside its bubble", () => {
  const ui = page();
  const bundle = nativeBundle("output-structure");
  const quoted = { name: "quoted attachment", status: "captured", mime: "image/png", bytes: 128,
    url: `https://manage.example/api/llm-chat/sessions/events/source-input/attachments/input_${"d".repeat(32)}` };
  bundle.inspection.outputs[0] = { ...bundle.inspection.outputs[0], content: "Legacy fallback", images: [quoted],
    message: { speaker: "Recorded bot", role: "assistant", content: "My reply", images: [],
      reply_to: { event_ref: "source-input", speaker: "Alice", role: "participant", content: "The quoted message", images: [quoted],
        forwarded_messages: [{ speaker: "Original member", role: "participant", content: "Quoted merged record" }] },
    },
  };
  const output = ui.panel.native(bundle).querySelector(".delivery-record");
  assert.equal(output.querySelector(".message-speaker").textContent, "Recorded bot");
  assert.ok(output.classList.contains("message-right"));
  const reply = output.querySelector(".message-reply-preview");
  assert.equal(reply.querySelectorAll(".image-button").length, 1);
  assert.ok(reply.querySelector(".message-forward"));
  assert.equal(reply.nextElementSibling.className, "content-message-body");
  assert.match(reply.nextElementSibling.textContent, /My reply/);
  assert.equal(output.querySelectorAll(".image-button").length, 1);
});

test("review native action opens full request inspection rather than revealing its compact review again", async () => {
  const ui = page();
  const opened = [];
  const renderer = ui.companion.create({ ...ui.panel.hooks, number: String,
    eventButton: (_ref, title) => ui.panel.hooks.button(title, () => {}),
    selectEvidenceTurn: () => assert.fail("Native call detail must not only reveal the review"),
    openNative: (ref) => opened.push(ref),
  });
  const review = renderer.review({ review_ref: "native-review", status: "completed", contacts: [],
    native_turn: { turn_ref: "native-review-turn", session_ref: "reflection-session" },
    events: { state_event_ref: "state", proposal_event_ref: "proposal", decision_event_ref: "decision" },
  });
  const actions = review.querySelectorAll("button");
  assert.equal(actions.length, 1);
  assert.match(actions[0].textContent, /\u8c03\u7528\u8be6\u60c5/);
  await click(actions[0]);
  assert.deepEqual(opened, ["native-review-turn"]);
  const fallback = renderer.review({ review_ref: "event-only", contacts: [],
    events: { state_event_ref: "state", proposal_event_ref: "proposal", decision_event_ref: "decision" } });
  assert.equal(fallback.querySelectorAll("button").length, 1);
  assert.match(fallback.querySelector("button").textContent, /\u539f\u59cb\u8bb0\u5f55/);
});
