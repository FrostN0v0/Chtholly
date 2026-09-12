"use strict";

(() => {
  const origin = window.location.origin;
  let leaving = false;
  let reauthenticationNeeded = false;
  let pendingLogouts = 0;
  let loggedOut = false;
  const readTimeout = 25000;
  let bootstrapNeeded = false;
  let bootstrapTimer = null;
  let bootstrapRequest = null;
  let bootstrapDelay = 0;
  let notice = null;
  let noticeStatus = null;

  function renderNotice() {
    if (noticeStatus === null) {
      if (notice) notice.remove();
      notice = null;
      return;
    }
    const container = document.querySelector(".offline-box, .reconnecting-banner") || document.querySelector(".el-main");
    if (!container) return;
    if (!notice) {
      notice = document.createElement("p");
      notice.setAttribute("role", "status");
    }
    const text = noticeStatus === 0
      ? "网络连接中断或请求超时，正在等待恢复。不会自动重新登录或重试配置写入。"
      : `后端或认证服务暂不可用（HTTP ${noticeStatus}）。不会自动重新登录或重试配置写入。`;
    if (notice.textContent !== text) notice.textContent = text;
    if (notice.parentNode !== container) container.appendChild(notice);
  }

  function connectionNotice(status) {
    noticeStatus = status;
    renderNotice();
  }

  function watchConnectionStatus() {
    renderNotice();
    // Native Vue owns this overlay. Move only our explanatory detail when it
    // mounts; never overwrite its wording or fabricate its health state.
    new MutationObserver(renderNotice).observe(document.body, { childList: true, subtree: true });
  }
  if (document.body) watchConnectionStatus();
  else document.addEventListener("DOMContentLoaded", watchConnectionStatus, { once: true });

  function cancelBootstrap() {
    if (bootstrapTimer !== null) window.clearTimeout(bootstrapTimer);
    bootstrapTimer = null;
    bootstrapNeeded = false;
    if (bootstrapRequest) bootstrapRequest.abort();
  }

  function resumeBootstrap() {
    if (!bootstrapNeeded || bootstrapRequest || bootstrapTimer !== null || leaving || loggedOut || pendingLogouts
      || document.visibilityState !== "visible") return;
    bootstrapTimer = window.setTimeout(() => {
      bootstrapTimer = null;
      if (leaving || loggedOut || pendingLogouts || document.visibilityState !== "visible") return;
      const request = new XMLHttpRequest();
      bootstrapRequest = request;
      request.open("GET", "/api/webui-sso/session");
      request.timeout = readTimeout;
      request.addEventListener("loadend", () => {
        bootstrapRequest = null;
        if (!bootstrapNeeded || leaving || loggedOut || pendingLogouts) return;
        if (request.status >= 200 && request.status < 300) {
          bootstrapNeeded = false;
          // Repeated rejected upgrades may be native Chat's one-socket-per-
          // session rule, not expired auth. Do not turn them into an auth flood.
          bootstrapDelay = 30000;
          connectionNotice(null);
        } else if (request.status === 0 || request.status === 429 || request.status >= 500) {
          bootstrapDelay = Math.min(30000, Math.max(1000, bootstrapDelay * 2));
          resumeBootstrap();
        } else {
          bootstrapNeeded = false;
          if (request.status !== 401) connectionNotice(request.status);
        }
      }, { once: true });
      request.send();
    }, bootstrapDelay);
  }

  function managementTarget() {
    const pathname = window.location.pathname;
    if (pathname === "/login" || !pathname.startsWith("/") || pathname.startsWith("//") || pathname.includes("\\")) {
      return "/";
    }
    return pathname + window.location.search;
  }

  function navigate(target) {
    if (leaving) return;
    leaving = true;
    window.location.replace(target);
  }

  function resumeReauthentication() {
    if (reauthenticationNeeded && !loggedOut && pendingLogouts === 0 && document.visibilityState === "visible") {
      // Visit the gateway again: another tab may already have renewed its SSO session.
      navigate(managementTarget());
    }
  }

  function leavePasswordRoute() {
    if (loggedOut) return;
    reauthenticationNeeded = true;
    resumeReauthentication();
  }

  // Native WebUI navigates to its password page through Vue Router, not HTTP redirects.
  for (const name of ["pushState", "replaceState"]) {
    const original = window.history[name];
    window.history[name] = function (data, unused, url) {
      if (url != null) {
        const target = new URL(url, window.location.href);
        if (target.origin === origin && target.pathname === "/login") {
          leavePasswordRoute();
          return;
        }
      }
      return Reflect.apply(original, this, arguments);
    };
  }

  // Router navigation can stall while loading its now-protected login chunk.
  // React to the API result first, without reading credentials or request bodies.
  const open = XMLHttpRequest.prototype.open;
  const healthRequests = new WeakSet();
  XMLHttpRequest.prototype.open = function (method, url) {
    const target = new URL(url, window.location.href);
    healthRequests.delete(this);
    if (target.origin === origin && target.pathname === "/api/health" && String(method).toUpperCase() === "GET") {
      healthRequests.add(this);
    }
    if (target.origin === origin && target.pathname.startsWith("/api/")) {
      const isLogout = String(method).toUpperCase() === "POST" && target.pathname === "/api/auth/logout";
      if (isLogout) {
        pendingLogouts += 1;
        cancelBootstrap();
      }
      this.addEventListener("loadend", () => {
        if (isLogout) {
          pendingLogouts = Math.max(0, pendingLogouts - 1);
          if (this.status >= 200 && this.status < 300) {
            loggedOut = true;
            reauthenticationNeeded = false;
            navigate("/signed-out");
            return;
          }
        }
        if (loggedOut || (this === bootstrapRequest && !bootstrapNeeded)) return;
        if (this.status === 401) {
          connectionNotice(null);
          leavePasswordRoute();
        } else if (isLogout) resumeReauthentication();
        if (this.status === 0 || this.status === 429 || this.status >= 500) connectionNotice(this.status);
        else if (this.status >= 200 && this.status < 300 && target.pathname === "/api/health") connectionNotice(null);
      }, { once: true });
    }
    return Reflect.apply(open, this, arguments);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    // Axios sets timeout after open(). Change only this read's deadline, not
    // its response: native health otherwise aborts after 2s, before auth.
    if (healthRequests.has(this)) this.timeout = readTimeout;
    return Reflect.apply(send, this, arguments);
  };

  if (typeof window.WebSocket === "function") {
    const NativeWebSocket = window.WebSocket;
    const clientClosed = new WeakSet();
    window.WebSocket = class extends NativeWebSocket {
      constructor() {
        super(...arguments);
        const target = new URL(this.url);
        if (target.protocol !== "wss:" || target.host !== window.location.host
          || !["/api/chat", "/ws/logs"].includes(target.pathname)) return;
        this.addEventListener("open", () => {
          bootstrapDelay = 0;
        });
        this.addEventListener("close", () => {
          if (clientClosed.has(this) || leaving || loggedOut || pendingLogouts) return;
          // Native stores own their reconnect timer. Only repair the cookie
          // through authenticated HTTP; never create/replay a socket or write.
          bootstrapNeeded = true;
          resumeBootstrap();
        });
      }

      close() {
        clientClosed.add(this);
        return super.close(...arguments);
      }
    };
  }

  document.addEventListener("visibilitychange", () => {
    resumeReauthentication();
    resumeBootstrap();
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    // BFCache restores the old guards as well as the UI; recheck at the gateway.
    leaving = false;
    pendingLogouts = 0;
    cancelBootstrap();
    if (loggedOut) navigate("/signed-out");
    else leavePasswordRoute();
  });

  window.addEventListener("popstate", () => {
    if (window.location.pathname === "/login") leavePasswordRoute();
  });
  if (window.location.pathname === "/login") leavePasswordRoute();
})();
