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
  function reason(value) { return REASONS[value] || value || missing; }
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
    const { node, button, disclosure, date, number, eventButton, selectEvidenceTurn, openNative } = hooks;
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
    function navigation(evidence, title, key, native = false) {
      if (!evidence?.turn_ref || !evidence?.session_ref) return null;
      const item = button(title, () => native ? openNative(evidence.turn_ref) : selectEvidenceTurn(evidence));
      item.dataset.focusKey = key;
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
      if (mode?.recorded === false) root.append(note("\u6a21\u5f0f\u672a\u6301\u4e45\u5316"));
      const limits = fold("\u5f53\u524d\u8fd0\u884c\u9650\u5236\u4e0e\u4eca\u65e5\u8ba1\u6570", "current-limits");
      if (data.current_limits) {
        const values = node("dl", "companion-facts");
        for (const [key, value] of Object.entries(data.current_limits)) values.append(node("dt", "", label(key)), node("dd", "", scalar(value)));
        limits.content.append(values);
      } else limits.content.append(note(currentMissing));
      const counts = (values) => values && typeof values === "object" ? Object.entries(values).map(([key, count]) => `${label(key)} ${number(count)}`).join(" / ") || "0" : currentMissing;
      limits.content.append(note(`\u6a21\u5f0f\u7248\u672c ${number(mode?.revision)} / \u4e0a\u4e0b\u6587\u7248\u672c ${number(mode?.context_revision)} \u00b7 ${date(mode?.updated_at)}`));
      limits.content.append(note(`${data.daily?.day || currentMissing} \u00b7 ${data.daily?.timezone || currentMissing}`));
      limits.content.append(note(`\u4eca\u65e5\u53cd\u601d\uff1a${counts(data.daily?.reviews)}\uff1b\u8054\u7cfb\u5c1d\u8bd5\uff1a${counts(data.daily?.contacts)}\uff1b\u786e\u8ba4\u9001\u8fbe\u6d88\u606f\uff1a${number(data.daily?.confirmed_messages)}`));
      if (data.daily?.confirmed_messages == null) limits.content.append(note("\u786e\u8ba4\u6d88\u606f\u8ba1\u6570\u4e0d\u53ef\u7528", true));
      root.append(limits.item);
      const life = data.life;
      const lifePanel = fold("\u5f53\u524d\u865a\u6784\u751f\u6d3b", "current-life", life?.truncated ? "\u663e\u793a\u5df2\u622a\u65ad" : life ? label(life.capture) : currentMissing);
      if (life) lifePanel.content.append(note(`\u751f\u6d3b\u7248\u672c ${number(life.version)} \u00b7 ${life.persona_key || currentMissing} \u00b7 ${date(life.updated_at)}`));
      lifePanel.content.append(SessionContent.invocation(life?.state ?? currentMissing, "arguments"));
      if (life?.truncated) lifePanel.content.append(note("\u751f\u6d3b\u8bb0\u5f55\u5df2\u622a\u65ad", true));
      root.append(lifePanel.item);
      const members = Array.isArray(data.members) ? data.members : null;
      const memberPanel = fold("\u5f53\u524d\u6210\u5458\u5173\u6ce8\u4e0e\u8054\u7cfb\u504f\u597d", "current-members", members ? `${members.length} \u4f4d\u6210\u5458${data.members_truncated ? " \u00b7 \u5217\u8868\u5df2\u622a\u65ad" : ""}` : currentMissing);
      if (!members || !members.length) memberPanel.content.append(note(members ? "\u6682\u65e0\u5df2\u8bb0\u5f55\u6210\u5458" : currentMissing));
      for (const member of members || []) {
        const entry = node("article", "companion-member");
        entry.dataset.recordKey = member.member_ref;
        entry.append(node("h4", "", member.display_name || member.member_ref || currentMissing));
        entry.append(note(`\u6700\u8fd1\u7528\u6237\u6d3b\u52a8 ${date(member.last_user_at)} \u00b7 \u6700\u8fd1\u8054\u7cfb ${date(member.last_contact_at)} \u00b7 \u7248\u672c ${number(member.version)}`));
        if (member.preference?.recorded === false) entry.append(note("\u8054\u7cfb\u504f\u597d\u672a\u6301\u4e45\u5316"));
        entry.append(SessionContent.invocation({ preference: member.preference ?? null, attention: member.attention ?? null }, "arguments"));
        if (!Array.isArray(member.attention)) entry.append(note("\u5173\u6ce8\u9879\u8bb0\u5f55\u4e0d\u53ef\u7528\u3002", true));
        else if (!member.attention.length) entry.append(note("\u5df2\u663e\u793a\u7684\u5173\u6ce8\u9879\uff1a0"));
        if (member.attention_truncated || member.attention?.some((item) => item.truncated)) entry.append(note("\u5173\u6ce8\u9879\u663e\u793a\u5df2\u622a\u65ad", true));
        memberPanel.content.append(entry);
      }
      if (data.members_truncated) memberPanel.content.append(note("\u6210\u5458\u5217\u8868\u5df2\u622a\u65ad", true));
      root.append(memberPanel.item);
      return root;
    }
    function contactEntry(contact) {
      const entry = node("article", "companion-contact");
      entry.dataset.recordKey = contact.attempt_ref;
      const heading = node("div", "companion-heading");
      const destination = contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : contact.display_name || contact.member_ref || missing;
      heading.append(node("strong", "", destination), badge(contact.status));
      entry.append(heading);
      if (contact.reason) entry.append(node("p", "", reason(contact.reason)));
      if (contact.confirmed_count != null) entry.append(node("p", "companion-contact-outcome", `\u786e\u8ba4\u9001\u8fbe ${number(contact.confirmed_count)} \u6761`));
      else entry.append(note("\u9001\u8fbe\u56de\u6267\u8ba1\u6570\u672a\u8bb0\u5f55", true));
      if (contact.status === "unknown") entry.append(note("\u53d1\u9001\u7ed3\u679c\u672a\u77e5", true));
      const source = navigation(contact.native_turn, "\u67e5\u770b\u8054\u7cfb\u8bb0\u5f55", `contact:${contact.attempt_ref}:turn`);
      if (source) entry.append(source);
      return entry;
    }
    function review(item, fallback, bundle) {
      const root = node("section", "companion-inspection");
      if (!item && !fallback) { root.append(note("\u590d\u76d8\u7ed3\u679c\u672a\u8bb0\u5f55", true)); return root; }
      const record = item || fallback;
      root.dataset.recordKey = item?.review_ref || "captured-reflection";
      const heading = node("header", "companion-heading");
      heading.append(node("h3", "", "\u590d\u76d8\u7ed3\u8bba"), badge(item?.status || "unknown"));
      root.append(heading);
      const summary = record.decision_summary ?? record.proposal?.decision_summary;
      root.append(node("p", "companion-decision-summary", summary || "\u590d\u76d8\u7ed3\u8bba\u672a\u8bb0\u5f55"));
      if (record.reason) root.append(node("p", "companion-review-reason", reason(record.reason)));
      const proposal = node("div", "companion-proposed-contact");
      proposal.append(node("strong", "", proposalStatus(record)));
      const contact = record.contact || record.proposal?.contact;
      if (contact && typeof contact === "object") {
        const destination = contact.target === "channel" ? "\u5f53\u524d\u9891\u9053" : contact.display_name || contact.member_ref || missing;
        proposal.append(node("p", "", `${destination} \u00b7 ${label(contact.purpose)}${contact.topic ? `\uff1a${contact.topic}` : ""}`));
      }
      else if (record.contact_proposed === true) proposal.append(note("\u8054\u7cfb\u63d0\u8bae\u5185\u5bb9\u672a\u8bb0\u5f55", true));
      root.append(proposal);
      if (record.decision?.status) {
        const decision = node("p", "companion-contact-decision");
        decision.append(badge(record.decision.status));
        if (record.decision.reason) decision.append(node("span", "", reason(record.decision.reason)));
        root.append(decision);
      }
      else root.append(note("\u8054\u7cfb\u51b3\u7b56\u672a\u8bb0\u5f55", true));
      const contacts = item && Array.isArray(item.contacts) ? item.contacts : null;
      const actual = node("div", "companion-actual-contact");
      actual.append(node("h4", "", "\u5b9e\u9645\u8054\u7cfb"));
      for (const entry of contacts || []) actual.append(contactEntry(entry));
      if (contacts && !contacts.length && !item.contacts_truncated) actual.append(node("p", "", "\u65e0\u8054\u7cfb\u5c1d\u8bd5"));
      if (!contacts || item?.contacts_truncated) actual.append(note("\u8054\u7cfb\u8bb0\u5f55\u4e0d\u5b8c\u6574\uff0c\u9001\u8fbe\u7ed3\u679c\u672a\u77e5", true));
      root.append(actual);
      const committed = item?.transaction_committed;
      root.append(node("p", "companion-transaction", `\u6570\u636e\u5e93\u63d0\u4ea4\uff1a${committed === true ? "\u5df2\u63d0\u4ea4" : committed === false ? "\u672a\u63d0\u4ea4" : "\u672a\u8bb0\u5f55"}`));
      if (!item) root.append(note("\u72ec\u7acb\u590d\u76d8\u8bb0\u5f55\u7f3a\u5931\uff0c\u63d0\u4ea4\u4e0e\u8054\u7cfb\u7ed3\u679c\u672a\u77e5", true));
      if (!item?.native_turn) root.append(note("\u539f\u751f\u8f6e\u6b21\u672a\u5173\u8054", true));
      if (record.capture && ["state", "proposal", "decision"].some((key) => record.capture[key] && record.capture[key] !== "recorded")) root.append(note("\u90e8\u5206\u5386\u53f2\u8bb0\u5f55\u7f3a\u5931", true));
      if (truncated(record)) root.append(note("\u8bb0\u5f55\u5df2\u622a\u65ad", true));
      const sources = node("div", "actions companion-navigation");
      const native = navigation(item?.native_turn, "\u8c03\u7528\u8be6\u60c5", `review:${item?.review_ref}:native`, true);
      if (native) sources.append(native);
      else {
        const ref = record.events?.decision_event_ref || record.events?.proposal_event_ref || record.events?.state_event_ref || record.events?.context_event_ref;
        if (ref) sources.append(eventButton(ref, "\u539f\u59cb\u8bb0\u5f55", "", bundle || { inspection: null, events: [] }));
      }
      for (const turn of item?.evidence_turns || []) {
        const source = navigation(turn, `\u804a\u5929\u8bc1\u636e #${turn.sequence ?? turn.turn_id ?? "?"}`, `review:${item.review_ref}:evidence:${turn.turn_ref}`);
        if (source) sources.append(source);
      }
      if (sources.childNodes.length) root.append(sources);
      return root;
    }
    return { overview, review };
  }

  global.SessionCompanion = Object.freeze({ create });
})(window);
