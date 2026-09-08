(() => {
  "use strict";
  const singleton = "__chthollyConfigApplyPanel";
  const detailed = location.pathname === "/api/config-apply/page";
  if ((!detailed && window.top !== window) || window[singleton]) return;
  const base = "/api/config-apply";
  const nativeFetch = window.fetch;
  const xhrOpen = XMLHttpRequest.prototype.open;
  const xhrSend = XMLHttpRequest.prototype.send;
  const xhrRequests = new WeakMap();
  const controllers = new Set();
  const digest = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value) ? value : null;
  const requestId = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value) ? value : null;
  const activeStates = new Set(["checking", "restarting", "verifying"]);
  const failureStates = new Set(["rejected", "rolled_back", "rollback_failed"]);
  let disposed = false;
  let paused = false;
  let timer = null;
  let polling = false;
  let pollAgain = false;
  let deadline = 0;
  let generation = 0;
  let target = null;
  let restartRequest = null;
  let restarting = false;
  let lastStatus = null;
  let stickyError = "";
  let authBlocked = false;
  let host;
  let root;
  let title;
  let message;
  let error;
  let detail;
  let restartButton;
  let refreshButton;

  function text(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  function show(label, description, tone = "pending") {
    if (!root || disposed) return;
    root.querySelector(".ca-panel").dataset.tone = tone;
    text(title, label);
    text(message, description);
    text(error, stickyError);
    error.hidden = !stickyError;
    restartButton.disabled = restarting || authBlocked || !lastStatus?.available ||
      Boolean(restartRequest) || activeStates.has(lastStatus?.state?.result);
  }

  function stopPolling() {
    clearTimeout(timer);
    timer = null;
  }

  function beginPolling() {
    if (disposed || paused) return;
    stopPolling();
    deadline = Date.now() + 180000;
    if (polling) pollAgain = true;
    else void poll();
  }

  function schedule() {
    if (disposed || paused) return;
    if (Date.now() >= deadline) {
      show("等待超时 / Timed out", "尚未确认应用成功。请检查服务状态后点击“刷新状态”；不会自动再次发起重启。", "error");
      return;
    }
    timer = setTimeout(() => void poll(), 2000);
  }

  async function readJson(response) {
    // Only our status/control and native save responses enter here; never read
    // request bodies, unrelated API responses, or configuration GET responses.
    if (!response.headers.get("content-type")?.includes("application/json")) {
      throw new Error(`服务器未返回 JSON（HTTP ${response.status}），请检查登录状态。`);
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("服务器响应为空。");
    let content = "";
    let size = 0;
    const decoder = new TextDecoder();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 16384) throw new Error("服务器响应超出状态接口限制。");
        content += decoder.decode(value, { stream: true });
      }
      content += decoder.decode();
      const data = JSON.parse(content);
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("服务器响应格式无效。");
      return data;
    } finally {
      void reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  function serverError(data, status) {
    const value = data?.message ?? data?.detail ?? data?.code;
    return typeof value === "string" ? value.slice(0, 500) : `请求失败（HTTP ${status}）。`;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await nativeFetch.call(window, `${base}${path}`, {
        ...options,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: { "X-Requested-With": "ChthollyConfigApply", ...options.headers },
        signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403) {
        authBlocked = true;
        throw new Error(`需要登录或同源访问权限（HTTP ${response.status}）。未确认任何重启。`);
      }
      const data = await readJson(response);
      if (!response.ok || data.success !== true) throw new Error(serverError(data, response.status));
      authBlocked = false;
      return data;
    } finally {
      clearTimeout(timeout);
      controllers.delete(controller);
    }
  }

  function renderStatus(status) {
    const running = digest(status.running_sha256);
    const saved = digest(status.saved_sha256);
    const state = status.state && typeof status.state === "object" ? status.state : {};
    const candidate = digest(state.candidate_sha256);
    const matchingRequest = !restartRequest || state.request_id === restartRequest;
    const relevant = matchingRequest && (!target || candidate === target);
    const stateLabel = typeof state.result === "string" ? state.result : "unknown";
    const reason = typeof state.reason === "string" ? state.reason.slice(0, 500) : "";
    const updated = typeof state.updated_at === "string" ? state.updated_at.slice(0, 80) : "—";
    text(detail, `运行摘要: ${running || "不可用"}\n保存摘要: ${saved || "不可用"}\n本次目标: ${target || "—"}\n服务阶段: ${stateLabel}\n更新时间: ${updated}${restartRequest ? `\n重启请求: ${restartRequest}` : ""}`);
    if (!status.available) {
      show("不可用 / Unavailable", "配置应用服务不可用。不能从此处确认应用或发起 Bot 重启。", "error");
      return false;
    }
    if (restarting && !restartRequest) {
      show("正在提交 / Submitting", "正在请求完整 Bot 重启；尚未确认请求被接受。");
      return true;
    }
    if (relevant && failureStates.has(stateLabel)) {
      const labels = { rejected: "已拒绝 / Rejected", rolled_back: "已回滚 / Rolled back", rollback_failed: "回滚失败 / Rollback failed" };
      show(labels[stateLabel], reason || "候选配置未成功应用。请检查配置与服务状态；不会自动重试重启。", "error");
      restartRequest = null;
      restartButton.disabled = restarting || authBlocked;
      return false;
    }
    if (relevant && activeStates.has(stateLabel)) {
      const labels = { checking: "已保存，检查中 / Checking", restarting: "Bot 重启中 / Restarting", verifying: "已重连，验证中 / Verifying" };
      show(labels[stateLabel], "保存不等于生效。正在等待完整 Bot 重启与健康检查；LLBot 不会重启。");
      return true;
    }
    if (restartRequest && (!matchingRequest || !["applied", "unchanged"].includes(stateLabel))) {
      show("重启请求已提交 / Restart requested", "等待服务处理本次请求；尚未确认重启完成。可能短暂断开 20–30 秒。");
      return true;
    }
    if (running && saved && running === saved && status.in_sync === true && (!target || running === target)) {
      restartRequest = null;
      if (stickyError) {
        show("上次操作失败或未确认 / Operation unconfirmed", "当前磁盘与运行配置一致，但这不代表上次保存或重启请求成功。请查看下方错误。", "error");
      } else {
        show("已应用 / Applied", "运行配置摘要与已保存配置一致。", "ok");
      }
      return false;
    }
    if (target && saved && target !== saved) {
      show("目标未应用 / Not applied", "磁盘配置与本次保存目标不同，可能已被回滚或被后续保存替换；不宣称成功。", "error");
      return false;
    }
    show("已保存，待应用 / Saved · Pending", "磁盘保存与当前运行配置尚未一致。自动应用处理中，也可确认后执行完整 Bot 重启。");
    return true;
  }

  async function poll() {
    if (disposed || paused || polling) return;
    polling = true;
    const observedGeneration = generation;
    let repeat = false;
    try {
      const status = await api("/status");
      if (disposed || paused || observedGeneration !== generation) return;
      lastStatus = status;
      repeat = renderStatus(status);
    } catch (failure) {
      if (disposed || paused || observedGeneration !== generation) return;
      if (authBlocked) {
        show("需要授权 / Authorization required", failure.message, "error");
      } else {
        show("连接中断，正在重连 / Reconnecting", `${failure.message} 重启期间通常需要 20–30 秒；尚未确认应用成功。`, "error");
        repeat = true;
      }
    } finally {
      polling = false;
      if (!disposed && !paused && (pollAgain || observedGeneration !== generation)) {
        pollAgain = false;
        void poll();
      } else if (repeat) schedule();
    }
  }

  function nativeSaveUrl(input, method) {
    if (String(method || "GET").toUpperCase() !== "PUT") return false;
    try {
      const url = new URL(input, location.href);
      return url.origin === location.origin && (
        /^\/api\/plugins\/[^/]+\/config\/?$/.test(url.pathname) ||
        /^\/api\/config\/[^/]+\/?$/.test(url.pathname)
      );
    } catch (_) {
      return false;
    }
  }

  function saveResult(status, data) {
    if (disposed) return;
    generation += 1;
    if (status < 200 || status >= 300 || data?.success !== true) {
      stickyError = serverError(data, status);
      if (status === 401 || status === 403) authBlocked = true;
      show("保存失败 / Save failed", "服务器未确认保存成功；未因本次失败发起重启。", "error");
      stopPolling();
      return;
    }
    const candidate = digest(data.candidate_sha256);
    if (!candidate) {
      stickyError = "保存响应缺少有效配置摘要，无法确认本次保存是否生效。请检查配置保存插件。";
      show("保存未验证 / Save unverified", "没有目标摘要，不会显示本次保存已应用。", "error");
      stopPolling();
      return;
    }
    target = candidate;
    restartRequest = null;
    stickyError = "";
    authBlocked = false;
    show("已保存，待确认 / Saved · Pending", "配置已写入磁盘，正在比较当前运行配置；这不是重启完成通知。");
    beginPolling();
  }

  function saveObservationFailed(failure) {
    if (disposed) return;
    generation += 1;
    stopPolling();
    stickyError = failure.message || "无法读取保存结果。";
    show("保存结果未知 / Save unconfirmed", "未确认保存或应用成功。正在检查服务状态，不会自动重发保存请求。", "error");
    beginPolling();
  }

  function observedFetch(input, options) {
    const isRequest = typeof Request !== "undefined" && input instanceof Request;
    const watch = nativeSaveUrl(isRequest ? input.url : input, options?.method ?? (isRequest ? input.method : "GET"));
    const result = nativeFetch.apply(this, arguments);
    if (watch) void result.then(response => {
      void readJson(response.clone()).then(data => saveResult(response.status, data)).catch(saveObservationFailed);
    }, saveObservationFailed);
    return result;
  }

  function observedOpen(method, url) {
    xhrRequests.set(this, nativeSaveUrl(url, method));
    return xhrOpen.apply(this, arguments);
  }

  function observedSend() {
    if (xhrRequests.get(this)) {
      this.addEventListener("loadend", () => {
        try {
          if (this.responseType === "json") saveResult(this.status, this.response);
          else if ((!this.responseType || this.responseType === "text") && this.responseText.length <= 16384) {
            saveResult(this.status, JSON.parse(this.responseText));
          } else throw new Error("保存响应格式无法验证。");
        } catch (failure) {
          saveObservationFailed(failure);
        }
      }, { once: true });
    }
    return xhrSend.apply(this, arguments);
  }

  async function restart() {
    if (restartButton.disabled || !window.confirm("确认完整重启 Chtholly Bot？\n所有 Bot 会话将短暂中断，WebUI 通常断开 20–30 秒。\n仅重启 Bot，不重启 LLBot。")) return;
    restarting = true;
    stickyError = "";
    generation += 1;
    stopPolling();
    show("正在提交 / Submitting", "正在请求完整 Bot 重启；尚未确认请求被接受。");
    try {
      const data = await api("/restart", { method: "POST" });
      const id = requestId(data.request_id);
      if (!id) throw new Error("重启响应缺少有效请求标识；无法确认是否已提交，请勿立即重复点击。");
      restartRequest = id;
      target = digest(lastStatus?.saved_sha256);
      show("重启请求已提交 / Restart requested", "等待服务确认处理本次请求与运行配置一致；不会自动再次提交重启。");
      beginPolling();
    } catch (failure) {
      stickyError = failure.message;
      show("重启未确认 / Restart unconfirmed", "没有收到有效的成功响应。请刷新状态检查服务；不会自动重试或宣称已经重启。", "error");
    } finally {
      restarting = false;
      restartButton.disabled = authBlocked || !lastStatus?.available || Boolean(restartRequest) || activeStates.has(lastStatus?.state?.result);
    }
  }

  function refresh() {
    authBlocked = false;
    beginPolling();
  }

  function cleanup() {
    disposed = true;
    stopPolling();
    for (const controller of controllers) controller.abort();
    controllers.clear();
    if (window.fetch === observedFetch) window.fetch = nativeFetch;
    if (XMLHttpRequest.prototype.open === observedOpen) XMLHttpRequest.prototype.open = xhrOpen;
    if (XMLHttpRequest.prototype.send === observedSend) XMLHttpRequest.prototype.send = xhrSend;
    host?.remove();
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("pageshow", onPageShow);
    document.removeEventListener("DOMContentLoaded", mount);
    delete window[singleton];
  }

  function onPageHide(event) {
    paused = true;
    stopPolling();
    for (const controller of controllers) controller.abort();
    if (!event.persisted) cleanup();
  }

  function onPageShow(event) {
    paused = false;
    if (event.persisted) beginPolling();
  }

  function mount() {
    if (disposed) return;
    host = document.createElement("aside");
    host.id = "chtholly-config-apply";
    host.setAttribute("aria-label", "配置应用与 Bot 重启");
    root = host.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = `${base}/assets/panel.css`;
    root.append(stylesheet);
    const panel = document.createElement("section");
    panel.className = detailed ? "ca-panel ca-detailed" : "ca-panel";
    // Static markup only. Server text is assigned with textContent below.
    panel.innerHTML = `<div class="ca-heading"><strong>配置应用 / Configuration Apply</strong><button type="button" class="ca-collapse" aria-expanded="true" aria-controls="ca-content">收起</button></div>
      <div role="status" aria-live="polite" aria-atomic="true"><h2></h2><p class="ca-message"></p></div>
      <p class="ca-error" role="alert" hidden></p>
      <div id="ca-content"><div class="ca-actions"><button type="button" class="ca-refresh">刷新状态</button><button type="button" class="ca-restart" disabled>完整重启 Bot（不含 LLBot）</button></div>
      <details><summary>摘要与服务详情</summary><pre></pre></details>
      <p class="ca-note">保存后仅在运行摘要匹配时确认生效。新凭证须先由管理员配置环境变量；此面板不读取或保存表单值。</p>
      <a class="ca-link" href="/api/config-apply/page" target="_blank" rel="noopener">打开配置应用详情</a></div>`;
    root.append(panel);
    title = root.querySelector("h2");
    message = root.querySelector(".ca-message");
    error = root.querySelector(".ca-error");
    detail = root.querySelector("pre");
    restartButton = root.querySelector(".ca-restart");
    refreshButton = root.querySelector(".ca-refresh");
    restartButton.addEventListener("click", () => void restart());
    refreshButton.addEventListener("click", refresh);
    const collapse = root.querySelector(".ca-collapse");
    collapse.hidden = detailed;
    root.querySelector(".ca-link").hidden = detailed;
    root.querySelector("details").open = detailed;
    collapse.addEventListener("click", () => {
      const content = root.getElementById("ca-content");
      content.hidden = !content.hidden;
      collapse.setAttribute("aria-expanded", String(!content.hidden));
      text(collapse, content.hidden ? "展开" : "收起");
    });
    document.body.append(host);
    show("正在读取 / Connecting", "正在读取配置应用服务状态。");
    beginPolling();
  }

  window[singleton] = { dispose: cleanup };
  if (!detailed) {
    window.fetch = observedFetch;
    XMLHttpRequest.prototype.open = observedOpen;
    XMLHttpRequest.prototype.send = observedSend;
  }
  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("pageshow", onPageShow);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
})();
