(() => {
  "use strict";
  const root = document.documentElement;
  const mobile = window.matchMedia("(max-width: 760px)");
  const frames = () => [...document.querySelectorAll(".panel-host iframe")].filter((frame) => {
    try { return new URL(frame.src, location.href).origin === location.origin; } catch { return false; }
  });
  const sendTheme = (frame) => {
    // A sandboxed frame has an opaque origin; only the selected local frame receives appearance data.
    frame.contentWindow?.postMessage({ type: "chtholly.theme", theme: root.classList.contains("dark") ? "dark" : "light" }, "*");
  };
  const broadcast = () => frames().forEach(sendTheme);
  const receive = (event) => {
    if (event.origin !== "null" && event.origin !== location.origin) return;
    if (event.data?.type !== "chtholly.theme-request") return;
    const frame = frames().find((candidate) => candidate.contentWindow === event.source);
    if (frame) sendTheme(frame);
  };
  const loaded = (event) => {
    if (event.target instanceof HTMLIFrameElement && frames().includes(event.target)) sendTheme(event.target);
  };
  const enhance = () => {
    const header = document.querySelector(".header");
    const sidebar = document.querySelector(".sidebar");
    if (!header || !sidebar) return;
    const controls = header.querySelectorAll(":scope > button");
    const collapse = controls[0];
    if (collapse) {
      collapse.setAttribute("aria-label", "\u5207\u6362\u5bfc\u822a\u680f");
      collapse.title = "\u5207\u6362\u5bfc\u822a\u680f";
      collapse.setAttribute("aria-expanded", String(!sidebar.querySelector(".el-menu--collapse")));
      if (mobile.matches && sidebar.querySelector(".el-menu--collapse")) collapse.click();
    }
    if (controls[1]) {
      controls[1].setAttribute("aria-label", "\u5207\u6362\u6df1\u6d45\u4e3b\u9898");
      controls[1].title = "\u5207\u6362\u6df1\u6d45\u4e3b\u9898";
    }
    const items = [...sidebar.querySelectorAll(".el-menu-item:not(.is-disabled)")];
    const active = items.find((item) => item.classList.contains("is-active")) || items[0];
    for (const item of items) {
      item.tabIndex = item === active ? 0 : -1;
      if (item === active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    let breadcrumb = header.querySelector(".workspace-breadcrumb");
    if (!breadcrumb) {
      breadcrumb = document.createElement("div");
      breadcrumb.className = "workspace-breadcrumb";
      const workspace = document.createElement("span");
      workspace.textContent = "\u5de5\u4f5c\u53f0";
      const separator = document.createElement("span");
      separator.className = "separator";
      separator.textContent = "/";
      separator.setAttribute("aria-hidden", "true");
      breadcrumb.append(workspace, separator, document.createElement("strong"));
      collapse?.after(breadcrumb);
    }
    const current = sidebar.querySelector(".el-menu-item.is-active")?.textContent.trim() || "\u63a7\u5236\u53f0";
    const label = breadcrumb.querySelector("strong");
    if (label.textContent !== current) label.textContent = current;
  };
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => { scheduled = false; enhance(); });
  };
  const appearance = new MutationObserver(broadcast);
  appearance.observe(root, { attributes: true, attributeFilter: ["class"] });
  const navigation = new MutationObserver((records) => {
    if (records.some((record) => record.type === "childList" || record.target.matches(".el-menu, .el-menu-item"))) schedule();
  });
  const start = () => {
    const app = document.getElementById("app");
    if (app) navigation.observe(app, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    enhance();
    broadcast();
  };
  window.addEventListener("message", receive);
  document.addEventListener("load", loaded, true);
  document.addEventListener("click", schedule);
  mobile.addEventListener("change", schedule);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    appearance.disconnect();
    navigation.disconnect();
    window.removeEventListener("message", receive);
    document.removeEventListener("load", loaded, true);
    document.removeEventListener("click", schedule);
    mobile.removeEventListener("change", schedule);
  });
})();
