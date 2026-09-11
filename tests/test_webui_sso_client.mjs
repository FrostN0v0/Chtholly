import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const client = readFileSync(new URL("../plugins/webui_sso/client.js", import.meta.url), "utf8");
const origin = "https://manage.example";

function page({ path = "/plugins?tab=config", visibility = "visible" } = {}) {
  const navigations = [];
  const requests = [];
  const document = new EventTarget();
  document.visibilityState = visibility;
  const window = new EventTarget();
  window.location = new URL(origin + path);
  window.location.replace = (target) => navigations.push(target);
  window.history = {};
  for (const name of ["pushState", "replaceState"]) {
    window.history[name] = (data, unused, url) => {
      if (url != null) window.location.href = new URL(url, window.location.href).href;
    };
  }
  class XMLHttpRequest extends EventTarget {
    open(method, url) {
      this.method = method;
      this.url = url;
    }
    send(body) {
      requests.push({ method: this.method, url: this.url, body });
    }
    complete(status) {
      this.status = status;
      this.dispatchEvent(new Event("loadend"));
    }
  }
  runInNewContext(client, { window, document, XMLHttpRequest, URL });
  return {
    navigations,
    requests,
    window,
    request(method = "GET", url = "/api/health", body) {
      const xhr = new XMLHttpRequest();
      xhr.open(method, url);
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
