import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const client = readFileSync(new URL("../plugins/webui_sso/client.js", import.meta.url), "utf8");
const origin = "https://manage.example";

function page({ path = "/plugins?tab=config", visibility = "visible" } = {}) {
  const navigations = [];
  const requests = [];
  const requestObjects = [];
  const sockets = [];
  const timers = new Map();
  const observers = [];
  let clock = 0;
  let nextTimer = 0;
  let offline = null;
  function element(textContent = "") {
    return {
      textContent,
      parentNode: null,
      children: [],
      setAttribute(name, value) { this[name] = value; },
      appendChild(child) {
        child.remove();
        this.children.push(child);
        child.parentNode = this;
      },
      remove() {
        if (this.parentNode) {
          this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
          this.parentNode = null;
        }
      },
    };
  }
  const main = element();
  const document = new EventTarget();
  document.body = element();
  document.visibilityState = visibility;
  document.createElement = () => element();
  document.querySelector = (selector) => selector === ".el-main" ? main : offline;
  class MutationObserver {
    constructor(callback) { observers.push(callback); }
    observe() {}
  }
  const window = new EventTarget();
  window.location = new URL(origin + path);
  window.location.replace = (target) => navigations.push(target);
  window.setTimeout = (callback, delay) => {
    const id = ++nextTimer;
    timers.set(id, { callback, due: clock + delay });
    return id;
  };
  window.clearTimeout = (id) => timers.delete(id);
  window.history = {};
  for (const name of ["pushState", "replaceState"]) {
    window.history[name] = (data, unused, url) => {
      if (url != null) window.location.href = new URL(url, window.location.href).href;
    };
  }
  class XMLHttpRequest extends EventTarget {
    timeout = 0;
    completed = false;
    open(method, url) {
      this.completed = false;
      this.method = method;
      this.url = url;
    }
    send(body) {
      requests.push({ method: this.method, url: this.url, body });
      requestObjects.push(this);
      if (this.timeout) this.deadline = window.setTimeout(() => this.complete(0), this.timeout);
    }
    complete(status) {
      if (this.completed) return;
      this.completed = true;
      window.clearTimeout(this.deadline);
      this.status = status;
      this.dispatchEvent(new Event("loadend"));
    }
    abort() { this.complete(0); }
  }
  class WebSocket extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;
    readyState = WebSocket.CONNECTING;
    constructor(url, protocols) {
      super();
      this.url = url;
      this.protocols = protocols;
      sockets.push(this);
    }
    accept() {
      this.readyState = WebSocket.OPEN;
      this.dispatchEvent(new Event("open"));
    }
    disconnect(code = 1006) {
      this.readyState = WebSocket.CLOSED;
      const event = new Event("close");
      Object.defineProperty(event, "code", { value: code });
      this.dispatchEvent(event);
    }
    close() { this.disconnect(1000); }
  }
  window.WebSocket = WebSocket;
  runInNewContext(client, { window, document, XMLHttpRequest, MutationObserver, URL });
  return {
    navigations,
    requests,
    requestObjects,
    sockets,
    window,
    status() {
      return [main, offline].filter(Boolean).flatMap((node) => node.children)
        .filter((node) => node.role === "status").map((node) => node.textContent);
    },
    showNativeOffline(text) {
      offline = element(text);
      for (const observer of observers) observer();
      return offline;
    },
    tick(milliseconds) {
      const until = clock + milliseconds;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
        if (!next) break;
        timers.delete(next[0]);
        clock = next[1].due;
        next[1].callback();
      }
      clock = until;
    },
    request(method = "GET", url = "/api/health", body, timeout = 2000) {
      const xhr = new XMLHttpRequest();
      xhr.open(method, url);
      xhr.timeout = timeout;
      xhr.send(body);
      return xhr;
    },
    visibility(value) {
      document.visibilityState = value;
      document.dispatchEvent(new Event("visibilitychange"));
    },
    pageshow(persisted) {
      const event = new Event("pageshow");
      Object.defineProperty(event, "persisted", { value: persisted });
      window.dispatchEvent(event);
    },
  };
}

test("hidden API failures defer one document visit until visible without replaying writes", () => {
  const tab = page({ visibility: "hidden" });
  tab.request("PUT", "/api/config", "changed config").complete(401);
  tab.request().complete(401);
  tab.visibility("hidden");
  assert.deepEqual(tab.navigations, []);
  tab.visibility("visible");
  tab.request().complete(401);
  tab.visibility("visible");
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
  assert.deepEqual(tab.requests.filter(({ method }) => method === "PUT"), [
    { method: "PUT", url: "/api/config", body: "changed config" },
  ]);
});

test("visible expiry revisits the management document so the gateway can reuse renewed SSO", () => {
  const tab = page({ path: "/plugins/settings?next=https%3A%2F%2Felsewhere.example#section" });
  tab.request().complete(401);
  assert.deepEqual(tab.navigations, ["/plugins/settings?next=https%3A%2F%2Felsewhere.example"]);
});

test("intercepted password navigation stays deferred and preserves the management return target", () => {
  const tab = page({ visibility: "hidden" });
  tab.window.history.pushState({}, "", "/login?redirect=/elsewhere");
  tab.window.history.replaceState({}, "", "/login");
  assert.deepEqual(tab.navigations, []);
  tab.visibility("visible");
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
});

test("password and network-path locations cannot become unsafe reauthentication targets", () => {
  const login = page({ path: "/login?redirect=https://elsewhere.example", visibility: "hidden" });
  assert.deepEqual(login.navigations, []);
  login.visibility("visible");
  assert.deepEqual(login.navigations, ["/"]);
  const networkPath = page({ path: "//elsewhere.example/private" });
  networkPath.request().complete(401);
  assert.deepEqual(networkPath.navigations, ["/"]);
});

test("unrelated origins and non-API responses neither reauthenticate nor reserve logout", () => {
  const tab = page();
  tab.request("GET", "https://elsewhere.example/api/health").complete(401);
  tab.request("POST", "https://elsewhere.example/api/auth/logout").complete(204);
  tab.request("POST", "https://elsewhere.example/api/auth/logout");
  tab.request("GET", "/assets/app.js").complete(401);
  assert.deepEqual(tab.navigations, []);
  tab.request().complete(401);
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
});

test("successful native logout wins over pending and late API failures and router redirects", () => {
  const tab = page({ visibility: "hidden" });
  tab.request().complete(401);
  const logout = tab.request("POST", "/api/auth/logout");
  tab.visibility("visible");
  tab.request().complete(401);
  tab.window.history.pushState({}, "", "/login");
  assert.deepEqual(tab.navigations, []);
  logout.complete(204);
  tab.request().complete(401);
  tab.window.history.replaceState({}, "", "/login");
  tab.visibility("visible");
  assert.deepEqual(tab.navigations, ["/signed-out"]);
});

test("failed logout releases deferred reauthentication only after other logouts settle", () => {
  const tab = page();
  const first = tab.request("POST", "/api/auth/logout");
  const second = tab.request("POST", "/api/auth/logout");
  tab.request().complete(401);
  first.complete(500);
  assert.deepEqual(tab.navigations, []);
  second.complete(403);
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
});

test("BFCache recovery clears a cached leaving guard but still waits for a visible document", () => {
  const tab = page();
  tab.pageshow(false);
  assert.deepEqual(tab.navigations, []);
  tab.request().complete(401);
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
  tab.visibility("hidden");
  tab.pageshow(true);
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
  tab.visibility("visible");
  tab.visibility("visible");
  assert.deepEqual(tab.navigations, ["/plugins?tab=config", "/plugins?tab=config"]);
});

test("BFCache restores recheck previously healthy UI and never revive a completed logout", () => {
  const healthy = page();
  healthy.pageshow(true);
  assert.deepEqual(healthy.navigations, ["/plugins?tab=config"]);
  const loggedOut = page();
  loggedOut.request("POST", "/api/auth/logout").complete(200);
  loggedOut.pageshow(true);
  loggedOut.request().complete(401);
  assert.deepEqual(loggedOut.navigations, ["/signed-out", "/signed-out"]);
});

test("slow authenticated health succeeds without extending or replaying write deadlines", () => {
  const tab = page();
  const health = tab.request();
  tab.tick(3000);
  health.complete(200);
  assert.deepEqual(tab.status(), []);
  assert.deepEqual(tab.navigations, []);
  tab.request("PUT", "/api/config", "change", 2000);
  tab.tick(2000);
  assert.equal(tab.status().length, 1);
  assert.deepEqual(tab.requests.filter(({ method }) => method === "PUT"), [
    { method: "PUT", url: "/api/config", body: "change" },
  ]);
});

test("transport outages retain native offline state and expose an inline recoverable status", () => {
  const tab = page();
  tab.request().complete(503);
  const native = tab.showNativeOffline("native disconnected state");
  assert.equal(native.textContent, "native disconnected state");
  assert.equal(native.children.length, 1);
  assert.match(tab.status()[0], /503/);
  assert.deepEqual(tab.navigations, []);
  tab.request().complete(200);
  assert.equal(native.textContent, "native disconnected state");
  assert.deepEqual(tab.status(), []);
});

test("failed native upgrades share a real HTTP bootstrap with backoff and no extra sockets", () => {
  const tab = page();
  const chat = new tab.window.WebSocket("wss://manage.example/api/chat");
  const logs = new tab.window.WebSocket("wss://manage.example/ws/logs");
  chat.disconnect();
  logs.disconnect();
  tab.tick(0);
  assert.deepEqual(tab.requests, [{ method: "GET", url: "/api/webui-sso/session", body: undefined }]);
  tab.requestObjects[0].complete(503);
  tab.tick(999);
  assert.equal(tab.requests.length, 1);
  tab.tick(1);
  tab.requestObjects[1].complete(0);
  tab.tick(1999);
  assert.equal(tab.requests.length, 2);
  tab.tick(1);
  tab.requestObjects[2].complete(204);
  assert.deepEqual(tab.status(), []);
  assert.deepEqual(tab.navigations, []);
  assert.equal(tab.sockets.length, 2);
});

test("bootstrap authentication expiry defers gateway navigation until visible", () => {
  const tab = page();
  new tab.window.WebSocket("wss://manage.example/ws/logs").disconnect();
  tab.tick(0);
  tab.visibility("hidden");
  tab.requestObjects[0].complete(401);
  assert.deepEqual(tab.navigations, []);
  tab.visibility("visible");
  assert.deepEqual(tab.navigations, ["/plugins?tab=config"]);
});

test("manual socket closure and logout cannot create or revive deferred bootstraps", () => {
  const tab = page({ visibility: "hidden" });
  new tab.window.WebSocket("wss://manage.example/api/chat").close();
  tab.visibility("visible");
  tab.tick(0);
  assert.deepEqual(tab.requests, []);
  tab.visibility("hidden");
  new tab.window.WebSocket("wss://manage.example/ws/logs").disconnect();
  tab.tick(30000);
  assert.deepEqual(tab.requests, []);
  tab.visibility("visible");
  tab.tick(0);
  assert.equal(tab.requests.length, 1);
  tab.request("POST", "/api/auth/logout").complete(204);
  tab.tick(60000);
  assert.equal(tab.requests.length, 2);
  assert.deepEqual(tab.navigations, ["/signed-out"]);
});

test("repeated native duplicate rejection does not become repeated login or auth traffic", () => {
  const tab = page();
  new tab.window.WebSocket("wss://manage.example/api/chat").disconnect();
  tab.tick(0);
  tab.requestObjects[0].complete(204);
  new tab.window.WebSocket("wss://manage.example/api/chat").disconnect();
  tab.tick(3000);
  assert.equal(tab.requests.length, 1);
  assert.deepEqual(tab.navigations, []);
  assert.equal(tab.sockets.length, 2);
});
