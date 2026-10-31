import { api, ApiError } from "./api.js";
import { h, icon, fmtBytes, timeAgo, plural, toast, confirmDialog } from "./dom.js";

const TABS = [["model", "Model"], ["limits", "Limits"], ["prompt", "Instructions"], ["search", "Web search"], ["users", "Users"], ["maint", "Maintenance"]];

const F = (path, label, type, help, extra = {}) => ({ path, label, type, help, ...extra });
const MODEL_FIELDS = [
  F("openai.base_url", "Model server URL", "text", "Any OpenAI-compatible server, usually ending in /v1. For example http://localhost:30000/v1 (SGLang), http://localhost:11434/v1 (Ollama) or https://openrouter.ai/api/v1.", { placeholder: "http://localhost:30000/v1" }),
  F("openai.model", "Model name", "text", "Exactly as the server expects it."),
  F("openai.api_key", "API key", "password", "Leave empty to keep the saved key. Local servers usually need none.", { placeholder: "Saved keys are never shown" }),
  F("openai.temperature", "Temperature", "number", "Lower is steadier. 0.2 to 0.4 works best when the assistant uses tools.", { min: 0, max: 2, step: 0.1 }),
  F("openai.max_tokens", "Longest reply (tokens)", "number", "Large enough to write whole files. 4096 or more is a good start.", { min: 64, step: 256 }),
  F("openai.timeout_seconds", "Wait for the model (seconds)", "number", "How long one model call may take.", { min: 5 }),
  F("openai.max_retries", "Retries after a temporary error", "number", "For busy or rate-limited servers.", { min: 0, max: 6 }),
  F("openai.retry_without_tools", "If the server rejects tools, carry on as plain chat", "bool", "The user is warned when this happens."),
];
const LIMIT_GROUPS = [
  ["Each task", [
    F("circuit_breaker.max_total_turns", "Reasoning rounds per agent", "number", "How many times one agent may call the model.", { min: 1 }),
    F("circuit_breaker.max_tool_calls", "Tool calls per agent", "number", "", { min: 1 }),
    F("circuit_breaker.max_consecutive_duplicates", "Identical calls before stopping", "number", "Catches an agent that repeats itself.", { min: 2 }),
    F("circuit_breaker.max_consecutive_failures", "Failed calls in a row before stopping", "number", "", { min: 1 }),
    F("circuit_breaker.enable_loop_fingerprinting", "Detect repeated tool calls", "bool", ""),
    F("circuit_breaker.max_delegations", "Workers per task", "number", "0 turns delegation off.", { min: 0 }),
    F("max_subagents_per_user", "Workers running at the same time", "number", "", { min: 1 }),
    F("circuit_breaker.max_run_seconds", "Time limit per task (seconds)", "number", "", { min: 10 }),
    F("circuit_breaker.tool_timeout_seconds", "Time limit per tool call (seconds)", "number", "", { min: 1 }),
    F("max_concurrent_runs", "Tasks running at once across all users", "number", "Takes effect after a restart.", { min: 1 }),
  ]],
  ["Context window", [
    F("context.max_context_tokens", "Model context size (tokens)", "number", "Set this to what your model supports.", { min: 512 }),
    F("context.summary_trigger_tokens", "Start shrinking context at (tokens)", "number", "Older chat is summarised and old tool output is dropped past this point. Keep it below the context size.", { min: 256 }),
    F("context.sliding_window_messages", "Recent messages always kept in full", "number", "", { min: 2 }),
    F("context.max_tool_output_length", "Longest tool result shown to the model (characters)", "number", "", { min: 100 }),
  ]],
  ["Memory and storage", [
    F("memory.max_long_term_facts", "Memories kept per user", "number", "", { min: 1 }),
    F("memory.max_facts_in_prompt", "Memories shown to the model per task", "number", "", { min: 0 }),
    F("memory.ttl_days_transient", "Keep activity details for (days)", "number", "After this the “Show what happened” view expires. Chats and memories stay.", { min: 1 }),
    F("memory.auto_prune_interval_turns", "Clean up expired data every N tasks", "number", "", { min: 1 }),
    F("workspace_quota_mb", "Files per user (MB)", "number", "", { min: 1 }),
    F("max_file_kb", "Largest file the assistant may write (KB)", "number", "", { min: 1 }),
    F("max_upload_mb", "Largest knowledge-base upload (MB)", "number", "", { min: 1 }),
    F("kb_quota_mb", "Knowledge-base storage per user (MB)", "number", "Each user's private documents. Documents added by admins don't count.", { min: 1 }),
    F("max_prompt_chars", "Longest message (characters)", "number", "", { min: 100 }),
  ]],
  ["Accounts", [F("allow_registration", "Let people create their own accounts", "bool", "Turn off once your users have signed up.")]],
];
const SEARCH_FIELDS = [
  F("search.provider", "Provider", "select", "With no provider the assistant is told web search is unavailable and never pretends otherwise.", { options: [["", "Off"], ["searxng", "SearXNG (self-hosted, no key)"], ["brave", "Brave Search (API key)"]] }),
  F("search.base_url", "SearXNG address", "text", "For example http://localhost:8888. The instance must allow the JSON format.", { placeholder: "http://localhost:8888" }),
  F("search.api_key", "Brave API key", "password", "Leave empty to keep the saved key.", { placeholder: "Saved keys are never shown" }),
  F("search.max_results", "Results per search", "number", "", { min: 1, max: 20 }),
];

const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function put(obj, path, v) { const ks = path.split("."); let o = obj; for (const k of ks.slice(0, -1)) o = o[k] ??= {}; o[ks.at(-1)] = v; }

export class AdminView {
  constructor({ onSaved, tab = "model" }) { this.onSaved = onSaved; this.tab = tab; this.el = h("div"); }

  async load() {
    try { this.config = (await api("/api/admin/config")).config; }
    catch (e) { this.el.replaceChildren(h("div", { class: "page" }, h("div", { class: "form-error" }, e.message))); return; }
    this.render();
  }

  go(tab) { this.tab = tab; this.render(); }

  render() {
    const tabs = h("div", { class: "tabs", role: "tablist" }, ...TABS.map(([id, label]) =>
      h("button", { class: "tab", role: "tab", "aria-selected": String(id === this.tab), onclick: () => this.go(id) }, label)));
    const body = h("div");
    this.el.replaceChildren(h("div", { class: "page" }, h("h2", null, "Admin"),
      h("p", { class: "lede" }, "Connect a model, set safety limits and manage people. Changes apply to the next task; a task that is already running keeps the settings it started with."), tabs, body));
    ({ model: () => this.model(body), limits: () => this.limits(body), prompt: () => this.prompt(body), search: () => this.searchTab(body),
       users: () => this.users(body), maint: () => this.maint(body) })[this.tab]();
  }

  // ---- generic form --------------------------------------------------------------------------------
  fields(fields) {
    const inputs = {};
    const nodes = fields.map((f) => {
      const locked = this.config.env_locked.includes(f.path);
      const id = "f-" + f.path.replace(/\./g, "-");
      let value = get(this.config, f.path);
      let input;
      if (f.type === "bool") input = h("input", { type: "checkbox", id, checked: value ? "" : null, disabled: locked || null, style: { width: "18px", height: "18px" } });
      else if (f.type === "select") input = h("select", { id, disabled: locked || null }, ...f.options.map(([v, l]) => h("option", { value: v, selected: v === value ? "" : null }, l)));
      else input = h("input", { id, type: f.type === "number" ? "number" : f.type, value: f.type === "password" ? "" : value ?? "", min: f.min, max: f.max, step: f.step,
        placeholder: f.type === "password" && get(this.config, f.path.replace(/api_key$/, "api_key_set")) ? "A key is saved. Type to replace it." : f.placeholder, disabled: locked || null,
        autocomplete: f.type === "password" ? "new-password" : "off" });
      inputs[f.path] = { input, f, locked };
      const help = [f.help, locked ? "Set by an environment variable, so it can't be changed here." : ""].filter(Boolean).join(" ");
      if (f.type === "bool") return h("div", { class: "field" }, h("label", { for: id, style: { display: "flex", gap: "10px", alignItems: "center", fontWeight: 600 } }, input, f.label), help ? h("span", { class: "help" }, help) : null);
      return h("div", { class: "field" }, h("label", { for: id }, f.label), input, help ? h("span", { class: "help" }, help) : null);
    });
    return { nodes, collect: () => {
      const patch = {};
      for (const { input, f, locked } of Object.values(inputs)) {
        if (locked) continue;
        if (f.type === "bool") put(patch, f.path, input.checked);
        else if (f.type === "number") { if (input.value !== "") put(patch, f.path, Number(input.value)); }
        else if (f.type === "password") { if (input.value) put(patch, f.path, input.value); }
        else put(patch, f.path, input.value.trim());
      }
      return patch;
    } };
  }

  async save(patch, button) {
    button.disabled = true;
    try {
      this.config = (await api("/api/admin/config", { method: "PUT", body: patch })).config;
      toast("Saved. New settings apply to the next task.");
      this.onSaved?.();
      return true;
    } catch (e) {
      toast(e.message, { kind: "bad", timeout: 9000 });
      return false;
    } finally { button.disabled = false; }
  }

  // ---- tabs ----------------------------------------------------------------------------------------
  model(body) {
    const { nodes, collect } = this.fields(MODEL_FIELDS);
    const result = h("div");
    const saveBtn = h("button", { class: "btn primary", onclick: async () => { if (await this.save(collect(), saveBtn)) this.render(); } }, "Save");
    const testBtn = h("button", { class: "btn", onclick: async () => {
      if (!(await this.save(collect(), testBtn))) return;
      testBtn.disabled = true; result.replaceChildren(h("div", { class: "result" }, h("span", { class: "spinner" }), "Contacting the model…"));
      try { this.showTest(result, await api("/api/admin/test-llm", { method: "POST" })); }
      catch (e) { result.replaceChildren(h("div", { class: "result bad" }, e.message)); }
      testBtn.disabled = false;
    } }, "Save and test connection");
    const clearKey = this.config.openai.api_key_set && !this.config.env_locked.includes("openai.api_key")
      ? h("button", { class: "btn quiet danger", onclick: async () => { if (await this.save({ openai: { api_key: null } }, clearKey)) this.render(); } }, "Remove saved key") : null;
    body.append(h("div", { class: "fields" }, ...nodes, h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } }, saveBtn, testBtn, clearKey), result));
  }

  showTest(box, r) {
    if (!r.ok) { box.replaceChildren(h("div", { class: "result bad" }, h("b", null, "Could not reach the model"), h("span", null, r.error))); return; }
    const tool = {
      supported: ["ok", "Tool calling works. Files, search and worker agents are available."],
      not_used: ["warn", "The model answered without using the test tool. It may handle tools poorly; try a task and watch the activity view."],
      rejected: ["bad", "The server rejected tool calling. The assistant can chat but can't write files or delegate. Use a model or server mode that supports tools."],
      unknown: ["warn", "Could not tell whether tool calling works."],
    }[r.tool_calling];
    box.replaceChildren(h("div", { class: "result ok" }, h("b", null, "Connected"), h("span", { class: "muted" }, `Replied “${r.reply.trim().slice(0, 60)}” in ${r.latency_ms} ms.`)),
      h("div", { class: "result " + (tool[0] === "ok" ? "ok" : tool[0] === "bad" ? "bad" : "") }, h("b", null, "Tools"), h("span", { class: "muted" }, tool[1])));
  }

  limits(body) {
    const groups = LIMIT_GROUPS.map(([title, fields]) => ({ title, ...this.fields(fields) }));
    const save = h("button", { class: "btn primary", onclick: async () => {
      const patch = {}; for (const g of groups) Object.assign(patch, ...[], g.collect());
      const merged = {}; for (const g of groups) deepAssign(merged, g.collect());
      if (await this.save(merged, save)) this.render();
    } }, "Save");
    body.append(h("div", { class: "fields" }, ...groups.flatMap((g) => [h("h3", { style: { margin: "6px 0 14px" } }, g.title), ...g.nodes]), save));
  }

  prompt(body) {
    const ta = h("textarea", { rows: "16", "aria-label": "Assistant instructions", style: { fontFamily: "var(--mono)", fontSize: "13px" }, disabled: this.config.env_locked.includes("system_prompt") || null }, this.config.system_prompt);
    const save = h("button", { class: "btn primary", onclick: async () => { if (await this.save({ system_prompt: ta.value }, save)) this.render(); } }, "Save");
    const reset = h("button", { class: "btn quiet", onclick: async () => { if (await this.save({ system_prompt: "" }, reset)) this.render(); } }, "Restore the default");
    body.append(h("div", null, h("p", { class: "muted", style: { marginBottom: "12px", maxWidth: "62ch" } }, "How the assistant should work. The list of tools, the date, saved memories and the rule that tool output is never treated as instructions are added automatically, so editing this can't make the assistant claim abilities it doesn't have."),
      ta, h("div", { style: { display: "flex", gap: "8px", marginTop: "12px" } }, save, reset),
      this.config.system_prompt_is_default ? h("p", { class: "faint", style: { marginTop: "10px", fontSize: "13px" } }, "You are using the built-in default.") : null));
  }

  searchTab(body) {
    const { nodes, collect } = this.fields(SEARCH_FIELDS);
    const save = h("button", { class: "btn primary", onclick: async () => { if (await this.save(collect(), save)) this.render(); } }, "Save");
    body.append(h("div", { class: "fields" }, ...nodes, save));
  }

  async users(body) {
    let data;
    try { data = await api("/api/admin/users"); } catch (e) { body.append(h("div", { class: "form-error" }, e.message)); return; }
    const rows = data.users.map((u) => h("div", { class: "list-row" }, h("span", { class: "avatar" }, u.user_id[0].toUpperCase()),
      h("div", { class: "grow" }, u.user_id, h("small", null, `${plural(u.messages, "message")}, ${plural(u.facts, "memory", "memories")}, joined ${timeAgo(u.created_at)}`)),
      h("button", { class: "icon-btn", title: "Delete user", "aria-label": `Delete ${u.user_id}`, onclick: async () => {
        if (!(await confirmDialog(`Delete ${u.user_id}?`, "Their account, chats, memories and files are removed for good.", "Delete user", true))) return;
        try { await api(`/api/admin/users/${encodeURIComponent(u.user_id)}`, { method: "DELETE" }); toast("User deleted"); this.render(); } catch (e) { toast(e.message, { kind: "bad" }); }
      } }, icon("trash"))));
    body.append(h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, "No users yet"), "People can create an account from the sign-in page.")])));
  }

  async maint(body) {
    let s;
    try { s = await api("/api/admin/stats"); } catch (e) { body.append(h("div", { class: "form-error" }, e.message)); return; }
    const stat = (n, label) => h("div", { class: "stat" }, h("b", null, n), h("span", null, label));
    body.append(h("div", { class: "stat-grid" }, stat(s.users, "users"), stat(s.messages, "messages"), stat(s.facts, "memories"), stat(s.run_traces, "stored activity logs"),
      stat(s.knowledge.documents, "knowledge documents"), stat(s.running_tasks, "tasks running now"), stat(fmtBytes(s.database_bytes), "database size")),
      h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
        h("button", { class: "btn", onclick: async (e) => { e.target.disabled = true; try { const r = await api("/api/admin/prune", { method: "POST" }); toast(`Removed ${r.pruned_run_traces} expired activity logs and ${r.pruned_login_sessions} old sign-ins`); this.render(); } catch (err) { toast(err.message, { kind: "bad" }); } } }, icon("refresh"), "Clear expired data now"),
        h("button", { class: "btn danger", onclick: async () => {
          if (!(await confirmDialog("Restore default settings?", "Limits, instructions and search settings go back to their defaults. Your admin password and saved API keys are kept.", "Restore defaults", true))) return;
          try { this.config = (await api("/api/admin/config/reset", { method: "POST" })).config; toast("Defaults restored"); this.onSaved?.(); this.render(); } catch (e) { toast(e.message, { kind: "bad" }); }
        } }, "Restore default settings")));
  }
}

function deepAssign(target, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === "object" && !Array.isArray(v)) deepAssign(target[k] ??= {}, v); else target[k] = v;
  }
}
