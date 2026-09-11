"use strict";

(() => {
  const origin = window.location.origin;
  let leaving = false;
  let reauthenticationNeeded = false;
  let pendingLogouts = 0;
  let loggedOut = false;

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
  XMLHttpRequest.prototype.open = function (method, url) {
    const target = new URL(url, window.location.href);
    if (target.origin === origin && target.pathname.startsWith("/api/")) {
      const isLogout = String(method).toUpperCase() === "POST" && target.pathname === "/api/auth/logout";
      if (isLogout) pendingLogouts += 1;
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
        if (this.status === 401) leavePasswordRoute();
        else if (isLogout) resumeReauthentication();
      }, { once: true });
    }
    return Reflect.apply(open, this, arguments);
  };

  document.addEventListener("visibilitychange", resumeReauthentication);
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    // BFCache restores the old guards as well as the UI; recheck at the gateway.
    leaving = false;
    pendingLogouts = 0;
    if (loggedOut) navigate("/signed-out");
    else leavePasswordRoute();
  });

  window.addEventListener("popstate", () => {
    if (window.location.pathname === "/login") leavePasswordRoute();
  });
  if (window.location.pathname === "/login") leavePasswordRoute();
})();
