(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const MIN_VALUE = 0;
  const MAX_VALUE = 100;
  const AXES = [
    { key: "affection", label: "\u597d\u611f" },
    { key: "trust", label: "\u4fe1\u4efb" },
    { key: "dependence", label: "\u4f9d\u8d56" },
    { key: "resentment", label: "\u6028\u5ff5" },
    { key: "familiarity", label: "\u719f\u6089\u5ea6" },
  ];
  let chartId = 0;

  function make(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined && value !== null) item.textContent = String(value);
    return item;
  }

  function makeSvg(tag, attributes = {}) {
    const item = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (value !== undefined && value !== null) item.setAttribute(name, String(value));
    }
    return item;
  }

  function finiteNumber(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function valueState(value) {
    const number = finiteNumber(value);
    if (number === null) return { known: false, value: null, reason: "\u672a\u77e5" };
    if (number < MIN_VALUE || number > MAX_VALUE) {
      return { known: false, value: null, reason: "\u8d85\u51fa\u8303\u56f4" };
    }
    return { known: true, value: number, reason: "" };
  }

  function sourceAxes(source) {
    if (!source || typeof source !== "object" || Array.isArray(source)) return {};
    if (source.axes && typeof source.axes === "object" && !Array.isArray(source.axes)) return source.axes;
    return source;
  }

  function axisStates(source) {
    const values = sourceAxes(source);
    return Object.fromEntries(AXES.map(({ key }) => [key, valueState(values[key])]));
  }

  function formatNumber(value) {
    const number = finiteNumber(value);
    return number === null ? "\u672a\u77e5" : String(number);
  }

  function formatDelta(after, before) {
    if (!after.known || !before.known) return "\u672a\u77e5";
    const delta = Number((after.value - before.value).toFixed(12));
    const prefix = delta > 0 ? "+" : "";
    return `${prefix}${formatNumber(delta)}`;
  }

  function point(index, radius, centerX, centerY) {
    const angle = -Math.PI / 2 + (index * Math.PI * 2) / AXES.length;
    return [centerX + Math.cos(angle) * radius, centerY + Math.sin(angle) * radius];
  }

  function pointsFor(states, radius, centerX, centerY) {
    return AXES.map((axis, index) => {
      const [x, y] = point(index, radius * (states[axis.key].known ? states[axis.key].value / MAX_VALUE : 0), centerX, centerY);
      return { axis, state: states[axis.key], x, y };
    });
  }

  function pointList(points) {
    return points.map(({ x, y }) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  }

  function appendRadarSeries(svg, states, className, label, centerX, centerY, radius) {
    const points = pointsFor(states, radius, centerX, centerY);
    if (points.every(({ state }) => state.known)) {
      svg.append(makeSvg("polygon", { class: className, points: pointList(points), "aria-label": label }));
    }
    for (const { axis, state, x, y } of points) {
      if (!state.known) continue;
      svg.append(makeSvg("circle", { class: `${className}-point chart-axis-${axis.key}`, cx: x, cy: y, r: 3.5 }));
    }
  }

  function appendRadar(svg, after, before, hasBefore, headingId, descriptionId, afterLabel, beforeLabel) {
    const centerX = 180;
    const centerY = 148;
    const radius = 92;
    const svgTitleId = `${headingId}-svg-title`;
    const svgDescriptionId = `${descriptionId}-svg-description`;
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-labelledby", `${svgTitleId} ${svgDescriptionId}`);
    const title = makeSvg("title", { id: svgTitleId });
    title.textContent = "\u4e94\u8f74\u5173\u7cfb\u96f7\u8fbe\u56fe";
    const description = makeSvg("desc", { id: svgDescriptionId });
    description.textContent = "\u6570\u503c\u8303\u56f4\u4e3a 0 \u5230 100\uff1b\u672a\u77e5\u8f74\u4e0d\u53c2\u4e0e\u591a\u8fb9\u5f62\u8fde\u63a5\u3002";
    svg.append(title, description);

    for (const level of [20, 40, 60, 80, 100]) {
      const gridPoints = AXES.map((_, index) => {
        const [x, y] = point(index, radius * level / MAX_VALUE, centerX, centerY);
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      }).join(" ");
      svg.append(makeSvg("polygon", { class: "chart-radar-grid", points: gridPoints }));
    }
    for (let index = 0; index < AXES.length; index += 1) {
      const [x, y] = point(index, radius, centerX, centerY);
      svg.append(makeSvg("line", { class: "chart-radar-axis", x1: centerX, y1: centerY, x2: x, y2: y }));
    }
    for (const [level, labelText] of [[0, "0"], [50, "50"], [100, "100"]]) {
      const label = makeSvg("text", { class: "chart-radar-scale", x: centerX + 5, y: centerY - radius * level / MAX_VALUE - 2 });
      label.textContent = labelText;
      svg.append(label);
    }
    appendRadarSeries(svg, after, "chart-radar-after", `${afterLabel}\u5173\u7cfb`, centerX, centerY, radius);
    if (hasBefore) appendRadarSeries(svg, before, "chart-radar-before", `${beforeLabel}\u5173\u7cfb`, centerX, centerY, radius);
  }

  function addLegend(root, hasBefore, afterLabel, beforeLabel) {
    const legend = make("div", "chart-legend");
    const current = make("span", "chart-legend-item");
    current.append(make("span", "chart-legend-swatch chart-legend-current"), make("span", "", afterLabel));
    legend.append(current);
    if (hasBefore) {
      const previous = make("span", "chart-legend-item");
      previous.append(make("span", "chart-legend-swatch chart-legend-before"), make("span", "", beforeLabel));
      legend.append(previous);
    }
    root.append(legend);
  }

  function relationship(axes, before = null) {
    const id = `chart-relationship-${chartId++}`;
    const afterStates = axisStates(axes);
    const hasBefore = before !== null && before !== undefined;
    const beforeStates = hasBefore ? axisStates(before) : null;
    const afterLabel = hasBefore ? "\u8bc4\u4f30\u540e" : "\u751f\u6210\u524d\u5feb\u7167";
    const beforeLabel = "\u8bc4\u4f30\u524d";
    const root = make("figure", "chart chart-relationship");
    const caption = make("figcaption", "chart-note", "0\u2013100 \u00b7 \u6028\u5ff5\u8d8a\u9ad8\u8868\u793a\u4e0d\u6ee1\u8d8a\u5f3a");
    caption.id = `${id}-caption`;
    root.setAttribute("aria-describedby", caption.id);
    root.setAttribute("aria-label", "\u5173\u7cfb\u8f74\u53d8\u5316");
    root.append(caption);
    addLegend(root, hasBefore, afterLabel, beforeLabel);
    const frame = make("div", "chart-radar-frame");
    const radar = makeSvg("svg", { class: "chart-radar", viewBox: "0 0 360 300", width: 360, height: 300 });
    appendRadar(radar, afterStates, beforeStates, hasBefore, id, caption.id, afterLabel, beforeLabel);
    frame.append(radar);
    for (const axis of AXES) {
      const after = afterStates[axis.key];
      const prior = beforeStates?.[axis.key];
      const label = make("div", `chart-axis-label chart-axis-${axis.key}`);
      label.append(make("strong", "chart-axis-name", axis.label));
      const afterText = after.known ? formatNumber(after.value) : after.reason;
      const values = make("div", "chart-axis-values");
      if (hasBefore) {
        values.append(make("span", "chart-value-before", prior.known ? formatNumber(prior.value) : prior.reason));
        values.append(make("span", "chart-value-arrow", "\u2192"));
      }
      values.append(make("span", "chart-value-after", afterText));
      label.append(values);
      if (hasBefore) label.append(make("span", "chart-axis-delta", `\u53d8\u5316 ${formatDelta(after, prior)}`));
      frame.append(label);
    }
    root.append(frame);
    return root;
  }

  function tokenState(value) {
    const number = finiteNumber(value);
    return number !== null && Number.isSafeInteger(number) && number >= 0 ? number : null;
  }

  function tokenText(value) {
    return value === null ? "\u672a\u77e5" : value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function coverageText(coverage) {
    if (!coverage || typeof coverage !== "object" || Array.isArray(coverage)) return "\u8986\u76d6\u8303\u56f4\u672a\u77e5";
    if (coverage.complete === false) return "\u90e8\u5206\u7edf\u8ba1";
    if (coverage.complete === true) return "\u5df2\u5b8c\u6574\u7edf\u8ba1";
    return "\u8986\u76d6\u8303\u56f4\u672a\u77e5";
  }

  function usage(usageValue) {
    const usageData = usageValue && typeof usageValue === "object" && !Array.isArray(usageValue) ? usageValue : {};
    const id = `chart-usage-${chartId++}`;
    const headingId = `${id}-heading`;
    const root = make("section", "chart chart-usage");
    root.setAttribute("aria-labelledby", headingId);
    const heading = make("h3", "chart-heading", "Token \u7528\u91cf");
    heading.id = headingId;
    root.append(heading);
    const status = make("span", "chart-coverage", coverageText(usageData.coverage));
    root.append(status);

    const input = tokenState(usageData.input_tokens);
    const output = tokenState(usageData.output_tokens);
    const metrics = make("dl", "chart-token-metrics");
    for (const [key, label] of [["input_tokens", "\u8f93\u5165"], ["output_tokens", "\u8f93\u51fa"], ["total_tokens", "\u5408\u8ba1"], ["cached_input_tokens", "\u7f13\u5b58\u8f93\u5165"], ["reasoning_tokens", "\u63a8\u7406"]]) {
      const value = tokenState(usageData[key]);
      const metric = make("div", "chart-token-metric");
      metric.append(make("dt", "chart-token-label", label), make("dd", "chart-token-value", tokenText(value)));
      const missing = tokenState(usageData.coverage?.fields?.[key]?.unknown);
      if (missing > 0) metric.append(make("span", "chart-note", `\u7f3a ${tokenText(missing)} \u6761\u8bb0\u5f55`));
      metrics.append(metric);
    }
    root.append(metrics);

    const composition = make("div", "chart-token-composition");
    composition.append(make("p", "chart-note", "\u7f13\u5b58 / \u63a8\u7406\u4e3a\u5b50\u9879"));
    const track = make("div", "chart-usage-track");
    track.setAttribute("aria-hidden", "true");
    if (input !== null && output !== null && input + output > 0) {
      const inputBar = make("span", "chart-usage-input");
      inputBar.style.width = `${input / (input + output) * 100}%`;
      const outputBar = make("span", "chart-usage-output");
      outputBar.style.width = `${output / (input + output) * 100}%`;
      track.append(inputBar, outputBar);
      composition.append(make("p", "chart-note", "\u5df2\u8bb0\u5f55\u8f93\u5165 / \u8f93\u51fa\u6784\u6210"));
    }
    if (track.children.length) composition.append(track);
    else composition.append(make("p", "chart-unknown", input === 0 && output === 0 ? "\u5df2\u8bb0\u5f55\u8f93\u5165 / \u8f93\u51fa\u5747\u4e3a 0" : "\u8f93\u5165 / \u8f93\u51fa\u672a\u5b8c\u6574\u8bb0\u5f55\uff0c\u6bd4\u4f8b\u672a\u77e5"));
    const legend = make("div", "chart-legend");
    for (const [className, label] of [["chart-legend-input", "\u8f93\u5165"], ["chart-legend-output", "\u8f93\u51fa"]]) {
      const item = make("span", "chart-legend-item");
      item.append(make("span", `chart-legend-swatch ${className}`), make("span", "", label));
      legend.append(item);
    }
    composition.append(legend);
    root.append(composition);
    return root;
  }

  globalThis.SessionCharts = Object.freeze({ relationship, usage });
})();
