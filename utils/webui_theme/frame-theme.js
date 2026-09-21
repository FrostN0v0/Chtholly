"use strict";

(() => {
  if (window.parent === window) return;
  const parentOrigin = new URL(document.URL).origin;
  const receive = (event) => {
    if (event.source !== window.parent || event.origin !== parentOrigin) return;
    const message = event.data;
    if (message?.type !== "chtholly.theme") return;
    if (message.theme !== "light" && message.theme !== "dark") return;
    document.documentElement.dataset.uiTheme = message.theme;
  };
  const request = () => window.parent.postMessage({ type: "chtholly.theme-request" }, parentOrigin);
  window.addEventListener("message", receive);
  window.addEventListener("pageshow", request);
  request();
})();
