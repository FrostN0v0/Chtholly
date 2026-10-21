import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const markup = readFileSync(new URL("../plugins/plugin_workshop/webui/index.html", import.meta.url), "utf8");
const client = readFileSync(new URL("../plugins/plugin_workshop/webui/app.js", import.meta.url), "utf8");
const origin = "https://manage.example";
const source = "# <script>globalThis.sourceExecuted = true</script>\nVALUE = 'new'\n";
const log = "<img src=x onerror=globalThis.logExecuted=true>";

function fixture(version) {
  return {
    plugin_name: "read_probe", version, source_hash: String(version).repeat(64),
    created_at: `2026-10-21T00:00:0${version}`, validation_status: version === 1 ? "passed" : "failed",
    active: version === 1, ever_active: version === 1,
    // Historical responses must not revive obsolete approval labels or controls.
    approved_by: "legacy-approver", approved_at: "2020-01-01",
    manifest: {
      title: "Read probe", description: "<svg onload=globalThis.descriptionExecuted=true>",
      commands: ["read_probe"], permissions: ["<script>permission</script>"],
      data_description: "No changes from this page", configuration: { value: version },
      checks: [{ command: "read_probe", expected_contains: "probe", operator: false, repeatable: true }],
    },
    report: {
      status: version === 1 ? "passed" : "failed", source_hash: String(version).repeat(64),
      checks: [{ name: "native command", passed: version === 1, detail: "<b>inert detail</b>" }],
      log, framework_version: "0.19.1", python_version: "3.11.15", image_id: "sha256:fixture",
    },
  };
}

function page({ framed = false, unauthorized = false } = {}) {
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.attributes = {};
      this.dataset = {};
      this.listeners = new Map();
      this.scrollTop = this.scrollLeft = 0;
      this.className = "";
      this.hidden = false;
    }
    get textContent() { return (this.ownText || "") + this.children.map((child) => child.textContent).join(""); }
    set textContent(value) { this.ownText = String(value); this.replaceChildren(); }
    set innerHTML(value) { throw new Error("Workshop views must not interpret text as HTML"); }
    append(...children) {
      for (const child of children) { child.parentElement = this; this.children.push(child); }
    }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    setAttribute(key, value) {
      this.attributes[key] = String(value);
      if (key === "id") this.id = String(value);
      if (key === "class") this.className = String(value);
      if (key === "hidden") this.hidden = true;
      if (key.startsWith("data-")) this.dataset[key.slice(5)] = String(value);
    }
    getAttribute(key) { return this.attributes[key]; }
    addEventListener(type, listener) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(listener);
    }
    async emit(type, values = {}) {
      if (this.disabled) return;
      await Promise.all((this.listeners.get(type) || []).map((listener) => listener({
        target: this, preventDefault() {}, ...values,
      })));
    }
    focus() { document.activeElement = this; }
    querySelectorAll(selector) {
      const matches = (node, part) => {
        if (part.startsWith("#")) return node.id === part.slice(1);
        if (part.startsWith(".")) return node.className.split(/\s+/).includes(part.slice(1));
        if (part === "[data-tab]") return Boolean(node.dataset.tab);
        return node.tagName === part.toUpperCase();
      };
      const selected = (node, selector) => {
        const parts = selector.trim().split(/\s+/);
        if (!matches(node, parts.pop())) return false;
        for (const part of parts.reverse()) {
          node = node.parentElement;
          while (node && !matches(node, part)) node = node.parentElement;
          if (!node) return false;
        }
        return true;
      };
      const descendants = (node) => node.children.flatMap((child) => [child, ...descendants(child)]);
      return descendants(this).filter((node) => selector.split(",").some((part) => selected(node, part)));
    }
  }

  const root = new Element("document");
  const stack = [root];
  for (const match of markup.matchAll(/<!--[^]*?-->|<![^>]*>|<\/?([a-z][\w-]*)([^>]*)>/gi)) {
    if (!match[1]) continue;
    if (match[0].startsWith("</")) { stack.pop(); continue; }
    const node = new Element(match[1]);
    for (const attribute of match[2].matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) {
      node.setAttribute(attribute[1], attribute[2] || "");
    }
    stack.at(-1).append(node);
    if (!["META", "INPUT", "LINK", "BR", "HR", "IMG"].includes(node.tagName)) stack.push(node);
  }
  const document = {
    URL: `${origin}/api/plugin-workshop/page`, hidden: false,
    createElement: (tag) => new Element(tag),
    getElementById: (id) => root.querySelectorAll(`#${id}`)[0] || null,
    querySelectorAll: (selector) => root.querySelectorAll(selector),
  };
  const requests = [];
  const responses = (url) => {
    const path = new URL(url, origin).pathname;
    if (unauthorized) return { success: false, code: "authentication_required", message: "Sign in" };
    if (path.endsWith("/status")) return { success: true, item: { ready: true, sandbox: { available: false, reason: "read fixture" } } };
    if (path.endsWith("/projects")) return { success: true, items: [{ plugin_name: "read_probe", enabled: true, active_version: 1 }] };
    if (path.endsWith("/versions")) return { success: true, items: [fixture(2), fixture(1)] };
    if (path.endsWith("/source")) return { success: true, item: { "__init__.py": path.includes("/versions/1/") ? "VALUE = 'old'\n" : source } };
    if (/\/versions\/[12]$/.test(path)) return { success: true, item: fixture(Number(path.at(-1))) };
    throw new Error(`Unexpected read: ${url}`);
  };
  const messages = [];
  const intervals = [];
  const listeners = new Map();
  const timers = new Map();
  let nextTimer = 0;
  const window = {
    scrollX: 0, scrollY: 0, scrollTo() {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { intervals.push(callback); },
  };
  window.parent = framed ? {
    postMessage(message, targetOrigin) {
      assert.equal(targetOrigin, origin);
      requests.push(message.payload);
      messages.push(message);
      queueMicrotask(() => {
        const receive = listeners.get("message");
        receive({ source: {}, origin, data: { id: message.id, error: "wrong sender" } });
        receive({ source: window.parent, origin: "https://attacker.example", data: { id: message.id, error: "wrong origin" } });
        receive({ source: window.parent, origin, data: { id: message.id, result: responses(message.payload.url) } });
      });
    },
  } : window;
  const context = createContext({
    window, document, URL, TextEncoder, crypto: { randomUUID },
    fetch: async (url, options) => {
      assert.equal(framed, false, "Opaque frames must use the native parent bridge");
      requests.push({ url, ...options });
      return {
        ok: !unauthorized, status: unauthorized ? 401 : 200,
        headers: new Map([["content-type", "application/json"]]),
        json: async () => responses(url),
      };
    },
  });
  runInContext(client + "\nglobalThis.workshopIdle = () => !state.busy;", context);
  async function idle() {
    for (let attempt = 0; attempt < 30; attempt++) {
      await new Promise((resolve) => setImmediate(resolve));
      if (context.workshopIdle()) return;
    }
    assert.fail("Workshop refresh did not settle");
  }
  return { document, root, requests, messages, timers, intervals, context, idle };
}

test("workshop renders history, report, source and diff as inert read-only text", async () => {
  const ui = page();
  await ui.idle();
  const get = ui.document.getElementById;
  assert.equal(get("source-code").textContent, source);
  assert.match(get("source-diff").textContent, /-VALUE = 'old'/);
  assert.match(get("source-diff").textContent, /\+VALUE = 'new'/);
  assert.equal(get("report-log").textContent, log);
  assert.equal(get("description").textContent, fixture(2).manifest.description);
  assert.equal(get("version-meta").textContent, "创建于 2026-10-21T00:00:02 · 尚未激活");
  assert.equal(ui.document.querySelectorAll("form, dialog, input, textarea, script, img, iframe").length, 0);
  assert.equal(ui.context.sourceExecuted, undefined);
  assert.equal(ui.context.logExecuted, undefined);
  assert.equal(get("versions").children.length, 2);

  await get("tab-source").emit("click");
  assert.equal(get("source-view").hidden, false);
  assert.equal(get("summary-view").hidden, true);
  await get("tab-source").emit("keydown", { key: "ArrowRight" });
  assert.equal(get("report-view").hidden, false);
  await get("versions").children[1].emit("click");
  await ui.idle();
  assert.equal(get("source-code").textContent, "VALUE = 'old'\n");
  assert.match(get("version-title").textContent, /v1/);
  await get("refresh").emit("click");
  await ui.idle();
  assert.ok(ui.requests.length > 8);
  for (const request of ui.requests) {
    assert.equal(request.method, "GET");
    assert.equal(request.body, undefined);
  }
});

test("opaque iframe bridge only requests reads and rejects forged parent responses", async () => {
  const ui = page({ framed: true });
  await ui.idle();
  assert.equal(ui.document.getElementById("source-code").textContent, source);
  assert.equal(ui.document.getElementById("connection").textContent, "已认证 · 已连接");
  assert.equal(ui.timers.size, 0);
  for (const message of ui.messages) {
    assert.equal(message.method, "api");
    assert.equal(message.payload.method, "GET");
    assert.equal(message.payload.data, undefined);
    assert.equal(message.payload.headers, undefined);
  }
});

test("authentication failures do not request or display confidential source", async () => {
  const ui = page({ unauthorized: true });
  await ui.idle();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.document.getElementById("source-code").textContent, "");
  assert.match(ui.document.getElementById("notice").textContent, /Sign in/);
  assert.equal(ui.document.getElementById("connection").textContent, "读取失败");
});
