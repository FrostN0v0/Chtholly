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

  function leavePasswordRoute() {
    if (leaving) return;
    leaving = true;
    const target = logoutRequested
      ? "/oauth2/sign_out?rd=" + encodeURIComponent(origin + "/signed-out")
      : signInTarget();
    window.location.replace(target);
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

  // Observe the stable logout API; never read or modify credentials or request bodies.
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    const target = new URL(url, window.location.href);
    if (String(method).toUpperCase() === "POST" && target.origin === origin && target.pathname === "/api/auth/logout") {
      logoutRequested = true;
      this.addEventListener("loadend", () => {
        if (this.status < 200 || this.status >= 300) logoutRequested = false;
      }, { once: true });
    }
    return Reflect.apply(open, this, arguments);
  };

  window.addEventListener("popstate", () => {
    if (window.location.pathname === "/login") leavePasswordRoute();
  });
  if (window.location.pathname === "/login") leavePasswordRoute();
})();
