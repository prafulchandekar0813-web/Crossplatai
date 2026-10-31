import { api, sse, ApiError } from "./api.js";
import { h, icon, clear, copyText, toast, plural } from "./dom.js";
import { renderMarkdown } from "./md.js";
import { Trace } from "./trace.js";
import { openPreview } from "./preview.js";

const EXAMPLES = [
  ["Build a small to-do web app", "Writes a Python backend and an HTML frontend. Watch the workers split the job.",
    "Build a small to-do web app in a folder called todo: a Python Flask backend that stores tasks in a JSON file, and a plain HTML and JavaScript frontend. Add a README with run instructions."],
  ["See what is in my workspace", "Lists the files you already have and explains what each one does.",
    "List the files in my workspace and tell me briefly what each one is for."],
  ["Teach it how I like answers", "Saves a preference that is used in every future chat.",
    "Remember that I prefer short answers, with code examples when they help."],
];

export const newSessionId = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 16) : Math.random().toString(16).slice(2, 18).padEnd(16, "0"));

export class ChatView {
  constructor({ config, panel, onSessions, onRunning, openSession }) {
    Object.assign(this, { config, panel, onSessions, onRunning, openSession });
    this.sid = null; this.running = false; this.runId = null; this.trace = null; this.abort = null; this.stick = true; this.token = 0;

    this.list = h("div", { class: "chat-inner" });
    this.scroller = h("div", { class: "chat-scroll", onscroll: () => { const s = this.scroller; this.stick = s.scrollTop + s.clientHeight >= s.scrollHeight - 90; } }, this.list);
    this.notice = h("div", { class: "composer-hint", hidden: true });
    this.input = h("textarea", { rows: "1", placeholder: "Describe a task, or ask a question", "aria-label": "Message", maxlength: String(config.max_prompt_chars),
      oninput: () => this.autosize(), onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.submit(); } } });
    this.sendBtn = h("button", { class: "btn primary", onclick: () => (this.running ? this.stop() : this.submit()) });
    this.counter = h("span");
    this.el = h("div", { class: "chat" }, this.scroller,
      h("div", { class: "composer-wrap" },
        h("div", { class: "composer" }, this.input, this.sendBtn),
        this.notice,
        h("div", { class: "composer-hint" }, h("span", null, "Press Enter to send, Shift+Enter for a new line."), this.counter)));
    this.syncComposer();
  }

  // ---- composer ------------------------------------------------------------------------------------
  autosize() {
    this.input.style.height = "auto";
    this.input.style.height = Math.min(this.input.scrollHeight, 220) + "px";
    const left = this.config.max_prompt_chars - this.input.value.length;
    this.counter.textContent = left < this.config.max_prompt_chars * 0.15 ? `${left} characters left` : "";
  }

  syncComposer(stopping = false) {
    this.sendBtn.replaceChildren(this.running ? icon("stop") : icon("send"), this.running ? (stopping ? "Stopping…" : "Stop") : "Send");
    this.sendBtn.disabled = stopping;
    this.sendBtn.setAttribute("aria-label", this.running ? "Stop the running task" : "Send message");
  }

  setNotice(node) { this.notice.hidden = !node; this.notice.replaceChildren(...(node ? [node] : [])); }

  fill(text) { this.input.value = text; this.autosize(); this.input.focus(); }

  scrollEnd(force = false) { if (force || this.stick) requestAnimationFrame(() => { this.scroller.scrollTop = this.scroller.scrollHeight; }); }

  // ---- rendering messages ----------------------------------------------------------------------------
  empty() {
    const ex = EXAMPLES.map(([title, sub, prompt]) => h("button", { class: "example", onclick: () => this.fill(prompt) }, h("div", null, h("b", null, title), h("span", null, sub))));
    return h("div", { class: "empty" },
      h("h2", null, "What should we work on?"),
      h("p", null, "Give the assistant a task. For bigger jobs it makes a plan, shares the work with worker agents and writes files for you, and you can follow each step as it happens."),
      h("div", { class: "examples" }, ...ex),
      h("div", { class: "how" },
        h("p", null, h("b", null, "Every step is visible. "), "Tool calls appear under your message while the assistant works. Click one to see exactly what it sent and received."),
        h("p", null, h("b", null, "Your files stay yours. "), "Anything written lands in Files, in a folder only you can see."),
        h("p", null, h("b", null, "It stops itself. "), "Limits on rounds, tool calls and time end a stuck task and still give you a summary.")));
  }

  addUser(text) { const el = h("div", { class: "turn-user" }, text); this.list.append(el); this.scrollEnd(true); }

  answerNode(content, status) {
    const cls = status === "error" ? "error" : status === "cancelled" ? "cancelled" : "";
    const el = h("div", { class: "answer " + cls });
    if (status === "error" || status === "cancelled") el.textContent = content; else el.append(renderMarkdown(content));
    return el;
  }

  answerTools(content, files = []) {
    const page = [...files].sort((a, b) => a.length - b.length).find((f) => /(^|\/)index\.html?$/i.test(f)) || files.find((f) => /\.html?$/i.test(f));
    return h("div", { class: "answer-tools" },
      page ? h("button", { class: "btn small primary", onclick: () => openPreview(page) }, icon("play"), "Preview app") : null,
      h("button", { class: "btn quiet small", onclick: async () => toast((await copyText(content)) ? "Copied answer" : "Could not copy") }, icon("copy"), "Copy"));
  }

  addAssistant(m) {
    const turn = h("div", { class: "turn-ai" });
    if (m.run_id) {
      const stub = h("button", { class: "btn quiet small", style: { justifySelf: "start" } }, icon("eye"), "Show what happened");
      stub.addEventListener("click", async () => {
        stub.disabled = true;
        try {
          const { run } = await api(`/api/runs/${m.run_id}`);
          const t = new Trace({ open: true, onFile: () => {} });
          t.replay(run.events);
          stub.replaceWith(t.el);
        } catch (e) { stub.disabled = false; toast(e.message, { kind: "bad" }); }
      });
      turn.append(stub);
    }
    turn.append(this.answerNode(m.content, m.status), ...(m.status === "ok" ? [this.answerTools(m.content)] : []));
    this.list.append(turn);
  }

  liveTurn() {
    const turn = h("div", { class: "turn-ai" });
    const answerSlot = h("div");
    this.trace = new Trace({
      open: true, onGrow: () => this.scrollEnd(),
      onChange: (t) => this.panel.render(),
      onFinal: (ev) => {
        const ok = ev.status === "completed" || ev.status === "partial";
        answerSlot.replaceWith(this.answerNode(ev.content, ok ? "ok" : ev.status === "cancelled" ? "cancelled" : "error"));
        if (ok) turn.append(this.answerTools(ev.content, [...this.trace.stats.files]));
        this.scrollEnd();
      },
    });
    turn.append(this.trace.el, answerSlot);
    this.list.append(turn);
    this.panel.show(this.trace);
    this.scrollEnd(true);
    return turn;
  }

  // ---- sessions --------------------------------------------------------------------------------------
  detach() {
    this.token++;
    if (this.abort) this.abort.abort();
    this.abort = null;
    this.trace?.dispose();
    this.trace = null; this.runId = null; this.running = false;
    this.syncComposer();
  }

  async open(sid, { fresh = false } = {}) {
    this.detach();
    this.sid = sid;
    clear(this.list);
    this.setNotice(null);
    this.panel.show(null);
    let data = { messages: [], active_run: null };
    if (!fresh) {
      try { data = await api(`/api/sessions/${sid}/messages`); }
      catch (e) { this.list.append(h("div", { class: "answer error" }, e.message)); return; }
    }
    for (const m of data.messages) (m.role === "user" ? this.addUser(m.content) : this.addAssistant(m));
    if (!data.messages.length && !data.active_run) this.list.append(this.empty());
    this.stick = true; this.scrollEnd(true);
    if (data.active_run) this.attach(data.active_run);
    else this.checkOtherRun();
  }

  async checkOtherRun() {
    try {
      const { active_run } = await api("/api/sessions");
      if (active_run && active_run.session_id !== this.sid) {
        this.setNotice(h("span", null, "A task is still running in another chat. ",
          h("button", { class: "btn quiet small", onclick: () => this.openSession(active_run.session_id) }, "Open it")));
      }
    } catch { /* not important */ }
  }

  // ---- running tasks ---------------------------------------------------------------------------------
  async submit() {
    const text = this.input.value.trim();
    if (!text || this.running) return;
    this.list.querySelector(".empty")?.remove();
    this.input.value = ""; this.autosize();
    this.addUser(text);
    const turn = this.liveTurn();
    this.running = true; this.syncComposer(); this.onRunning(this.sid);
    const my = ++this.token;
    this.abort = new AbortController();
    let started = false;
    try {
      await this.consume(sse(`/api/sessions/${this.sid}/chat`, { method: "POST", body: { prompt: text }, signal: this.abort.signal }), my, () => { started = true; });
    } catch (e) {
      if (e.name === "AbortError" || my !== this.token) return;
      if (!started) {  // the server refused the task: show why and give the text back
        this.trace?.dispose(); this.trace = null; this.panel.show(null);
        turn.replaceChildren(this.answerNode(e.message, "error"));
        this.input.value = text; this.autosize();
        this.running = false; this.syncComposer(); this.onRunning(null);
        return;
      }
      await this.reattach(my);
    }
    this.finishRun(my);
  }

  async attach(run) {
    this.liveTurn();
    this.running = true; this.runId = run.id || run; this.syncComposer(); this.onRunning(this.sid);
    const my = ++this.token;
    this.abort = new AbortController();
    this.lastSeq = -1;
    try { await this.consume(sse(`/api/runs/${this.runId}/events?after=-1`, { signal: this.abort.signal }), my); }
    catch (e) { if (e.name === "AbortError" || my !== this.token) return; await this.reattach(my); }
    this.finishRun(my);
  }

  async consume(stream, my, onStart) {
    for await (const ev of stream) {
      if (my !== this.token) return;
      onStart?.();
      this.lastSeq = ev.seq;
      if (ev.type === "run_started") this.runId = ev.run_id;
      if (ev.type === "file_changed") this.noteFile(ev.path);
      this.trace?.apply(ev);
      if (ev.type === "run_finished") this.sawFinish = true;
    }
  }

  /** Network dropped mid-task: the server keeps working, so keep trying to pick the event stream back up. */
  async reattach(my) {
    this.setNotice(h("span", null, "Connection lost. Trying to reconnect; the task keeps running on the server."));
    for (let i = 0; i < 8 && my === this.token; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        this.abort = new AbortController();
        this.sawFinish = false;
        await this.consume(sse(`/api/runs/${this.runId}/events?after=${this.lastSeq ?? -1}`, { signal: this.abort.signal }), my);
        if (this.sawFinish) { this.setNotice(null); return; }
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) break;
      }
    }
    if (my === this.token) { this.setNotice(null); toast("Lost the connection to the server. Reload the page to see the result.", { kind: "bad" }); }
  }

  finishRun(my) {
    if (my !== this.token) return;
    this.running = false; this.abort = null; this.syncComposer(); this.onRunning(null); this.setNotice(null);
    this.onSessions();
    this.panel.render();
  }

  noteFile(path) { this.filesTouched?.(path); }

  async stop() {
    if (!this.runId) return;
    this.syncComposer(true);
    try { await api(`/api/runs/${this.runId}/cancel`, { method: "POST" }); }
    catch (e) { toast(e.message, { kind: "bad" }); this.syncComposer(false); }
  }
}
