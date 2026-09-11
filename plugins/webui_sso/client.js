"use strict";

(() => {
  const origin = window.location.origin;
  let leaving = false;
  let logoutRequested = false;

  function signInTarget() {
    const current = window.location.pathname === "/login"
      ? "/"
      : window.location.pathname + window.location.search;
    return "/oauth2/start?rd=" + encodeURIComponent(current);
  }

  function navigate(target) {
    if (leaving) return;
    leaving = true;
    window.location.replace(target);
  }

  function leavePasswordRoute() {
    if (!logoutRequested) navigate(signInTarget());
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
      if (isLogout) logoutRequested = true;
      this.addEventListener("loadend", () => {
        if (isLogout) {
          logoutRequested = false;
          if (this.status >= 200 && this.status < 300) {
            navigate("/signed-out");
            return;
          }
        }
        if (this.status === 401) leavePasswordRoute();
      }, { once: true });
    }
    return Reflect.apply(open, this, arguments);
  };

  window.addEventListener("popstate", () => {
    if (window.location.pathname === "/login") leavePasswordRoute();
  });
  if (window.location.pathname === "/login") leavePasswordRoute();
})();
