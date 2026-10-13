import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const app = readFileSync(new URL("../plugins/llm_chat/webui_sessions/app.js", import.meta.url), "utf8");
const content = readFileSync(new URL("../plugins/llm_chat/webui_sessions/content.js", import.meta.url), "utf8");
const companion = readFileSync(new URL("../plugins/llm_chat/webui_sessions/companion.js", import.meta.url), "utf8");

function page() {
  function descendants(element) {
    return element.children.flatMap((child) => [child, ...descendants(child)]);
  }
  function matches(element, selector) {
    const parts = selector.trim().split(/\s+/);
    const last = parts.pop();
    const own = (item, part) => {
      if (part === "[data-focus-key]") return Boolean(item.dataset.focusKey);
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
    get textContent() { return (this.ownText || "") + this.children.map((child) => child.textContent).join(""); }
    set textContent(value) { this.ownText = String(value); this.replaceChildren(); }
    append(...children) {
      for (const child of children) {
        if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((item) => item !== child);
        this.children.push(child);
        child.parentElement = this;
      }
    }
    replaceChildren(...children) {
      for (const child of this.children) child.parentElement = null;
      this.children = [];
      this.append(...children);
    }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key]; }
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
    window, document, URL, AbortController, AbortSignal, DOMException,
    IntersectionObserver: Observer, MutationObserver: Observer,
    fetch: () => new Promise(() => {}),
  });
  runInContext(content, sandbox);
  sandbox.SessionContent = window.SessionContent;
  runInContext(companion, sandbox);
  sandbox.SessionCompanion = window.SessionCompanion;
  runInContext(app + `
    renderTurns = () => {};
    renderSession = () => {};
    renderWorkspace = () => renderIO();
    state.turn = { user_name: "Alice", status: "running" };
    state.inspection = { turn: state.turn, outputs: [] };
    globalThis.panel = {
      render(events) { state.events = events; renderIO(); },
      refresh(events) { state.events = events; refreshPanels(); },
      formatDate: date,
    };
  `, sandbox);
  return { panel: sandbox.panel, cards: () => document.getElementById("io-view").querySelectorAll(".input-record") };
}

function input(ref, content, steering) {
  return {
    event_ref: ref, event_type: "user_input", preview: content,
    message: { speaker: "Alice", content, quotes: [], mentions: [], truncated: false },
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
