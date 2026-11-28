// Review: for the administrator and managers. Skill proposals from the curator, the assistant's flags, the skills themselves
// and plain counters. Nothing here shows a chat, a file or a key.
import { api } from "./api.js";
import { h, icon, timeAgo, plural, toast, confirmDialog } from "./dom.js";

const TABS = [["proposals", "Proposals"], ["flags", "Flags"], ["skills", "Skills"], ["numbers", "Numbers"]];
const say = (e) => toast(e.message || String(e), { kind: "bad" });

export class ReviewView {
  constructor() { this.el = h("div", { class: "page" }); this.tab = "proposals"; this.extra = []; }

  /** Other tabs (for example Templates) can be added by the shell before load(). */
  addTab(id, label, render) { this.extra.push([id, label, render]); }

  async load() { this.paint(); }

  paint() {
    const tabs = [...TABS, ...this.extra.map(([id, label]) => [id, label])];
    const body = h("div", { style: { marginTop: "14px" } });
    this.el.replaceChildren(h("h2", null, "Review"),
      h("p", { class: "lede" }, "Help the assistant get better without reading anyone's chats. You see generic notes and counters only."),
      h("div", { class: "tabs", role: "tablist" }, ...tabs.map(([id, label]) => h("button", { class: "tab", role: "tab", "aria-selected": String(id === this.tab),
        onclick: () => { this.tab = id; this.paint(); } }, label))), body);
    const extra = this.extra.find(([id]) => id === this.tab);
    (extra ? extra[2](body) : this[this.tab](body)).catch((e) => body.append(h("div", { class: "form-error" }, e.message || String(e))));
  }

  async proposals(body) {
    const { proposals } = await api("/api/review/proposals");
    const run = h("button", { class: "btn primary", onclick: async () => {
      run.disabled = true;
      try { const r = await api("/api/review/curator/run", { method: "POST" }); toast(r.message); this.paint(); } catch (e) { say(e); run.disabled = false; }
    } }, icon("brain"), "Ask the curator for proposals");
    body.append(h("p", { class: "muted" }, "The curator reads the flags and counters and drafts changes to skills. Nothing changes until you approve it."), run,
      ...(proposals.length ? proposals.map((p) => this.proposal(p)) : [h("div", { class: "empty-note" }, h("b", null, "No proposals waiting"), "Run the curator once there are some flags.")]));
  }

  proposal(p) {
    const text = h("textarea", { rows: "10", "aria-label": `Text of ${p.name}`, style: { width: "100%" } }, p.body);
    const old = p.current ? h("details", null, h("summary", null, "Current text"), h("pre", { class: "diff-old" }, p.current.body)) : null;
    const decide = (what) => async () => {
      if (what === "approve" && !(await confirmDialog(`Approve ${p.name}?`, "It goes live for every assistant straight away. You can revert it later in Skills.", "Approve"))) return;
      try { await api(`/api/review/proposals/${p.id}/${what}`, { method: "POST", body: what === "approve" ? { body: text.value } : {} }); toast(what === "approve" ? "Approved" : "Rejected"); this.paint(); } catch (e) { say(e); }
    };
    return h("div", { class: "card", style: { margin: "12px 0", padding: "14px" } },
      h("div", null, h("b", null, p.name), h("span", { class: "chip", style: { marginLeft: "8px" } }, p.action === "add" ? "new skill" : "edit"), h("small", { style: { marginLeft: "8px" } }, timeAgo(p.created_at))),
      h("p", { class: "muted" }, p.reason || "No reason given."), h("p", null, p.description), old,
      h("label", { class: "small" }, "Proposed text (you can edit it before approving)"), text,
      h("div", { class: "form-row", style: { marginTop: "8px" } },
        h("button", { class: "btn primary", onclick: decide("approve") }, "Approve"), h("button", { class: "btn", onclick: decide("reject") }, "Reject")));
  }

  async flags(body) {
    const { flags } = await api("/api/review/flags");
    body.append(h("p", { class: "muted" }, "Generic notes the assistant wrote about guides that were missing, wrong or out of date. They hold no user names and are deleted after 30 days."),
      ...(flags.length ? flags.map((f) => h("div", { class: "list-row" },
        h("div", { class: "grow" }, h("b", null, f.topic || "(no topic)"), h("span", { class: "chip", style: { marginLeft: "8px" } }, f.kind), f.count > 1 ? h("span", { class: "chip", style: { marginLeft: "6px" } }, `${f.count} times`) : null,
          h("small", null, f.note + (f.suggestion ? `  Suggestion: ${f.suggestion}` : "") + `  (${timeAgo(f.last_seen)})`)),
        h("button", { class: "btn small", onclick: async () => { try { await api(`/api/review/flags/${f.id}/status`, { method: "POST", body: { status: "dismissed" } }); this.paint(); } catch (e) { say(e); } } }, "Dismiss")))
        : [h("div", { class: "empty-note" }, h("b", null, "No open flags"), "They appear as the assistant works.")]));
  }

  async skills(body) {
    const { skills } = await api("/api/review/skills");
    const edit = async (name) => {
      const got = name ? await api(`/api/review/skills/${encodeURIComponent(name)}`) : { name: "", description: "", body: "", source: "added" };
      const n = h("input", { type: "text", value: got.name, maxlength: "40", placeholder: "playbook-fitness", ...(name ? { readonly: "" } : {}), "aria-label": "Skill name" });
      const d = h("input", { type: "text", value: got.description, maxlength: "240", placeholder: "One line: when to use it", "aria-label": "Description" });
      const t = h("textarea", { rows: "14", style: { width: "100%" }, "aria-label": "Skill text" }, got.body);
      const save = h("button", { class: "btn primary", onclick: async () => {
        try { await api(`/api/review/skills/${encodeURIComponent(n.value)}`, { method: "PUT", body: { description: d.value, body: t.value } }); toast("Saved"); this.paint(); } catch (e) { say(e); }
      } }, "Save");
      body.replaceChildren(h("button", { class: "btn small", onclick: () => this.paint() }, "Back"), h("h3", null, name || "New skill"), n, d, t,
        h("p", { class: "muted small" }, "Shared by every assistant: no names, emails, keys or personal details."), save);
    };
    body.append(h("button", { class: "btn", onclick: () => edit("") }, icon("plus"), "Add a skill"),
      ...skills.map((s) => h("div", { class: "list-row" },
        h("div", { class: "grow" }, h("b", null, s.name), h("span", { class: "chip", style: { marginLeft: "8px" } }, s.source), h("small", null, s.description)),
        h("button", { class: "btn small", onclick: () => edit(s.name) }, "Edit"),
        s.source === "built-in" ? null : h("button", { class: "btn small", onclick: async () => {
          if (!(await confirmDialog(`Revert ${s.name}?`, s.source === "added" ? "The skill is removed." : "It goes back to the built-in text.", "Revert", true))) return;
          try { await api(`/api/review/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" }); this.paint(); } catch (e) { say(e); }
        } }, "Revert"))));
  }

  async numbers(body) {
    const { metrics } = await api("/api/review/flags");
    const get = (k) => metrics[k] || 0;
    const tools = [...new Set(Object.keys(metrics).filter((k) => k.startsWith("tool.")).map((k) => k.split(".")[1]))].sort();
    const avg = (a, b) => (get(b) ? (get(a) / get(b)).toFixed(1) : "-");
    body.append(h("p", { class: "muted" }, "Plain counters for the last 14 days from users who allow them."),
      h("div", { class: "list" },
        h("div", { class: "list-row" }, h("div", { class: "grow" }, h("b", null, "Tasks"), h("small", null, `${get("task.completed")} completed, ${get("task.partial")} partial, ${get("task.error")} failed, ${get("task.checkpoints")} checkpoints used`))),
        h("div", { class: "list-row" }, h("div", { class: "grow" }, h("b", null, "Design notes per check"), h("small", null, `first check ${avg("design.first_notes", "design.first_checks")}, last check ${avg("design.last_notes", "design.last_checks")} (lower is better)`)))),
      h("h3", null, "Tools"),
      ...(tools.length ? tools.map((t) => { const ok = get(`tool.${t}.ok`), err = get(`tool.${t}.err`);
        return h("div", { class: "list-row" }, h("div", { class: "grow" }, h("b", null, t), h("small", null, `${plural(ok + err, "call")}, ${err} failed (${ok + err ? Math.round((err / (ok + err)) * 100) : 0}%)`))); })
        : [h("div", { class: "empty-note" }, h("b", null, "No numbers yet"))]));
  }
}
