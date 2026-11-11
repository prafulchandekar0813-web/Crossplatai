// Turns the server's event stream into the activity view: a vertical line with one node per step,
// where every worker agent branches off as its own colour-coded lane.
import { h, icon, fmtDuration, plural } from "./dom.js";

const TOOL_ICON = { plan_task: "plan", delegate_task: "branch", write_file: "file", patch_file: "file", edit_lines: "file", insert_text: "file", read_file: "eye", list_files: "folder",
  web_search: "search", read_url: "eye", knowledgebase_search: "book", remember: "bookmark" };
const LANE_HUES = [215, 165, 285, 25, 340, 95];
const STATUS_LABEL = { completed: "Worked for", partial: "Stopped early after", error: "Failed after", cancelled: "Stopped by you after", timeout: "Timed out after" };
const STATUS_TONE = { completed: "ok", partial: "warn", error: "bad", cancelled: "warn", timeout: "bad" };
const AGENT_TAG = { queued: ["Waiting", ""], running: ["Working", "accent"], completed: ["Done", "ok"], partial: ["Stopped early", "warn"],
  error: ["Failed", "bad"], cancelled: ["Cancelled", "warn"], stopped: ["Stopped", "warn"] };

export const agentName = (a) => (a.id === "orchestrator" ? "Assistant" : a.role);

export class Trace {
  constructor({ open = true, onGrow = () => {}, onFile = () => {}, onChange = () => {}, onFinal = () => {} } = {}) {
    Object.assign(this, { onGrow, onFile, onChange, onFinal });
    this.status = "idle";
    this.agents = new Map();
    this.calls = new Map();
    this.laneCount = 0;
    this.now = "";
    this.stats = { tokensIn: 0, tokensOut: 0, llmCalls: 0, files: new Set(), startedAt: 0, durationMs: 0, plan: null };
    this.ticker = null;
    this.open = open;

    this.statusEl = h("span", { class: "spinner" });
    this.labelEl = h("span", { class: "label" }, "Activity");
    this.nowEl = h("span", { class: "now" });
    this.head = h("button", { class: "trace-head", "aria-expanded": String(open), onclick: () => this.setOpen(!this.open) },
      this.statusEl, this.labelEl, this.nowEl, icon("chevron", "chev"));
    this.rootSteps = h("div", { class: "steps" });
    this.body = h("div", { class: "trace-body" }, this.rootSteps);
    this.el = h("div", { class: "trace", "data-open": String(open) }, this.head, this.body);
    this.body.hidden = !open;
    this.agents.set("orchestrator", { id: "orchestrator", role: "Assistant", parent: null, status: "running", steps: this.rootSteps,
      turns: 0, tools: 0, think: null });
  }

  setOpen(open) {
    this.open = open;
    this.el.dataset.open = String(open);
    this.head.setAttribute("aria-expanded", String(open));
    this.body.hidden = !open;
    if (open) this.onGrow();
  }

  get running() { return this.status === "running"; }
  get workers() { return [...this.agents.values()].filter((a) => a.id !== "orchestrator"); }

  replay(events) { for (const ev of events) this.apply(ev, true); this.renderHead(); }

  // ---- DOM helpers ---------------------------------------------------------------------------------
  ensureAgent(id, parent, role, task) {
    let a = this.agents.get(id);
    if (a) { if (role) a.role = role; return a; }
    const hue = LANE_HUES[this.laneCount++ % LANE_HUES.length];
    a = { id, role: role || "Worker", parent, status: "queued", turns: 0, tools: 0, think: null, hue, task: task || "" };
    this.agents.set(id, a);
    a.tag = h("span", { class: "tag" });
    a.meta = h("span", { class: "step-meta" });
    a.steps = h("div", { class: "steps" });
    a.report = h("div", { class: "lane-report", hidden: true });
    a.lane = h("div", { class: "lane", style: { "--h": hue } },
      h("div", { class: "lane-head" }, icon("branch"), h("b", null, a.role), a.tag, a.meta),
      task ? h("p", { class: "lane-task" }, task) : null, a.steps, a.report);
    (this.agents.get(parent) || this.agents.get("orchestrator")).steps.append(a.lane);
    this.paintAgent(a);
    return a;
  }

  paintAgent(a) {
    if (!a.tag) return;
    const [label, tone] = AGENT_TAG[a.status] || [a.status, ""];
    a.tag.className = "tag " + tone;
    a.tag.textContent = label;
    a.lane.querySelector("b").textContent = a.role;
  }

  addStep(agent, { kind = "tool", ico = "file", text, state = "running" }) {
    const textEl = h("span", { class: "step-text" }, text);
    const metaEl = h("span", { class: "step-meta" });
    const line = h("div", { class: "step-line" }, icon(ico), textEl, metaEl);
    const row = h("div", { class: "step " + kind, "data-state": state }, h("span", { class: "node" }), line);
    agent.steps.append(row);
    this.onGrow();
    let detailEl = null;
    return {
      row,
      setState: (s) => { row.dataset.state = s; },
      setMeta: (t) => { metaEl.textContent = t; },
      setSub: (t) => { if (t) row.querySelector(".step-sub")?.remove(), line.after(h("div", { class: "step-sub" }, t)); },
      setDetail: (obj) => {
        const text = JSON.stringify(obj, null, 2);
        line.classList.add("has-detail");
        line.tabIndex = 0; line.setAttribute("role", "button"); line.setAttribute("aria-expanded", "false");
        const toggle = () => {
          if (!detailEl) { detailEl = h("pre", { class: "step-detail" }, text); row.append(detailEl); }
          else detailEl.hidden = !detailEl.hidden;
          line.setAttribute("aria-expanded", String(!detailEl.hidden));
          this.onGrow();
        };
        line.addEventListener("click", toggle);
        line.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      },
    };
  }

  info(agent, ico, text, kind = "info", state = "info") {
    const s = this.addStep(agent, { kind, ico, text, state });
    return s;
  }

  // ---- events --------------------------------------------------------------------------------------
  apply(ev, replaying = false) {
    // Run-level events (run_started, final, run_finished) carry no agent; they must not create a lane.
    const a = ev.type === "agent_queued" || !ev.agent ? this.agents.get("orchestrator") : this.agents.get(ev.agent) || this.ensureAgent(ev.agent, "orchestrator");
    switch (ev.type) {
      case "run_started":
        this.status = "running"; this.stats.startedAt = ev.ts * 1000;
        if (!replaying) this.startTicker();
        break;
      case "queued": this.info(a, "think", ev.message); break;
      case "agent_queued": this.ensureAgent(ev.agent, ev.parent, ev.role, ev.task); break;
      case "agent_started": { const w = this.ensureAgent(ev.agent, ev.parent, ev.role); w.status = "running"; w.startedAt = ev.ts; this.paintAgent(w); break; }
      case "llm_start":
        a.turns++;
        a.think = this.addStep(a, { kind: "think", ico: "think", text: a.id === "orchestrator" ? "Thinking…" : `${a.role} is thinking…` });
        this.now = a.id === "orchestrator" ? "Thinking…" : `${a.role} is thinking…`;
        break;
      case "llm_end":
        a.think?.row.remove(); a.think = null;
        this.stats.llmCalls++;
        this.stats.tokensIn += ev.usage?.prompt_tokens || 0; this.stats.tokensOut += ev.usage?.completion_tokens || 0;
        break;
      case "assistant_note": this.info(a, "think", ev.text, "note", "ok"); break;
      case "plan": {
        this.stats.plan = ev;
        const s = this.addStep(a, { kind: "plan", ico: "plan", text: "Made a plan", state: "ok" });
        s.row.append(h("div", { class: "plan" }, h("b", null, ev.title), h("ol", null, ...ev.steps.map((t) => h("li", null, t)))));
        break;
      }
      case "tool_start": {
        a.tools++;
        const s = this.addStep(a, { ico: TOOL_ICON[ev.tool] || "file", text: ev.summary });
        this.calls.set(ev.call_id, { s, args: ev.args, summary: ev.summary });
        this.now = ev.summary;
        break;
      }
      case "tool_end": {
        const c = this.calls.get(ev.call_id);
        if (!c) break;
        c.s.setState(ev.ok ? "ok" : "err");
        c.s.setMeta(ev.duration_ms >= 50 ? fmtDuration(ev.duration_ms) : "");
        if (ev.summary && ev.summary !== c.summary) c.s.setSub(ev.summary);
        c.s.setDetail({ arguments: c.args, result: ev.preview });
        break;
      }
      case "file_changed": this.stats.files.add(ev.path); this.onFile(ev.path, ev.action, replaying); break;
      case "memory_saved": this.info(a, "bookmark", `Saved to memory: ${ev.fact}`, "info", "ok"); break;
      case "context_compacted": this.info(a, "think", "Shortened older tool output to save space in the model's context"); break;
      case "context_summarised": this.info(a, "think", ev.message); break;
      case "warning": this.info(a, "alert", ev.message, "warn", "warn"); break;
      case "breaker": this.info(a, "alert", `Safety limit reached: ${ev.reason}`, "warn", "warn"); break;
      case "agent_finished": {
        a.status = ev.status; a.duration = ev.duration_ms; a.think?.row.remove(); a.think = null;
        this.paintAgent(a);
        if (a.report && ev.summary) { a.report.hidden = false; a.report.textContent = ev.summary; }
        a.meta && (a.meta.textContent = fmtDuration(ev.duration_ms));
        break;
      }
      case "final": this.onFinal(ev); break;
      case "run_finished":
        this.status = ev.status; this.stats.durationMs = ev.duration_ms; this.stopTicker();
        for (const w of this.agents.values()) if (w.status === "running" || w.status === "queued") { w.status = "stopped"; this.paintAgent(w); }
        this.rootSteps.querySelectorAll('.step[data-state="running"]').forEach((r) => { r.dataset.state = "warn"; });
        this.agents.get("orchestrator").status = ev.status === "completed" ? "completed" : ev.status;
        break;
    }
    if (!replaying) { this.renderHead(); this.onChange(this); }
  }

  // ---- header --------------------------------------------------------------------------------------
  elapsed() { return this.running ? Date.now() - this.stats.startedAt : this.stats.durationMs; }
  startTicker() { this.stopTicker(); this.ticker = setInterval(() => { this.renderHead(); this.onChange(this); }, 500); }
  stopTicker() { if (this.ticker) clearInterval(this.ticker); this.ticker = null; }
  dispose() { this.stopTicker(); }

  renderHead() {
    const s = this.stats;
    if (this.running) {
      this.statusEl.className = "spinner";
      this.labelEl.textContent = `Working for ${fmtDuration(this.elapsed())}`;
      this.nowEl.replaceChildren(this.now);
      return;
    }
    const tone = STATUS_TONE[this.status];
    this.statusEl.className = "dot " + (tone || "");
    this.labelEl.textContent = this.status === "idle" ? "Activity" : `${STATUS_LABEL[this.status] || "Finished"} ${fmtDuration(s.durationMs)}`;
    const chips = [];
    const nW = this.workers.length;
    if (nW) chips.push(h("span", { class: "tag" }, plural(nW, "worker")));
    if (s.files.size) chips.push(h("span", { class: "tag" }, plural(s.files.size, "file")));
    if (s.llmCalls) chips.push(h("span", { class: "tag" }, plural(s.llmCalls, "model call")));
    this.nowEl.replaceChildren(...chips);
  }
}

// ---- right-hand panel -------------------------------------------------------------------------------------
export class RunPanel {
  constructor(limits, onOpenFile) {
    this.limits = limits;
    this.onOpenFile = onOpenFile;
    this.el = h("aside", { class: "panel", "aria-label": "Task details" });
    this.show(null);
  }

  show(trace) {
    this.trace = trace;
    this.render();
  }

  meter(label, value, max) {
    const pct = Math.min(100, (value / max) * 100);
    return h("div", { class: "meter" },
      h("div", { class: "meter-top" }, h("span", null, label), h("span", { class: "faint" }, `${value} of ${max}`)),
      h("div", { class: "meter-bar" }, h("i", { class: pct >= 80 ? "hot" : "", style: { width: pct + "%" } })));
  }

  render() {
    const t = this.trace;
    const L = this.limits;
    if (!t) {
      this.el.replaceChildren(
        h("section", null, h("h3", null, "Task details"),
          h("p", { class: "muted" }, "When you send a task, this panel shows which agents are working, what they touched and how close they are to their limits.")),
        h("section", null, h("h3", null, "Limits for each task"),
          h("dl", { class: "kv" },
            h("dt", null, "Reasoning rounds per agent"), h("dd", null, L.max_total_turns),
            h("dt", null, "Tool calls per agent"), h("dd", null, L.max_tool_calls),
            h("dt", null, "Workers per task"), h("dd", null, L.max_delegations),
            h("dt", null, "Time limit"), h("dd", null, fmtDuration(L.max_run_seconds * 1000)))));
      return;
    }
    const tone = STATUS_TONE[t.status] || "";
    const word = { running: "Working", completed: "Done", partial: "Stopped early", error: "Failed", cancelled: "Stopped", timeout: "Timed out", idle: "Ready" }[t.status];
    const root = t.agents.get("orchestrator");
    const files = [...t.stats.files];
    this.el.replaceChildren(
      h("section", null, h("div", { class: "status-big" }, t.running ? h("span", { class: "spinner" }) : h("span", { class: "dot " + tone }), word,
        h("small", null, fmtDuration(t.elapsed())))),
      h("section", null, h("h3", null, "Agents"),
        ...[...t.agents.values()].map((a) => h("div", { class: "agent-row", style: a.hue != null ? { "--h": a.hue } : {} },
          a.id === "orchestrator" ? h("span", { class: "dot " + (a.status === "running" ? "running" : STATUS_TONE[a.status] || "") }) : h("span", { class: "swatch" }),
          h("span", { class: "name" }, agentName(a)),
          h("small", null, a.status === "queued" ? "waiting" : `${plural(a.tools, "tool call")}`))),
        L.max_delegations ? h("p", { class: "faint", style: { fontSize: "13px", marginTop: "6px" } }, `${t.workers.length} of ${L.max_delegations} workers used`) : null),
      h("section", null, h("h3", null, "Assistant limits"),
        this.meter("Reasoning rounds", root.turns, L.max_total_turns), this.meter("Tool calls", root.tools, L.max_tool_calls)),
      files.length ? h("section", null, h("h3", null, "Files written"),
        ...files.map((f) => h("button", { class: "file-link", title: "Open in Files", onclick: () => this.onOpenFile(f) }, icon("file"), h("span", null, f)))) : null,
      h("section", null, h("h3", null, "Model usage"),
        h("dl", { class: "kv" }, h("dt", null, "Model calls"), h("dd", null, t.stats.llmCalls),
          h("dt", null, "Tokens read"), h("dd", null, t.stats.tokensIn.toLocaleString()),
          h("dt", null, "Tokens written"), h("dd", null, t.stats.tokensOut.toLocaleString()))));
  }
}
