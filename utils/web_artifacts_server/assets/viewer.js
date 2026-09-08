(() => {
  "use strict";

  const configNode = document.getElementById("artifact-config");
  let config = {};
  try {
    config = JSON.parse(configNode ? configNode.textContent || "{}" : "{}");
  } catch (_) {
    config = {};
  }

  const get = (id) => document.getElementById(id);
  const titleNode = get("artifact-title");
  const subtitleNode = get("artifact-subtitle");
  const frame = get("artifact-frame");
  const frameShell = get("frame-shell");
  const viewportNode = get("viewport-label");
  const statusNode = get("frame-status");
  const expiryNode = get("artifact-expiry");
  const detailsNode = get("artifact-details");
  const downloadNode = get("source-download");

  const title =
    typeof config.title === "string" && config.title.trim()
      ? config.title
      : "Artifact preview";
  const entry =
    typeof config.entry === "string" && config.entry
      ? config.entry
      : "index.html";
  const version = Number.isFinite(Number(config.version))
    ? String(config.version)
    : "?";
  const artifactPrefix =
    typeof config.artifact_prefix === "string" ? config.artifact_prefix : "";
  const filePrefix =
    typeof config.file_prefix === "string" ? config.file_prefix : "";

  const safeUrl = (candidate, prefix) => {
    if (
      typeof candidate !== "string" ||
      !candidate ||
      typeof prefix !== "string" ||
      !prefix
    )
      return "about:blank";
    try {
      const url = new URL(candidate, location.href);
      const allowed = new URL(prefix, location.href);
      if (
        url.origin !== allowed.origin ||
        !url.pathname.startsWith(allowed.pathname)
      )
        return "about:blank";
      return url.href;
    } catch (_) {
      return "about:blank";
    }
  };

  const entryUrl = safeUrl(config.entry_url, filePrefix);
  const sourceUrl = safeUrl(config.source_url, artifactPrefix);

  titleNode.textContent = title;
  subtitleNode.textContent = `Version ${version} · ${entry}`;
  downloadNode.href = sourceUrl;
  downloadNode.setAttribute("download", "source.zip");

  const fileCount = Number(config.file_count);
  const sourceBytes = Number(config.source_bytes);
  const formatBytes = (value) => {
    if (!Number.isFinite(value) || value < 0) return "unknown size";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
  };
  const countText =
    Number.isInteger(fileCount) && fileCount >= 0
      ? `${fileCount} files`
      : "files unavailable";
  detailsNode.textContent = `${countText} · ${formatBytes(sourceBytes)} source · immutable version ${version}`;

  const expiry = Number(config.expires_at);
  if (Number.isFinite(expiry) && expiry > 0) {
    try {
      expiryNode.textContent = `Expires ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(expiry * 1000))}`;
    } catch (_) {
      expiryNode.textContent = "Expiring preview";
    }
  } else {
    expiryNode.textContent = "Expiring preview";
  }

  const setMode = (mode) => {
    const mobile = mode === "mobile";
    frameShell.classList.toggle("is-mobile", mobile);
    viewportNode.textContent = mobile
      ? "Mobile · responsive"
      : "Desktop · responsive";
    document.querySelectorAll("[data-viewport]").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.getAttribute("data-viewport") === mode),
      );
    });
  };

  document.querySelectorAll("[data-viewport]").forEach((button) => {
    button.addEventListener("click", () =>
      setMode(button.getAttribute("data-viewport") || "desktop"),
    );
  });

  const loadEntry = (reload) => {
    if (entryUrl === "about:blank") {
      statusNode.textContent = "Unavailable";
      statusNode.classList.add("is-error");
      return;
    }
    const url = new URL(entryUrl);
    if (reload) url.searchParams.set("_reload", String(Date.now()));
    frame.setAttribute("src", url.href);
    statusNode.textContent = "Loading";
    statusNode.classList.remove("is-error");
  };

  get("reload-preview").addEventListener("click", () => loadEntry(true));
  frame.addEventListener("load", () => {
    statusNode.textContent = "Ready";
    statusNode.classList.remove("is-error");
  });
  frame.addEventListener("error", () => {
    statusNode.textContent = "Unavailable";
    statusNode.classList.add("is-error");
  });

  setMode("desktop");
  loadEntry(false);
})();
