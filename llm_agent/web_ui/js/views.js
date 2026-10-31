import { api, upload, ApiError } from "./api.js";
import { h, icon, clear, fmtBytes, timeAgo, plural, toast, confirmDialog, copyText } from "./dom.js";
import { renderMarkdown } from "./md.js";
import { openPreview } from "./preview.js";

const page = (title, lede, ...body) => h("div", { class: "page" }, h("h2", null, title), h("p", { class: "lede" }, lede), ...body);
const emptyNote = (title, text) => h("div", { class: "empty-note" }, h("b", null, title), text);
const errorNote = (e) => h("div", { class: "form-error" }, e.message || String(e));

// ================================================================================ Files
export class FilesView {
  constructor() {
    this.el = h("div"); this.entries = []; this.open = new Set(); this.selected = null; this.mode = "source";
  }

  async load(select) {
    if (select) this.selected = select;
    try {
      const data = await api("/api/workspace");
      this.entries = data.entries; this.usage = data.usage_bytes; this.quota = data.quota_bytes;
      if (!this.seeded) { this.entries.filter((e) => e.type === "dir").forEach((e) => this.open.add(e.path)); this.seeded = true; }
      if (select) { let p = ""; for (const part of select.split("/").slice(0, -1)) { p = p ? `${p}/${part}` : part; this.open.add(p); } }
    } catch (e) { this.el.replaceChildren(page("Files", "", errorNote(e))); return; }
    this.render();
  }

  tree() {
    const rows = [];
    const visible = (path) => { const parts = path.split("/"); let p = ""; for (const part of parts.slice(0, -1)) { p = p ? `${p}/${part}` : part; if (!this.open.has(p)) return false; } return true; };
    for (const e of this.entries) {
      if (!visible(e.path)) continue;
      const depth = e.path.split("/").length - 1;
      const name = e.path.split("/").pop();
      const isDir = e.type === "dir";
      const row = h("button", { class: "tree-row", style: { paddingLeft: 8 + depth * 16 + "px" }, "aria-current": String(e.path === this.selected),
        onclick: () => { if (isDir) { this.open.has(e.path) ? this.open.delete(e.path) : this.open.add(e.path); this.render(); } else { this.selected = e.path; this.mode = "source"; this.render(); } } },
        icon(isDir ? "folder" : "file"), h("span", null, name), isDir ? null : h("small", { class: "faint", style: { marginLeft: "auto" } }, fmtBytes(e.size)));
      rows.push(isDir ? h("div", { class: "tree-item" }, row, h("a", { class: "icon-btn", style: { width: "28px", height: "28px", flex: "none" }, title: `Download ${name} as .zip`,
        "aria-label": `Download folder ${name}`, href: `/api/workspace/zip?path=${encodeURIComponent(e.path)}` }, icon("download")),
        h("button", { class: "icon-btn", style: { width: "28px", height: "28px", flex: "none" }, title: `Delete folder ${name}`, "aria-label": `Delete folder ${name}`, onclick: () => this.deleteFolder(e.path) }, icon("trash"))) : row);
    }
    return rows;
  }

  async deleteFolder(path) {
    const inside = this.entries.filter((e) => e.type === "file" && e.path.startsWith(path + "/")).length;
    if (!(await confirmDialog(`Delete the folder “${path}”?`, `${plural(inside, "file")} inside will be removed for good. Download it first if you want a copy.`, "Delete folder", true))) return;
    try {
      await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" });
      if (this.selected && this.selected.startsWith(path + "/")) this.selected = null;
      toast("Folder deleted"); this.load();
    } catch (e) { toast(e.message, { kind: "bad" }); }
  }

  render() {
    const pct = Math.min(100, (this.usage / this.quota) * 100);
    const files = this.entries.filter((e) => e.type === "file").length;
    const left = this.entries.length
      ? h("div", { class: "tree", role: "tree", "aria-label": "Workspace files" }, ...this.tree())
      : h("div", { class: "list" }, emptyNote("No files yet", "Ask the assistant to write something, for example “Create a README for my project”."));
    this.viewer = h("div");
    this.el.replaceChildren(page("Files", "Everything the assistant has written for you. Only you can see this folder.",
      this.entries.length ? h("div", { style: { margin: "0 0 14px" } }, h("a", { class: "btn small", href: "/api/workspace/zip" }, icon("download"), "Download everything (.zip)")) : null,
      h("div", { class: "usage" }, h("div", { class: "meter-top" }, h("span", null, plural(files, "file")), h("span", { class: "faint" }, `${fmtBytes(this.usage)} of ${fmtBytes(this.quota)}`)),
        h("div", { class: "meter-bar" }, h("i", { class: pct > 80 ? "hot" : "", style: { width: pct + "%" } }))),
      h("div", { class: "split" }, left, this.viewer)));
    if (this.selected) this.showFile(this.selected);
    else if (this.entries.length) this.viewer.append(h("div", { class: "list" }, emptyNote("Pick a file", "Choose a file on the left to read it here.")));
  }

  async showFile(path) {
    const v = this.viewer;
    v.replaceChildren(h("div", { class: "viewer" }, h("div", { class: "empty-note" }, "Loading…")));
    let f;
    try { f = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`); }
    catch (e) { v.replaceChildren(errorNote(e)); return; }
    if (this.selected !== path) return;
    const ext = path.split(".").pop().toLowerCase();
    const canPreview = !f.binary && (ext === "md" || ext === "html" || ext === "htm");
    const head = h("div", { class: "viewer-head" }, h("b", { title: path }, path));
    if (ext === "html" || ext === "htm") {
      head.append(h("button", { class: "btn small primary", onclick: () => openPreview(path) }, icon("play"), "Open preview"));
    } else if (canPreview) {
      head.append(h("button", { class: "btn small", onclick: () => { this.mode = this.mode === "preview" ? "source" : "preview"; this.showFile(path); } },
        icon("eye"), this.mode === "preview" ? "Show source" : "Preview"));
    }
    head.append(
      h("button", { class: "icon-btn", title: "Copy contents", "aria-label": "Copy contents", onclick: async () => toast((await copyText(f.content)) ? "Copied" : "Could not copy") }, icon("copy")),
      h("a", { class: "icon-btn", title: "Download", "aria-label": "Download", href: `/api/workspace/download?path=${encodeURIComponent(path)}` }, icon("download")),
      h("button", { class: "icon-btn", title: "Delete", "aria-label": "Delete file", onclick: async () => {
        if (!(await confirmDialog("Delete this file?", `${path} will be removed from your workspace.`, "Delete", true))) return;
        try { await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, { method: "DELETE" }); this.selected = null; toast("Deleted"); this.load(); }
        catch (e) { toast(e.message, { kind: "bad" }); }
      } }, icon("trash")));
    let body;
    if (f.binary) body = h("div", { class: "empty-note" }, h("b", null, "Binary file"), "It can't be shown here. Use Download to open it.");
    else if (this.mode === "preview" && ext === "md") body = renderMarkdown(f.content);
    else if (this.mode === "preview") {
      body = h("div", null, h("iframe", { sandbox: "", srcdoc: f.content, title: "HTML preview" }),
        h("p", { class: "faint", style: { padding: "8px 14px", fontSize: "13px" } }, "Preview only: scripts are switched off, so interactive parts won't run."));
    } else body = h("pre", { tabindex: "0" }, f.content + (f.truncated ? "\n\n… (file continues; download it to see everything)" : ""));
    v.replaceChildren(h("div", { class: "viewer" }, head, body));
  }
}

// ================================================================================ Memory
export class MemoryView {
  constructor() { this.el = h("div"); }

  async load() {
    let data;
    try { data = await api("/api/memory"); } catch (e) { this.el.replaceChildren(page("Memory", "", errorNote(e))); return; }
    const input = h("input", { type: "text", placeholder: "For example: I work in Python and prefer short answers", "aria-label": "New memory", maxlength: "300" });
    const cat = h("select", { "aria-label": "Type" }, h("option", { value: "general" }, "General"), h("option", { value: "preference" }, "Preference"), h("option", { value: "profile" }, "About me"));
    const add = async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      try { await api("/api/memory", { method: "POST", body: { fact: input.value, category: cat.value } }); this.load(); }
      catch (err) { toast(err.message, { kind: "bad" }); }
    };
    const rows = data.facts.map((f) => h("div", { class: "list-row" },
      h("div", { class: "grow" }, f.fact, h("small", null, `${{ general: "General", preference: "Preference", profile: "About me" }[f.category] || f.category}, saved ${timeAgo(f.created_at)} ${f.source === "user" ? "by you" : "by the assistant"}`)),
      h("button", { class: "icon-btn", title: "Forget this", "aria-label": "Forget this", onclick: async () => { await api(`/api/memory/${f.id}`, { method: "DELETE" }); this.load(); } }, icon("trash"))));
    this.el.replaceChildren(page("Memory", "Facts the assistant keeps between chats. It saves things when you ask it to remember them, and you can add or remove anything here.",
      h("form", { class: "form-row", onsubmit: add }, input, cat, h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Add")),
      h("p", { class: "faint", style: { marginBottom: "10px", fontSize: "13.5px" } }, `${data.facts.length} of ${data.limit} memories used`),
      h("div", { class: "list" }, ...(rows.length ? rows : [emptyNote("Nothing remembered yet", "Try saying “Remember that I prefer short answers” in a chat.")]))));
  }
}

// ================================================================================ Knowledge
export class KnowledgeView {
  constructor({ isAdmin }) { this.isAdmin = isAdmin; this.el = h("div"); }

  async load(notes = []) {
    let data;
    this.notes = h("div", { style: { display: "grid", gap: "8px", margin: "0 0 14px" } },
      ...notes.map(([text, ok]) => h("div", { class: "form-error", style: ok ? "background:color-mix(in srgb, var(--ok) 14%, transparent);color:var(--ok)" : "" }, text)));
    try { data = await api("/api/knowledge"); } catch (e) { this.el.replaceChildren(page("Knowledge base", "", errorNote(e))); return; }
    const results = h("div");
    const q = h("input", { type: "text", placeholder: "Try a question about your documents", "aria-label": "Search the knowledge base", maxlength: "300" });
    const search = async (e) => {
      e.preventDefault();
      if (!q.value.trim()) return;
      try {
        const r = await api(`/api/knowledge/search?q=${encodeURIComponent(q.value)}`);
        results.replaceChildren(h("div", { class: "list" }, ...(r.matches.length
          ? r.matches.map((m) => h("div", { class: "hit" }, h("b", null, m.filename), m.section ? h("span", { class: "faint" }, `  ${m.section}`) : null, h("p", null, m.excerpt)))
          : [emptyNote("No matching passages", r.note || "Nothing in the documents matches those words. Try different terms.")])));
      } catch (err) { results.replaceChildren(errorNote(err)); }
    };
    const rows = data.documents.map((d) => h("div", { class: "list-row" }, icon("file"),
      h("div", { class: "grow" }, d.filename, h("small", null, `${fmtBytes(d.size_bytes)}, added ${timeAgo(d.modified_at)}`)),
      h("button", { class: "icon-btn", title: "Remove", "aria-label": "Remove document", onclick: async () => {
        if (!(await confirmDialog("Remove this document?", `${d.filename} will no longer be searchable.`, "Remove", true))) return;
        try { await api(`/api/knowledge/${encodeURIComponent(d.filename)}`, { method: "DELETE" }); this.load(); } catch (err) { toast(err.message, { kind: "bad" }); }
      } }, icon("trash"))));
    const blocks = [];
    if (data.quota_bytes) {
      const pct = Math.min(100, (data.usage_bytes / data.quota_bytes) * 100);
      blocks.push(h("div", { class: "meter", style: { marginBottom: "14px", maxWidth: "360px" } },
        h("div", { class: "meter-top" }, h("span", null, "Your storage"), h("span", { class: "faint" }, `${fmtBytes(data.usage_bytes)} of ${fmtBytes(data.quota_bytes)}`)),
        h("div", { class: "meter-bar" }, h("i", { class: pct >= 80 ? "hot" : "", style: { width: pct + "%" } }))));
    }
    {
      const file = h("input", { type: "file", multiple: true, hidden: true, accept: ".txt,.md,.markdown,.csv,.json,.html,.htm,.pdf,.docx,.pptx,.xlsx" });
      const drop = h("div", { class: "drop" }, icon("upload"), h("p", null, "Drop documents here, or ", h("button", { class: "btn small", onclick: () => file.click() }, "choose files")),
        h("p", { class: "faint", style: { fontSize: "13px", marginTop: "4px" } }, `Text, Markdown, HTML, CSV, JSON, PDF (with selectable text), DOCX, PPTX and XLSX are supported.`));
      const send = async (files) => {
        const log = [];
        for (const f of files) {
          log.push(h("div", { class: "form-error", style: { background: "var(--sunken)", color: "var(--ink-2)" } }, h("span", { class: "spinner" }), ` Adding ${f.name}…`));
          this.notes.replaceChildren(...log);
          const row = log[log.length - 1];
          try { const r = await upload(`/api/knowledge/${encodeURIComponent(f.name)}`, f); row.replaceChildren(`Added ${r.filename}`); row.style.cssText = "background:color-mix(in srgb, var(--ok) 14%, transparent);color:var(--ok)"; }
          catch (err) { row.replaceChildren(`${f.name} was not added. ${err.message}`); row.style.cssText = ""; }
        }
        await this.load(log.filter((r) => r.textContent.includes("was not added") || r.textContent.startsWith("Added")).map((r) => [r.textContent, r.textContent.startsWith("Added")]));
      };
      file.addEventListener("change", () => send([...file.files]));
      drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); send([...e.dataTransfer.files]); });
      blocks.push(drop, file);
    }
    this.el.replaceChildren(page("Knowledge base", data.shared ? "Shared reference documents. Every user's assistant can search them, but users can't see this list or the file names."
      : "Your own documents. Only you can see them, and the assistant searches them when your question needs them.",
      ...blocks, this.notes,
      h("div", { class: "list", style: { marginBottom: "26px" } }, ...(rows.length ? rows : [emptyNote("No documents yet", "Add some above and the assistant can start using them.")])),
      h("h3", { style: { marginBottom: "10px" } }, "Try a search"),
      h("form", { class: "form-row", onsubmit: search }, q, h("button", { class: "btn", type: "submit" }, icon("search"), "Search")), results));
  }
}

// ================================================================================ Account dialog
export function passwordDialog() {
  const cur = h("input", { type: "password", autocomplete: "current-password", id: "pw-cur" });
  const nw = h("input", { type: "password", autocomplete: "new-password", id: "pw-new" });
  const err = h("div", { class: "form-error", hidden: true });
  const dlg = h("dialog", null, h("h3", null, "Change password"),
    h("p", { class: "muted", style: { marginBottom: "14px" } }, "Other devices will be signed out."), err,
    h("div", { class: "field" }, h("label", { for: "pw-cur" }, "Current password"), cur),
    h("div", { class: "field" }, h("label", { for: "pw-new" }, "New password"), nw, h("span", { class: "help" }, "At least 8 characters.")),
    h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        try { await api("/api/auth/password", { method: "POST", body: { current: cur.value, new: nw.value } }); dlg.close(); toast("Password changed"); }
        catch (e) { err.hidden = false; err.textContent = e.message; }
      } }, "Change password")));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg); dlg.showModal(); cur.focus();
}
