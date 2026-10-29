"use strict";

(function defineSessionCompanion(global) {
  const LABELS = Object.freeze({
    normal: "\u666e\u901a\u6a21\u5f0f", companion: "\u966a\u4f34\u6a21\u5f0f", active: "\u8fdb\u884c\u4e2d", completed: "\u5df2\u5b8c\u6210", committed: "\u5df2\u63d0\u4ea4",
    succeeded: "\u6210\u529f", failed: "\u5931\u8d25", cancelled: "\u5df2\u53d6\u6d88", interrupted: "\u5df2\u4e2d\u65ad", blocked: "\u5bbf\u4e3b\u963b\u6b62",
    unknown: "\u7ed3\u679c\u672a\u77e5", missing: "\u5386\u53f2\u672a\u8bb0\u5f55", not_recorded: "\u5386\u53f2\u672a\u8bb0\u5f55", not_proposed: "\u672a\u63d0\u8bae\u8054\u7cfb",
    reserved: "\u5df2\u9884\u7559\u8054\u7cfb", confirmed: "\u5df2\u786e\u8ba4\u9001\u8fbe", sent: "\u5df2\u53d1\u9001", silent: "\u4e3b\u52a8\u6c89\u9ed8", pending: "\u7b49\u5f85\u4e2d",
    delivered: "\u5df2\u786e\u8ba4\u9001\u8fbe", expired: "\u5df2\u8fc7\u671f", queued: "\u5df2\u6392\u5165\u4f1a\u8bdd\u673a\u4f1a", recorded: "\u5df2\u8bb0\u5f55", invalid: "\u8bb0\u5f55\u65e0\u6548", overflow: "\u6355\u83b7\u8d85\u9650",
    check_in: "\u95ee\u5019", share: "\u5206\u4eab", continue: "\u7ee7\u7eed\u8bdd\u9898", free_chat: "\u95f2\u804a",
    request_timeout: "\u8bf7\u6c42\u8d85\u65f6 (\u79d2)", reflection_interval_minutes: "\u53cd\u601d\u95f4\u9694 (\u5206\u949f)", daily_reflections: "\u6bcf\u65e5\u53cd\u601d\u4e0a\u9650", daily_contacts: "\u6bcf\u65e5\u8054\u7cfb\u4e0a\u9650",
    contact_interval_minutes: "\u8054\u7cfb\u95f4\u9694 (\u5206\u949f)", member_idle_days: "\u6210\u5458\u6d3b\u8dc3\u7a97\u53e3 (\u5929)", input_chars: "\u8f93\u5165\u5b57\u7b26\u4e0a\u9650", output_tokens: "\u8f93\u51fa Token \u4e0a\u9650",
  });
  const REASONS = Object.freeze({
    no_contact_proposed: "\u6a21\u578b\u672a\u63d0\u8bae\u8054\u7cfb", mode_changed: "\u6a21\u5f0f\u5df2\u53d8\u66f4", participant_active: "\u53c2\u4e0e\u8005\u6b63\u5728\u6d3b\u52a8", service_stopped: "\u670d\u52a1\u5df2\u505c\u6b62",
    state_not_committed: "\u53cd\u601d\u72b6\u6001\u672a\u63d0\u4ea4", account_unavailable: "\u8d26\u53f7\u4e0d\u53ef\u7528", contact_inflight_or_unknown: "\u8054\u7cfb\u53d1\u9001\u4e2d\u6216\u7ed3\u679c\u672a\u77e5",
    quiet_hours: "\u514d\u6253\u6270\u65f6\u6bb5", daily_contact_limit: "\u8fbe\u5230\u6bcf\u65e5\u8054\u7cfb\u9650\u5236", recent_user_activity: "\u7528\u6237\u521a\u521a\u6d3b\u52a8", member_inactive: "\u6210\u5458\u4e0d\u5728\u6d3b\u8dc3\u7a97\u53e3",
    contact_interval: "\u8054\u7cfb\u95f4\u9694\u672a\u5230", duplicate_contact: "\u91cd\u590d\u8054\u7cfb", review_changed: "\u53cd\u601d\u5df2\u53d8\u66f4", member_preference: "\u6210\u5458\u8054\u7cfb\u504f\u597d\u9650\u5236",
    contact_reserved: "\u901a\u8fc7\u5bbf\u4e3b\u68c0\u67e5\u5e76\u9884\u7559", contact_claim_failed: "\u8054\u7cfb\u9884\u7559\u5931\u8d25", reflection_interrupted: "\u590d\u76d8\u88ab\u4e2d\u65ad", reflection_failed: "\u590d\u76d8\u5931\u8d25",
    dialogue_opportunity_queued: "\u5df2\u4ea4\u7ed9\u5f53\u524d\u9891\u9053\u4f1a\u8bdd\u51b3\u5b9a\u65f6\u673a", dialogue_opportunity_pending: "\u9891\u9053\u5df2\u6709\u5f85\u5904\u7406\u673a\u4f1a",
  });
  const missing = "\u5386\u53f2\u672a\u8bb0\u5f55";
  const currentMissing = "\u5f53\u524d\u8bb0\u5f55\u4e0d\u53ef\u7528";
  function label(value) { return LABELS[value] || value || missing; }
  function reason(value) { return REASONS[value] ? `${REASONS[value]} (${value})` : value || missing; }
  function scalar(value) { return value == null ? currentMissing : typeof value === "boolean" ? value ? "\u662f" : "\u5426" : String(value); }
  function proposalStatus(record) {
    const proposed = typeof record.contact_proposed === "boolean" ? record.contact_proposed
      : record.proposal && Object.hasOwn(record.proposal, "contact") ? record.proposal.contact !== null : null;
    return proposed === true ? "\u63d0\u8bae\u8054\u7cfb" : proposed === false ? label("not_proposed") : "\u63d0\u8bae\u672a\u8bb0\u5f55";
  }
  function truncated(record) {
    return Boolean(record.audit_truncated || record.contacts_truncated
      || Object.values(record.capture?.truncated || {}).some(Boolean)
      || Object.values(record.truncated || {}).some(Boolean));
  }

  function create(hooks) {
    const { node, button, disclosure, date, number, duration, eventButton, selectEvidenceTurn } = hooks;
    function note(text, warning = false) { return node("p", warning ? "partial-notice" : "muted", text); }
    function badge(value) {
      const item = node("span", "badge companion-status", label(value));
      item.dataset.status = value || "missing";
      if (["failed", "cancelled", "interrupted", "blocked", "invalid"].includes(value)) item.classList.add("badge-danger");
      if (["active", "pending", "queued", "reserved", "unknown", "overflow"].includes(value)) item.classList.add("badge-pending");
      if (["confirmed", "succeeded", "completed", "committed", "sent", "delivered"].includes(value)) item.classList.add("badge-success");
      return item;
    }
    function fold(title, key, description = "") {
      const panel = disclosure(title, description, "record-card companion-disclosure");
      panel.item.dataset.detailKey = key;
      return panel;
    }
    function snapshot(title, key, value, description = "") {
      const panel = fold(title, key, description);
      const content = SessionContent.invocation(value ?? missing, "arguments");
      content.dataset.detailKey = key;
      panel.content.append(content);
      return panel.item;
    }
    function navigation(evidence, title, key) {
      const item = button(title, () => selectEvidenceTurn(evidence));
      item.dataset.focusKey = key;
      item.disabled = !evidence?.turn_ref || !evidence?.session_ref;
      if (item.disabled) item.title = "\u539f\u751f\u8f6e\u6b21\u672a\u5173\u8054\uff1b\u4ecd\u53ef\u8bfb\u53d6\u5df2\u4fdd\u5b58\u8bb0\u5f55";
      return item;
    }
    function overview(data) {
      const root = node("section", "companion-overview");
      root.dataset.recordKey = "current-companion";
      root.append(node("h3", "", "\u5f53\u524d\u966a\u4f34\u72b6\u6001"));
      if (!data) { root.append(note(currentMissing)); return root; }
      const mode = data.mode;
      const heading = node("div", "companion-heading");
      heading.append(badge(mode?.mode), node("span", "", mode?.persona_key || "\u4eba\u683c\u672a\u8bb0\u5f55"));
      root.append(heading);
      if (mode?.mode === "companion" || mode?.next_review_at) root.append(note(`\u4e0b\u6b21\u8ba1\u5212\u590d\u76d8\uff1a${date(mode?.next_review_at)}`));
      if (mode?.recorded === false) root.append(note("\u672a\u6301\u4e45\u5316\u6a21\u5f0f\u8bb0\u5f55\uff1b\u5f53\u524d\u91c7\u7528\u666e\u901a\u6a21\u5f0f\u9ed8\u8ba4\u7b56\u7565\u3002"));
      root.append(note("\u4ec5\u8bfb\u5f53\u524d\u72b6\u6001\uff0c\u4e0d\u7528\u4e8e\u8865\u5168\u5386\u53f2\u51b3\u7b56\u3002"));
      const limits = fold("\u5f53\u524d\u8fd0\u884c\u9650\u5236\u4e0e\u4eca\u65e5\u8ba1\u6570", "current-limits", "\u5f53\u524d\u914d\u7f6e\uff0c\u4e0d\u662f\u5386\u53f2\u8054\u7cfb\u9650\u5236");
      if (data.current_limits) {
        const values = node("dl", "companion-facts");
        for (const [key, value] of Object.entries(data.current_limits)) values.append(node("dt", "", label(key)), node("dd", "", scalar(value)));
        limits.content.append(values);
      } else limits.content.append(note(currentMissing));
      const counts = (values) => values && typeof values === "object" ? Object.entries(values).map(([key, count]) => `${label(key)} ${number(count)}`).join(" / ") || "0" : currentMissing;
      limits.content.append(note(`\u6a21\u5f0f\u7248\u672c ${number(mode?.revision)} / \u4e0a\u4e0b\u6587\u7248\u672c ${number(mode?.context_revision)} \u00b7 ${date(mode?.updated_at)}`));
      limits.content.append(note(`${data.daily?.day || currentMissing} \u00b7 ${data.daily?.timezone || currentMissing}`));
      limits.content.append(note(`\u4eca\u65e5\u53cd\u601d\uff1a${counts(data.daily?.reviews)}\uff1b\u8054\u7cfb\u5c1d\u8bd5\uff1a${counts(data.daily?.contacts)}\uff1b\u786e\u8ba4\u9001\u8fbe\u6d88\u606f\uff1a${number(data.daily?.confirmed_messages)}`));
      if (data.daily?.confirmed_messages == null) limits.content.append(note("\u786e\u8ba4\u6d88\u606f\u8ba1\u6570\u4e0d\u53ef\u7528\uff0c\u4e0d\u6309 0 \u5904\u7406\u3002", true));
      root.append(limits.item);
      const life = data.life;
      const lifePanel = fold("\u5f53\u524d\u865a\u6784\u751f\u6d3b", "current-life", life?.truncated ? "\u663e\u793a\u5df2\u622a\u65ad" : life ? label(life.capture) : currentMissing);
      lifePanel.content.append(note("\u89d2\u8272\u865a\u6784\u751f\u6d3b\uff0c\u4e0d\u662f\u73b0\u5b9e\u7ecf\u5386\u6216\u5e73\u53f0\u6d3b\u52a8\u3002"));
      if (life) lifePanel.content.append(note(`\u751f\u6d3b\u7248\u672c ${number(life.version)} \u00b7 ${life.persona_key || currentMissing} \u00b7 ${date(life.updated_at)}`));
      lifePanel.content.append(SessionContent.invocation(life?.state ?? currentMissing, "arguments"));
      if (life?.truncated) lifePanel.content.append(note("\u5f53\u524d\u751f\u6d3b\u663e\u793a\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u8bb0\u5f55\u3002", true));
      root.append(lifePanel.item);
      const members = Array.isArray(data.members) ? data.members : null;
      const memberPanel = fold("\u5f53\u524d\u6210\u5458\u5173\u6ce8\u4e0e\u8054\u7cfb\u504f\u597d", "current-members", members ? `${members.length} \u4f4d\u6210\u5458${data.members_truncated ? " \u00b7 \u5217\u8868\u5df2\u622a\u65ad" : ""}` : currentMissing);
      if (!members || !members.length) memberPanel.content.append(note(members ? "\u6682\u65e0\u5df2\u8bb0\u5f55\u6210\u5458" : currentMissing));
      for (const member of members || []) {
        const entry = node("article", "companion-member");
        entry.dataset.recordKey = member.member_ref;
        entry.append(node("h4", "", member.display_name || member.member_ref || currentMissing));
        entry.append(note(`\u6700\u8fd1\u7528\u6237\u6d3b\u52a8 ${date(member.last_user_at)} \u00b7 \u6700\u8fd1\u8054\u7cfb ${date(member.last_contact_at)} \u00b7 \u7248\u672c ${number(member.version)}`));
        if (member.preference?.recorded === false) entry.append(note("\u8054\u7cfb\u504f\u597d\u672a\u6301\u4e45\u5316\uff1b\u4f7f\u7528\u5f53\u524d\u9ed8\u8ba4\u7b56\u7565\u3002"));
        entry.append(SessionContent.invocation({ preference: member.preference ?? null, attention: member.attention ?? null }, "arguments"));
        if (!Array.isArray(member.attention)) entry.append(note("\u5173\u6ce8\u9879\u8bb0\u5f55\u4e0d\u53ef\u7528\u3002", true));
        else if (!member.attention.length) entry.append(note("\u5df2\u663e\u793a\u7684\u5173\u6ce8\u9879\uff1a0"));
        if (member.attention_truncated || member.attention?.some((item) => item.truncated)) entry.append(note("\u5173\u6ce8\u9879\u663e\u793a\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u8bb0\u5f55\u3002", true));
        memberPanel.content.append(entry);
      }
      if (data.members_truncated) memberPanel.content.append(note("\u6210\u5458\u5217\u8868\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u6210\u5458\u3002", true));
      root.append(memberPanel.item);
      return root;
    }
    function contactEntry(contact) {
      const entry = node("article", "companion-contact");
      entry.dataset.recordKey = contact.attempt_ref;
      const heading = node("div", "companion-heading");
      const destination = contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : contact.display_name || contact.member_ref || missing;
      heading.append(node("h4", "", destination), badge(contact.status));
      entry.append(heading, node("p", "", reason(contact.reason)));
      entry.append(note(`\u786e\u8ba4\u9001\u8fbe\u6d88\u606f ${number(contact.confirmed_count)} \u00b7 ${date(contact.created_at)} \u2192 ${date(contact.finished_at)}`));
      if (contact.status === "unknown") entry.append(note("\u53d1\u9001\u7ed3\u679c\u672a\u77e5\uff0c\u4e0d\u7b49\u4e8e\u672a\u53d1\u9001\uff0c\u4e5f\u4e0d\u63a8\u65ad\u9001\u8fbe\u3002", true));
      if (contact.confirmed_count == null) entry.append(note("\u9001\u8fbe\u56de\u6267\u8ba1\u6570\u4e0d\u53ef\u7528\uff0c\u4e0d\u6309 0 \u5904\u7406\u3002", true));
      entry.append(navigation(contact.native_turn, "\u67e5\u770b\u8054\u7cfb\u8f6e\u6b21\u4e0e\u9001\u8fbe\u8bc1\u636e", `contact:${contact.attempt_ref}:turn`));
      entry.append(snapshot("\u8054\u7cfb\u5feb\u7167\u4e0e\u4f20\u8f93\u5ba1\u8ba1", "contact-audit", {
        attempt_ref: contact.attempt_ref, member_ref: contact.member_ref, target: contact.target,
        proposal: contact.proposal, limits: contact.limits, mode_revision: contact.mode_revision,
        context_revision: contact.context_revision, member_version: contact.member_version,
        transport_started_at: contact.transport_started_at, transport_inflight: contact.transport_inflight,
        previous_contact_at: contact.previous_contact_at, receipt_capture: contact.receipt_capture,
      }, "\u5f53\u65f6\u6355\u83b7\uff0c\u4e0d\u662f\u5f53\u524d\u914d\u7f6e"));
      return entry;
    }
    function review(item, fallback) {
      const root = node("section", "companion-inspection");
      if (!item && !fallback) { root.append(note("\u672c\u8f6e\u672a\u8bb0\u5f55\u966a\u4f34\u53cd\u601d\u3002")); return root; }
      // A captured-event fallback never supplies a review status, transaction or contact history.
      const record = item || fallback;
      root.dataset.recordKey = item?.review_ref || "captured-reflection";
      const heading = node("header", "companion-heading");
      heading.append(node("h3", "", "\u966a\u4f34\u53cd\u601d\u7ed3\u679c"));
      if (item) heading.append(badge(item.status));
      root.append(heading);
      const summary = record.decision_summary ?? record.proposal?.decision_summary;
      root.append(node("p", "companion-decision-summary", summary || "\u590d\u76d8\u7ed3\u8bba\u672a\u8bb0\u5f55"));
      if (item) {
        root.append(note(`${item.persona_key || "\u4eba\u683c\u672a\u8bb0\u5f55"} \u00b7 ${date(item.started_at)} \u2192 ${date(item.finished_at)} \u00b7 ${duration(item.duration_ms)}`));
        if (item.reason) root.append(node("p", "", reason(item.reason)));
        if (!item.native_turn) root.append(note("\u672a\u5173\u8054\u539f\u751f\u8f6e\u6b21\uff1b\u4ecd\u53ef\u8bfb\u53d6\u5df2\u4fdd\u5b58\u7684\u590d\u76d8\u4e0e\u8054\u7cfb\u8bb0\u5f55\u3002"));
      } else root.append(note("\u672a\u5173\u8054\u72ec\u7acb\u590d\u76d8\u8bb0\u5f55\uff1b\u4ee5\u4e0b\u4ec5\u4e3a\u672c\u8f6e\u6355\u83b7\u4e8b\u5b9e\uff0c\u4e8b\u52a1\u4e0e\u8054\u7cfb\u7ed3\u679c\u672a\u77e5\u3002", true));
      const stages = node("ol", "companion-stages");
      const proposal = node("li", "companion-stage");
      proposal.append(node("h4", "", "1. \u6a21\u578b\u63d0\u8bae"), node("p", "", proposalStatus(record)));
      const contact = record.contact || record.proposal?.contact;
      if (contact && typeof contact === "object") {
        const destination = contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : contact.display_name || contact.member_ref || missing;
        proposal.append(note(`${destination} \u00b7 ${label(contact.purpose)}\uff1a${contact.topic || missing}`));
        if (contact.reason) proposal.append(note(contact.reason));
      }
      const gate = node("li", "companion-stage");
      gate.append(node("h4", "", "2. \u5bbf\u4e3b\u95e8\u7981"), badge(record.decision?.status));
      if (record.decision) gate.append(note(reason(record.decision.reason)), note(`${date(record.decision.checked_at)} \u00b7 ${record.decision.source || missing}`));
      else gate.append(note("\u95e8\u7981\u5386\u53f2\u672a\u8bb0\u5f55\uff0c\u4e0d\u4ece\u8054\u7cfb\u6570\u91cf\u63a8\u65ad\u539f\u56e0\u3002"));
      const contacts = item && Array.isArray(item.contacts) ? item.contacts : null;
      const actual = node("li", "companion-stage");
      actual.append(node("h4", "", "3. \u5b9e\u9645\u8054\u7cfb"), node("p", "", contacts ? `\u5df2\u663e\u793a ${number(contacts.length)} \u6b21\u5c1d\u8bd5` : missing));
      if (!contacts || item?.contacts_truncated) actual.append(note("\u8054\u7cfb\u8bb0\u5f55\u4e0d\u5b8c\u6574\uff0c\u4e0d\u80fd\u636e\u6b64\u65ad\u8a00\u672a\u53d1\u9001\u3002"));
      if (item?.contacts_truncated) actual.append(note("\u8054\u7cfb\u5217\u8868\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5168\u91cf\u8bb0\u5f55\u3002", true));
      if (contacts?.some((entry) => entry.status === "unknown")) actual.append(note("\u5b58\u5728\u7ed3\u679c\u672a\u77e5\u7684\u8054\u7cfb\u5c1d\u8bd5\u3002", true));
      stages.append(proposal, gate, actual);
      root.append(stages);
      if (item) {
        const transaction = item.transaction_committed === true ? "\u5df2\u63d0\u4ea4" : item.transaction_committed === false ? "\u672a\u63d0\u4ea4" : missing;
        const application = item.changes?.committed === true ? "\u6355\u83b7\u7684\u53d8\u66f4\u5df2\u5e94\u7528"
          : item.transaction_committed === true ? "\u4e8b\u52a1\u5df2\u63d0\u4ea4\uff0c\u53d8\u66f4\u5feb\u7167\u4e0d\u53ef\u7528"
          : item.transaction_committed === false ? "\u672a\u5e94\u7528\u672c\u6b21\u63d0\u8bae" : "\u53d8\u66f4\u5e94\u7528\u7ed3\u679c\u672a\u8bb0\u5f55";
        root.append(node("p", "companion-transaction", `\u6570\u636e\u5e93\u4e8b\u52a1\uff1a${transaction} \u00b7 ${application}`));
      }
      const audit = node("div", "actions companion-navigation");
      for (const [field, title] of [["state_event_ref", "\u539f\u59cb\u8f93\u5165"], ["proposal_event_ref", "\u539f\u59cb\u63d0\u8bae"], ["decision_event_ref", "\u5bbf\u4e3b\u51b3\u7b56"], ["context_event_ref", "\u5f53\u65f6\u4e0a\u4e0b\u6587"]]) audit.append(eventButton(record.events?.[field], title));
      root.append(audit);
      if (["state", "proposal", "decision"].some((key) => record.capture?.[key] !== "recorded")) {
        root.append(note(`\u8f93\u5165\u6355\u83b7 ${label(record.capture?.state)} \u00b7 \u63d0\u8bae\u6355\u83b7 ${label(record.capture?.proposal)} \u00b7 \u51b3\u7b56\u6355\u83b7 ${label(record.capture?.decision)}`));
      }
      if (truncated(record)) root.append(note("\u672c\u6b21\u663e\u793a\u5df2\u622a\u65ad\uff0c\u4e0d\u662f\u5b8c\u6574\u5ba1\u8ba1\uff1b\u53ef\u6309\u9700\u67e5\u770b\u539f\u59cb\u8bb0\u5f55\u3002", true));
      for (const entry of contacts || []) root.append(contactEntry(entry));
      if (item) root.append(snapshot("\u751f\u6210\u53d8\u5316\u4e0e\u4e8b\u52a1\u63d0\u4ea4", "review-changes", {
        life_version: item.life_version, committed_life_version: item.committed_life_version,
        transaction_committed: item.transaction_committed, changes: item.changes ?? null,
      }, "\u751f\u6210\u63d0\u8bae\u4e0d\u7b49\u4e8e\u5df2\u5e94\u7528\uff1b\u751f\u6d3b\u5185\u5bb9\u5747\u4e3a\u89d2\u8272\u865a\u6784"));
      root.append(snapshot("\u5f53\u65f6\u53cd\u601d\u8f93\u5165", "review-input", record.state, "\u4ec5\u4f7f\u7528\u672c\u6b21\u6355\u83b7\uff0c\u4e0d\u4ee5\u5f53\u524d\u72b6\u6001\u66ff\u4ee3"));
      root.append(snapshot("\u7ed3\u6784\u5316\u6a21\u578b\u63d0\u8bae", "review-proposal", record.proposal, "\u5df2\u8bb0\u5f55\u7684\u7ed3\u6784\u5316\u8f93\u51fa\uff0c\u4e0d\u662f\u9690\u85cf\u601d\u7ef4\u94fe"));
      if (item) {
        const evidence = fold("\u5df2\u9a8c\u8bc1\u7684\u804a\u5929\u8bc1\u636e", "review-evidence", Array.isArray(item.evidence_turns) ? `${item.evidence_turns.length} \u8f6e` : missing);
        for (const turn of item.evidence_turns || []) evidence.content.append(navigation(turn, `\u67e5\u770b\u8bc1\u636e #${turn.turn_id} / \u8f6e\u6b21 ${turn.sequence ?? "?"}`, `review:${item.review_ref}:evidence:${turn.turn_id}`));
        if (!item.evidence_turns?.length) evidence.content.append(note("\u65e0\u53ef\u7528\u7684\u5df2\u9a8c\u8bc1\u8bc1\u636e\u5f15\u7528\u3002"));
        root.append(evidence.item);
      }
      root.append(snapshot("\u539f\u59cb\u51b3\u7b56\u4e0e\u5ba1\u8ba1\u5143\u6570\u636e", "review-metadata", item ? {
        review_ref: item.review_ref, persona_key: item.persona_key, status: item.status, reason: item.reason,
        started_at: item.started_at, finished_at: item.finished_at, mode_revision: item.mode_revision,
        context_revision: item.context_revision, committed: item.committed, committed_contact: item.committed_contact,
        decision: item.decision, native_turn: item.native_turn, events: item.events, capture: item.capture,
        audit_truncated: item.audit_truncated, contacts_truncated: item.contacts_truncated,
      } : { decision: record.decision, events: record.events, capture: record.capture, truncated: record.truncated }, "\u53ea\u8bfb\u5386\u53f2\u4e8b\u5b9e\uff0c\u4e0d\u4ee5\u5f53\u524d\u72b6\u6001\u8865\u5168"));
      return root;
    }
    return { overview, review };
  }

  global.SessionCompanion = Object.freeze({ create });
})(window);
