"use strict";

(function defineSessionContent(global) {
  const FIELD_LABELS = Object.freeze({
    content: "\u5185\u5bb9",
    text: "\u6587\u672c",
    role: "\u89d2\u8272",
    speaker: "\u53d1\u8a00\u4eba",
    system: "\u7cfb\u7edf\u6307\u4ee4",
    messages: "\u6d88\u606f\u5217\u8868",
    message: "\u6d88\u606f",
    arguments: "\u53c2\u6570",
    args: "\u53c2\u6570",
    result: "\u7ed3\u679c",
    segments: "\u6bb5\u843d",
    status: "\u72b6\u6001",
    name: "\u540d\u79f0",
    type: "\u7c7b\u578b",
    tool: "\u5de5\u5177",
    tool_name: "\u5de5\u5177\u540d\u79f0",
    id: "\u6807\u8bc6",
    timestamp: "\u65f6\u95f4",
    created_at: "\u521b\u5efa\u65f6\u95f4",
    updated_at: "\u66f4\u65b0\u65f6\u95f4",
    error: "\u9519\u8bef",
    code: "\u4ee3\u7801",
    language: "\u8bed\u8a00",
    prompt: "\u63d0\u793a\u8bcd",
    description: "\u8bf4\u660e",
    metadata: "\u5143\u6570\u636e",
    payload: "\u8d1f\u8f7d",
    data: "\u6570\u636e",
    value: "\u503c",
    source: "\u6765\u6e90",
    truncated: "\u5df2\u622a\u65ad",
    quotes: "\u5f15\u7528\u4e0a\u4e0b\u6587",
    mentions: "\u63d0\u53ca",
    metadata_ref: "\u5143\u6570\u636e\u5f15\u7528",
    model: "\u6a21\u578b",
    provider: "\u63d0\u4f9b\u65b9",
    input: "\u8f93\u5165",
    output: "\u8f93\u51fa",
    choices: "\u5019\u9009\u56de\u590d",
    finish_reason: "\u7ed3\u675f\u539f\u56e0",
    tool_calls: "\u5de5\u5177\u8c03\u7528",
    media: "\u5a92\u4f53",
    image: "\u56fe\u7247",
    url: "\u5730\u5740",
    context: "\u4e0a\u4e0b\u6587",
    history: "\u5386\u53f2",
    user: "\u7528\u6237",
    assistant: "\u52a9\u624b",
    request: "\u8bf7\u6c42",
    response: "\u54cd\u5e94",
    tokens: "Token \u7edf\u8ba1",
    input_tokens: "\u8f93\u5165 Token",
    output_tokens: "\u8f93\u51fa Token",
    total_tokens: "\u603b Token",
    finish: "\u7ed3\u675f",
  });
  const ROLE_LABELS = Object.freeze({
    assistant: "\u52a9\u624b",
    user: "\u7528\u6237",
    tool: "\u5de5\u5177",
    system: "\u7cfb\u7edf",
    participant: "\u53c2\u4e0e\u8005",
    unknown: "\u672a\u77e5",
    text: "\u6587\u672c",
    mention: "\u63d0\u53ca",
    image: "\u56fe\u7247",
  });
  const MAX_INLINE_NODES = 220;
  function roleLabel(value) {
    const key = String(value ?? "").toLowerCase();
    return Object.hasOwn(ROLE_LABELS, key) ? ROLE_LABELS[key] : displayScalar(value);
  }

  const TEXT_FIELDS = new Set([
    "content", "text", "system", "result", "prompt", "description", "error",
  ]);
  const JSON_WRAPPERS = new Set(["data", "payload", "value", "body", "result", "response", "message"]);
  const MAX_DEPTH = 8;
  const MAX_NODES = 360;
  const MAX_ARRAY_ITEMS = 80;
  const MAX_OBJECT_FIELDS = 80;
  const MAX_TEXT_CHARS = 20000;
  const MAX_MARKDOWN_CHARS = 240000;
  const MAX_MARKDOWN_LINES = 700;

  function create(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function append(parent, ...children) {
    for (const child of children) if (child) parent.append(child);
    return parent;
  }

  function isObject(value) {
    return value !== null && typeof value === "object";
  }

  function isPlainObject(value) {
    return isObject(value) && !Array.isArray(value);
  }

  function displayScalar(value) {
    if (value === null) return "\u7a7a\u503c";
    if (value === undefined) return "\u672a\u8bb0\u5f55";
    if (typeof value === "boolean") return value ? "\u662f" : "\u5426";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "\u975e\u6709\u9650\u6570\u503c";
    if (typeof value === "bigint") return String(value);
    return String(value);
  }

  function fieldLabel(key) {
    return Object.hasOwn(FIELD_LABELS, key) ? FIELD_LABELS[key] : String(key);
  }

  function shortText(value, limit = 120) {
    const text = displayScalar(value).replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit)}\u2026` : text;
  }

  function exactSerialize(value) {
    try {
      const serialized = JSON.stringify(value, null, 2);
      return serialized === undefined ? displayScalar(value) : serialized;
    } catch (_) {
      return displayScalar(value);
    }
  }


  function parseCompleteJson(value) {
    if (typeof value !== "string") return value;
    const source = value.trim();
    if (!source || source.length > MAX_TEXT_CHARS * 8) return value;
    const first = source[0];
    const last = source[source.length - 1];
    if (!((first === "{" && last === "}") || (first === "[" && last === "]") || (first === '"' && last === '"'))) return value;
    try {
      const parsed = JSON.parse(source);
      if (typeof parsed === "string" && parsed !== value) return parseCompleteJson(parsed);
      return parsed;
    } catch (_) {
      return value;
    }
  }

  function unwrapJson(value, rounds = 0) {
    if (rounds >= 3 || !isPlainObject(value)) return value;
    const keys = Object.keys(value);
    if (keys.length !== 1) return value;
    const wrapper = keys[0];
    if (!JSON_WRAPPERS.has(wrapper)) return value;
    const candidate = parseCompleteJson(value[wrapper]);
    if (candidate === value[wrapper] && !isObject(candidate)) return value;
    return unwrapJson(candidate, rounds + 1);
  }

  function prepareValue(value) {
    let prepared = parseCompleteJson(value);
    if (isObject(prepared)) prepared = unwrapJson(prepared);
    return prepared;
  }

  function createRemainder(value, label = "\u5269\u4f59\u6570\u636e", note = "\u5df2\u6298\u53e0\uff0c\u6253\u5f00\u540e\u67e5\u770b\u4fdd\u7559\u6570\u636e") {
    const details = create("details", "content-remainder");
    const summary = create("summary", "content-remainder-summary", `${label} \u00b7 ${note}`);
    const pre = create("pre", "content-code");
    let loaded = false;
    details.addEventListener("toggle", () => {
      if (details.open && !loaded) {
        pre.textContent = exactSerialize(value);
        loaded = true;
      }
    });
    details.append(summary, pre);
    return details;
  }

  function createBudget() {
    return { nodes: 0 };
  }

  function spend(budget) {
    budget.nodes += 1;
    return budget.nodes <= MAX_NODES;
  }

  function plainText(text, className = "content-plain") {
    const source = String(text ?? "");
    if (source.length <= MAX_TEXT_CHARS) return create("span", className, source);
    const details = create("details", "content-remainder");
    details.append(
      create("summary", "content-remainder-summary", `\u6587\u672c\u8f83\u957f\uff08${source.length.toLocaleString()} \u5b57\u7b26\uff09\uff0c\u70b9\u51fb\u5c55\u5f00`),
      create("pre", "content-code", source),
    );
    return details;
  }

  function appendInline(parent, source, inlineState) {
    const text = String(source ?? "");
    const pattern = /(\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|https?:\/\/[^\s<>]+|`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|(?<![\w])\*[^*\n]+\*(?!\*)|(?<![\w])_[^_\n]+_(?!\w))/g;
    let cursor = 0;
    let match;
    while ((match = pattern.exec(text))) {
      if (inlineState && inlineState.nodes >= MAX_INLINE_NODES) {
        parent.append(createRemainder(text.slice(cursor), "\u5269\u4f59\u5185\u8054\u6587\u672c"));
        return;
      }
      if (inlineState) inlineState.nodes += 1;
      if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith("[")) {
        const link = create("a", "content-link", match[2]);
        link.href = match[3];
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        link.referrerPolicy = "no-referrer";
        parent.append(link);
      } else if (token.startsWith("http://") || token.startsWith("https://")) {
        const trailing = token.match(/[.,!?;:)]*$/)?.[0] || "";
        const href = trailing ? token.slice(0, -trailing.length) : token;
        const link = create("a", "content-link", href);
        link.href = href;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        link.referrerPolicy = "no-referrer";
        parent.append(link, document.createTextNode(trailing));
      } else if (token.startsWith("`")) {
        parent.append(create("code", "content-inline-code", token.slice(1, -1)));
      } else if (token.startsWith("**") || token.startsWith("__")) {
        parent.append(create("strong", "content-strong", token.slice(2, -2)));
      } else {
        parent.append(create("em", "content-emphasis", token.slice(1, -1)));
      }
      cursor = match.index + token.length;
    }
    if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
  }

  function renderCodeBlock(code, language) {
    const block = create("div", "content-code-block");
    const head = create("div", "content-code-head");
    const caption = create("span", "content-code-language", language || "\u4ee3\u7801");
    const copy = create("button", "content-copy-button", "\u590d\u5236");
    copy.type = "button";
    copy.addEventListener("click", () => {
      const done = () => {
        copy.textContent = "\u5df2\u590d\u5236";
        setTimeout(() => { copy.textContent = "\u590d\u5236"; }, 1200);
      };
      if (global.navigator?.clipboard?.writeText) {
        global.navigator.clipboard.writeText(code).then(done, () => { copy.textContent = "\u590d\u5236\u5931\u8d25"; });
      } else {
        copy.textContent = "\u6682\u4e0d\u652f\u6301";
      }
    });
    append(head, caption, copy);
    const pre = create("pre", "content-code");
    pre.append(create("code", "content-code-text", code));
    append(block, head, pre);
    return block;
  }

  function splitTableRow(line) {
    let source = line.trim();
    if (source.startsWith("|")) source = source.slice(1);
    if (source.endsWith("|")) source = source.slice(0, -1);
    return source.split("|").map((cell) => cell.trim());
  }

  function isTableSeparator(line) {
    return /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  }

  function renderTable(lines, inlineState) {
    const table = create("table", "content-table");
    const head = create("thead");
    const body = create("tbody");
    const columns = splitTableRow(lines[0]);
    const headRow = create("tr");
    for (const cell of columns) {
      const th = create("th");
      appendInline(th, cell, inlineState);
      headRow.append(th);
    }
    head.append(headRow);
    for (const line of lines.slice(2, 102)) {
      const row = create("tr");
      const cells = splitTableRow(line);
      for (let index = 0; index < columns.length; index += 1) {
        const td = create("td");
        appendInline(td, cells[index] ?? "", inlineState);
        row.append(td);
      }
      body.append(row);
    }
    table.append(head, body);
    const wrap = create("div", "content-table-wrap");
    wrap.append(table);
    if (lines.length > 102) wrap.append(createRemainder(lines.slice(102).join("\n"), "\u8868\u683c\u4f59\u4f59\u884c"));
    return wrap;
  }

  function markdown(text) {
    const source = String(text ?? "");
    const root = create("div", "content-markdown");
    if (!source) {
      root.append(create("p", "content-empty", "\u65e0\u5185\u5bb9"));
      return root;
    }
    const allLines = source.replace(/\r\n?/g, "\n").split("\n");
    const oversized = source.length > MAX_MARKDOWN_CHARS || allLines.length > MAX_MARKDOWN_LINES;
    let lines = allLines;
    let remainder = "";
    if (oversized) {
      let prefixEnd = Math.min(source.length, 30000);
      const newline = source.lastIndexOf("\n", prefixEnd);
      if (newline > 0) prefixEnd = newline;
      lines = source.slice(0, prefixEnd).split("\n");
      remainder = source.slice(prefixEnd).replace(/^\n/, "");
    }
    const inlineState = { nodes: 0 };
    let index = 0;
    const nativeHeading = /^\s*\u3010([^\u3011\n]{1,60})\u3011\s*$/;
    const isBlockStart = (line, next) => /^(?:\s{0,3}#{1,6}\s+|\s*```|\s*>|\s*[-*+]\s+|\s*\d+[.)]\s+)/.test(line) || nativeHeading.test(line) || (line.includes("|") && next && isTableSeparator(next));
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const fence = line.match(/^\s*```\s*([\w.+-]*)\s*$/);
      if (fence) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) codeLines.push(lines[index++]);
        if (index < lines.length) index += 1;
        root.append(renderCodeBlock(codeLines.join("\n"), fence[1]));
        continue;
      }
      const nativeSection = line.match(nativeHeading);
      if (nativeSection) {
        root.append(create("h2", "content-heading", nativeSection[1]));
        index += 1;
        continue;
      }
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const title = create(`h${heading[1].length}`, "content-heading");
        appendInline(title, heading[2], inlineState);
        root.append(title);
        index += 1;
        continue;
      }
      if (line.includes("|") && lines[index + 1] && isTableSeparator(lines[index + 1])) {
        const tableLines = [line, lines[index + 1]];
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) tableLines.push(lines[index++]);
        root.append(renderTable(tableLines, inlineState));
        continue;
      }
      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const list = create(ordered ? "ol" : "ul", "content-list");
        while (index < lines.length) {
          const pattern = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
          const itemMatch = lines[index].match(pattern);
          if (!itemMatch) break;
          const item = create("li", "content-list-item");
          appendInline(item, itemMatch[1], inlineState);
          list.append(item);
          index += 1;
        }
        root.append(list);
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quote = create("blockquote", "content-markdown-quote");
        const quoteLines = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) quoteLines.push(lines[index++].replace(/^\s*> ?/, ""));
        const body = create("div", "content-quote-body");
        appendInline(body, quoteLines.join("\n"), inlineState);
        quote.append(body);
        root.append(quote);
        continue;
      }
      const paragraphLines = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index], lines[index + 1])) paragraphLines.push(lines[index++]);
      const paragraph = create("p", "content-paragraph");
      appendInline(paragraph, paragraphLines.join("\n"), inlineState);
      root.append(paragraph);
    }
    if (remainder) root.append(createRemainder(remainder, "\u957f\u6587\u672c\u5269\u4f59\u5185\u5bb9", "\u9996\u5c4f\u4ec5\u5c55\u793a\u6709\u9650\u524d\u7f00"));
    return root;
  }

  function renderFieldValue(key, value, budget, depth) {
    const prepared = prepareValue(value);
    if (typeof prepared === "string" && TEXT_FIELDS.has(key)) return markdown(prepared);
    return renderPrepared(prepared, budget, depth, key);
  }

  function renderModelMessage(value, budget, depth) {
    const card = create("article", "content-model-message");
    const header = create("div", "content-model-message-head");
    append(header, create("span", "content-role", roleLabel(value.role)));
    if (value.name) append(header, create("span", "content-speaker", displayScalar(value.name)));
    card.append(header);
    if (Object.hasOwn(value, "content")) {
      const body = create("div", "content-model-message-content");
      body.append(renderFieldValue("content", value.content, budget, depth + 1));
      card.append(body);
    }
    for (const key of Object.keys(value)) {
      if (key === "role" || key === "name" || key === "content") continue;
      card.append(renderField(key, value[key], budget, depth + 1));
    }
    return card;
  }

  function renderToolCall(value, budget, depth) {
    const card = create("article", "content-tool-call");
    const header = create("div", "content-tool-head");
    append(header, create("span", "content-tool-label", "\u5de5\u5177"), create("strong", "content-tool-name", value.name || value.tool_name || "\u672a\u547d\u540d"));
    card.append(header);
    const argumentKey = Object.hasOwn(value, "arguments") ? "arguments" : "args";
    if (Object.hasOwn(value, argumentKey)) card.append(renderField(argumentKey, value[argumentKey], budget, depth + 1));
    if (Object.hasOwn(value, "result")) card.append(renderField("result", value.result, budget, depth + 1));
    for (const key of Object.keys(value)) {
      if (["name", "tool_name", "arguments", "args", "result"].includes(key)) continue;
      card.append(renderField(key, value[key], budget, depth + 1));
    }
    return card;
  }

  function renderSegments(value, budget, depth) {
    const card = create("section", "content-segments-card");
    card.append(create("h4", "content-card-title", "\u5185\u5bb9\u6bb5\u843d"));
    const list = create("div", "content-segments");
    const limit = Math.min(value.length, MAX_ARRAY_ITEMS);
    let remainderAdded = false;
    for (let index = 0; index < limit; index += 1) {
      if (!spend(budget)) {
        list.append(createRemainder(value.slice(index), "\u5269\u4f59\u6bb5\u843d"));
        remainderAdded = true;
        break;
      }
      const segment = value[index];
      const item = create("article", "content-segment");
      if (isPlainObject(segment)) {
        const type = segment.type || segment.kind || `#${index + 1}`;
        item.append(create("div", "content-segment-type", roleLabel(type)));
        const body = create("div", "content-segment-body");
        for (const key of Object.keys(segment)) {
          if (key === "type" || key === "kind") continue;
          body.append(renderField(key, segment[key], budget, depth + 1));
        }
        item.append(body);
      } else item.append(renderPrepared(segment, budget, depth + 1, "segment"));
      list.append(item);
    }
    if (value.length > limit && !remainderAdded) list.append(createRemainder(value.slice(limit), "\u5269\u4f59\u6bb5\u843d"));
    card.append(list);
    return card;
  }

  function renderField(key, value, budget, depth) {
    const field = create("div", "content-field");
    const label = create("dt", "content-field-label", fieldLabel(key));
    const body = create("dd", "content-field-value");
    body.append(renderFieldValue(key, value, budget, depth));
    field.append(label, body);
    return field;
  }

  function renderPrepared(value, budget, depth, key = "") {
    if (!spend(budget)) return createRemainder(value, "\u5df2\u8fbe\u5230\u5c55\u793a\u4e0a\u9650");
    if (depth > MAX_DEPTH) return createRemainder(value, "\u5d4c\u5957\u5c42\u7ea7\u8f83\u6df1");
    if (value === null || value === undefined || typeof value !== "object") return typeof value === "string" && (key === "" || TEXT_FIELDS.has(key)) ? markdown(value) : plainText(displayScalar(value));
    if (Array.isArray(value)) {
      if (key === "segments") return renderSegments(value, budget, depth);
      const card = create("section", "content-array-card");
      card.append(create("h4", "content-card-title", `${fieldLabel(key || "value")} \u00b7 ${value.length} \u9879`));
      const list = create("div", "content-array-items");
      const limit = Math.min(value.length, MAX_ARRAY_ITEMS);
      let remainderAdded = false;
      for (let index = 0; index < limit; index += 1) {
        if (!spend(budget)) {
          list.append(createRemainder(value.slice(index), "\u5269\u4f59\u6570\u7ec4\u9879"));
          remainderAdded = true;
          break;
        }
        const item = create("article", "content-array-item");
        item.append(create("span", "content-array-index", `#${index + 1}`));
        item.append(renderPrepared(value[index], budget, depth + 1, "item"));
        list.append(item);
      }
      if (value.length > limit && !remainderAdded) list.append(createRemainder(value.slice(limit), "\u5269\u4f59\u6570\u7ec4\u9879"));
      card.append(list);
      return card;
    }
    if (isPlainObject(value)) {
      if (typeof value.role === "string" && Object.hasOwn(value, "content")) return renderModelMessage(value, budget, depth);
      if ((typeof value.name === "string" || typeof value.tool_name === "string") && (Object.hasOwn(value, "arguments") || Object.hasOwn(value, "args"))) return renderToolCall(value, budget, depth);
      const card = create("section", "content-object-card");
      const title = key ? create("h4", "content-card-title", fieldLabel(key)) : null;
      if (title) card.append(title);
      const fields = create("dl", "content-fields");
      const keys = Object.keys(value);
      const limit = Math.min(keys.length, MAX_OBJECT_FIELDS);
      let remainderAdded = false;
      for (let index = 0; index < limit; index += 1) {
        if (!spend(budget)) {
          fields.append(createRemainder(Object.fromEntries(keys.slice(index).map((name) => [name, value[name]])), "\u5269\u4f59\u5b57\u6bb5"));
          remainderAdded = true;
          break;
        }
        fields.append(renderField(keys[index], value[keys[index]], budget, depth + 1));
      }
      if (keys.length > limit && !remainderAdded) fields.append(createRemainder(Object.fromEntries(keys.slice(limit).map((name) => [name, value[name]])), "\u5269\u4f59\u5b57\u6bb5"));
      card.append(fields);
      return card;
    }
    return plainText(displayScalar(value));
  }

  function value(value) {
    const root = create("div", "content-value");
    const budget = createBudget();
    root.append(renderPrepared(prepareValue(value), budget, 0));
    return root;
  }

  function message(record) {
    const root = create("article", "content-message");
    const header = create("header", "content-message-head");
    if (record && record.speaker !== null && record.speaker !== undefined && record.speaker !== "") header.append(create("span", "content-message-speaker", displayScalar(record.speaker)));
    if (record?.truncated) header.append(create("span", "content-truncated-badge", "\u5185\u5bb9\u5df2\u622a\u65ad"));
    if (header.childNodes.length) root.append(header);
    const body = create("div", "content-message-body");
    body.append(markdown(record?.content == null ? "" : String(record.content)));
    root.append(body);
    const mentions = Array.isArray(record?.mentions) ? record.mentions : [];
    const names = mentions.map((item) => (isObject(item) ? item.name : item)).filter((name) => name !== null && name !== undefined && String(name) !== "");
    if (names.length) {
      const mentionBox = create("div", "content-mentions");
      mentionBox.append(create("span", "content-meta-label", "\u63d0\u53ca"));
      for (const name of names) mentionBox.append(create("span", "content-mention", `@${String(name)}`));
      root.append(mentionBox);
    }
    const quotes = Array.isArray(record?.quotes) ? record.quotes : [];
    if (quotes.length) {
      const quoteGroup = create("section", "content-quotes");
      quoteGroup.append(create("h4", "content-quotes-title", "\u5f15\u7528\u4e0a\u4e0b\u6587"));
      for (const quote of quotes) {
        const item = create("blockquote", "content-message-quote");
        const quoteHead = create("div", "content-quote-head");
        if (quote?.role) quoteHead.append(create("span", "content-role", roleLabel(quote.role)));
        if (quote?.speaker) quoteHead.append(create("span", "content-speaker", displayScalar(quote.speaker)));
        if (quoteHead.childNodes.length) item.append(quoteHead);
        item.append(markdown(quote?.content == null ? "" : String(quote.content)));
        quoteGroup.append(item);
      }
      root.append(quoteGroup);
    }
    return root;
  }

  global.SessionContent = Object.freeze({ message, value, markdown });
})(window);
