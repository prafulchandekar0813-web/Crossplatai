import { api, ApiError, onUnauthorized } from "./api.js";
import { h, icon, brandMark, toast, confirmDialog, timeAgo } from "./dom.js";
import { ChatView, newSessionId } from "./chat.js";
import { RunPanel } from "./trace.js";
import { FilesView, MemoryView, KnowledgeView, passwordDialog } from "./views.js";
import { AdminView } from "./admin.js";
import { openPreview } from "./preview.js";

const root = document.getElementById("app");
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

let state = { user: null, config: null };
let shell = null;

// ---------------------------------------------------------------------------------------- boot
async function boot() {
  try { state = await api("/api/bootstrap"); }
  catch (e) { root.replaceChildren(h("div", { class: "page" }, h("h2", null, "Can't reach the server"), h("p", { class: "lede" }, e.message),
    h("button", { class: "btn primary", onclick: boot }, "Try again"))); return; }
  state.user ? showApp() : showAuth();
}

onUnauthorized(() => { if (state.user) { state.user = null; toast("You were signed out. Please sign in again.", { kind: "bad" }); showAuth(); } });

// ---------------------------------------------------------------------------------------- auth
function showAuth() {
  shell = null;
  let mode = state.config.allow_registration ? "register" : "login";
  const render = () => {
    const err = h("div", { class: "form-error", hidden: true });
    const user = h("input", { id: "au", type: "text", autocomplete: "username", required: "", maxlength: "32", autocapitalize: "none", spellcheck: "false" });
    const pass = h("input", { id: "ap", type: "password", autocomplete: mode === "login" ? "current-password" : "new-password", required: "", minlength: mode === "login" ? null : "8" });
    const btn = h("button", { class: "btn primary", type: "submit", style: { width: "100%" } }, mode === "login" ? "Sign in" : "Create account");
    const submit = async (e) => {
      e.preventDefault(); err.hidden = true; btn.disabled = true;
      try {
        const r = await api(`/api/auth/${mode}`, { method: "POST", body: { username: user.value.trim(), password: pass.value } });
        state.user = r.user; showApp();
      } catch (x) { err.hidden = false; err.textContent = x.message; btn.disabled = false; }
    };
    const form = h("form", { onsubmit: submit },
      h("h2", null, mode === "login" ? "Welcome back" : "Create your account"),
      h("p", { class: "muted", style: { marginBottom: "20px" } }, mode === "login" ? "Sign in to continue." : "Pick a username and a password of at least 8 characters."),
      err,
      h("div", { class: "field" }, h("label", { for: "au" }, "Username"), user, mode === "register" ? h("span", { class: "help" }, "3 to 32 letters, numbers, dots, dashes or underscores.") : null),
      h("div", { class: "field" }, h("label", { for: "ap" }, "Password"), pass),
      btn,
      state.config.allow_registration ? h("p", { class: "switch" }, mode === "login" ? "New here? " : "Already have an account? ",
        h("button", { type: "button", class: "btn quiet small", onclick: () => { mode = mode === "login" ? "register" : "login"; render(); } }, mode === "login" ? "Create an account" : "Sign in")) : null);
    root.replaceChildren(h("div", { class: "auth" },
      h("div", { class: "auth-story" }, h("div", { class: "brand", style: { padding: 0, color: "var(--accent)" } }, brandMark(), h("span", { style: { color: "var(--ink)" } }, "Agent Workbench")),
        h("h1", null, "Give it a task. Watch it work."),
        h("p", null, "An assistant that plans, hands parts of the job to worker agents, writes files and tells you honestly what it did. Every step is visible while it happens."),
        h("p", { class: "auth-mini muted" }, "Your chats, files and memories are private to your account.")),
      h("div", { class: "auth-form" }, form)));
    user.focus();
  };
  render();
}

// ---------------------------------------------------------------------------------------- app shell
const VIEWS = { chat: "Chat", files: "Files", memory: "Memory", knowledge: "Knowledge base", admin: "Admin" };

function showApp() {
  const isAdmin = state.user.role === "admin";
  const panel = new RunPanel(state.config.limits, (path) => go("files", path));
  const files = new FilesView(), memory = new MemoryView(), knowledge = new KnowledgeView({ isAdmin });
  const admin = isAdmin ? new AdminView({ onSaved: refreshConfig }) : null;
  const chat = new ChatView({ config: state.config, panel, onSessions: () => loadSessions(), onRunning: (sid) => { runningSid = sid; renderSessions(); }, openSession: (sid) => openChat(sid) });
  let sessions = [], runningSid = null, current = "chat", newFiles = 0;
  chat.filesTouched = () => { if (current !== "files") { newFiles++; renderNav(); } };

  const sessionsEl = h("div", { class: "sessions", "aria-label": "Chats" });
  const navEl = h("nav", { "aria-label": "Sections" });
  const bannerEl = h("div");
  const titleEl = h("h1");
  const viewEl = h("div", { class: "view" });
  const mainEl = h("div", { class: "main" }, h("div", { class: "topbar" },
    h("button", { class: "icon-btn menu-btn", "aria-label": "Open menu", onclick: () => document.body.classList.toggle("rail-open") }, icon("menu")), titleEl), bannerEl, viewEl);
  const shellEl = h("div", { class: "shell" });

  // account footer
  const themeBtn = h("button", { class: "icon-btn", title: "Switch light / dark", "aria-label": "Switch light or dark theme", onclick: toggleTheme }, icon("moon"));
  const account = h("div", { class: "account" }, h("span", { class: "avatar" }, state.user.user_id[0].toUpperCase()),
    h("div", { class: "who" }, h("b", null, state.user.user_id), h("span", null, isAdmin ? "Administrator" : "Member")),
    themeBtn,
    h("button", { class: "icon-btn", title: "Change password", "aria-label": "Change password", onclick: passwordDialog }, icon("sliders")),
    h("button", { class: "icon-btn", title: "Sign out", "aria-label": "Sign out", onclick: async () => {
      if (runningSid && !(await confirmDialog("Sign out?", "A task is still running. It will keep going, but you won't see it until you sign back in.", "Sign out"))) return;
      chat.detach(); try { await api("/api/auth/logout", { method: "POST" }); } catch { /* ignore */ }
      state.user = null; history.replaceState(null, "", location.pathname); showAuth();
    } }, icon("logout")));

  const rail = h("aside", { class: "rail" },
    h("div", { class: "brand", style: { color: "var(--accent)" } }, brandMark(), h("span", { style: { color: "var(--ink)" } }, "Agent Workbench")),
    h("button", { class: "btn new-chat", onclick: () => openChat(newSessionId(), true) }, icon("plus"), "New chat"),
    sessionsEl, navEl, account);

  shellEl.append(rail, mainEl, panel.el);
  root.replaceChildren(shellEl);
  document.body.classList.remove("rail-open");
  document.body.addEventListener("click", (e) => { if (e.target === document.body) document.body.classList.remove("rail-open"); });

  function toggleTheme() {
    const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next; store.set("workbench.theme", next);
  }

  // ---- sessions
  async function loadSessions() {
    try { const d = await api("/api/sessions"); sessions = d.sessions; if (d.active_run) runningSid = d.active_run.session_id; } catch { /* keep old list */ }
    renderSessions();
  }
  function renderSessions() {
    const list = [...sessions];
    if (chat.sid && !list.some((s) => s.id === chat.sid)) list.unshift({ id: chat.sid, title: "New chat", updated_at: Date.now() / 1000, messages: 0, draft: true });
    sessionsEl.replaceChildren(list.length ? h("h3", null, "Recent chats") : h("p", { class: "faint", style: { padding: "10px" } }, "Your chats will appear here."),
      ...list.map((s) => h("div", { class: "session", "aria-current": String(s.id === chat.sid && current === "chat"), role: "button", tabindex: "0",
        onclick: () => openChat(s.id), onkeydown: (e) => { if (e.key === "Enter") openChat(s.id); } },
        s.id === runningSid ? h("span", { class: "dot running", title: "A task is running here" }) : null,
        h("span", { class: "title", title: s.title }, s.title),
        h("button", { class: "icon-btn del", title: "Delete chat", "aria-label": `Delete chat ${s.title}`, onclick: async (e) => {
          e.stopPropagation();
          if (s.id === runningSid) { toast("Stop the running task first.", { kind: "bad" }); return; }
          if (s.draft) { openChat(newSessionId(), true); return; }
          if (!(await confirmDialog("Delete this chat?", "The messages are removed for good. Files and memories stay.", "Delete", true))) return;
          try { await api(`/api/sessions/${s.id}`, { method: "DELETE" }); } catch (x) { toast(x.message, { kind: "bad" }); return; }
          if (s.id === chat.sid) openChat(newSessionId(), true); else loadSessions();
        } }, icon("trash")))));
  }

  async function openChat(sid, fresh = false) {
    current = "chat"; store.set("workbench.session", sid);
    history.replaceState(null, "", "#/chat/" + sid);
    await chat.open(sid, { fresh }); render(); loadSessions();
    document.body.classList.remove("rail-open");
  }

  // ---- navigation
  function renderNav() {
    const items = [["preview", "play", "Preview app"], ["files", "folder", "Files"], ["memory", "brain", "Memory"], ["knowledge", "book", "Knowledge base"], ...(isAdmin ? [["admin", "sliders", "Admin"]] : [])];
    navEl.replaceChildren(...items.map(([id, ic, label]) => h("button", { class: "nav-item", "aria-current": current === id ? "page" : "false", onclick: () => (id === "preview" ? openPreview() : go(id)) },
      icon(ic), label, id === "files" && newFiles ? h("span", { class: "badge", title: "Files written since you last looked" }, String(newFiles)) : null)));
  }

  function go(view, arg) {
    if (view === "chat") return openChat(chat.sid || newSessionId(), !chat.sid);
    current = view; document.body.classList.remove("rail-open");
    history.replaceState(null, "", "#/" + view);
    if (view === "files") { newFiles = 0; files.load(arg); }
    if (view === "memory") memory.load();
    if (view === "knowledge") knowledge.load();
    if (view === "admin") admin.load();
    render();
  }

  function render() {
    titleEl.textContent = current === "chat" ? "Chat" : VIEWS[current];
    const node = { chat: chat.el, files: files.el, memory: memory.el, knowledge: knowledge.el, admin: admin?.el }[current];
    viewEl.replaceChildren(node);
    viewEl.style.overflow = current === "chat" ? "hidden" : "auto";
    shellEl.classList.toggle("no-panel", current !== "chat");
    panel.el.hidden = current !== "chat";
    renderNav(); renderSessions(); renderBanner();
  }

  function renderBanner() {
    bannerEl.replaceChildren();
    if (state.config.llm_ready) return;
    bannerEl.append(h("div", { class: "banner" }, icon("alert"), isAdmin ? "No model is connected yet, so the assistant can't answer." : "The assistant isn't set up yet. Ask your administrator to connect a model.",
      isAdmin ? h("button", { class: "btn small", onclick: () => go("admin") }, "Connect a model") : null));
  }

  async function refreshConfig() {
    try { state = { ...state, ...(await api("/api/bootstrap")) }; } catch { return; }
    chat.config = state.config; panel.limits = state.config.limits; panel.render(); renderBanner();
  }

  // ---- start
  (async () => {
    renderNav();
    await loadSessions();
    const m = location.hash.match(/^#\/(chat\/([\w-]+)|files|memory|knowledge|admin)$/);
    if (m && m[1] === "files" || m && ["memory", "knowledge"].includes(m[1]) || (m && m[1] === "admin" && isAdmin)) { await chat.open(newSessionId(), { fresh: true }); go(m[1]); return; }
    const sid = (m && m[2]) || store.get("workbench.session");
    if (sid && sessions.some((s) => s.id === sid)) openChat(sid); else openChat(newSessionId(), true);
  })();
}

boot();
