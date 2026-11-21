// "Designer" screen: start from a template or a blank page, list your designs, open the editor.
import { api, ApiError } from "../api.js";
import { h, icon, toast, confirmDialog, timeAgo, plural } from "../dom.js";
import { applyTheme, renderPage } from "./render.js";
import { Editor } from "./editor.js";

export function preview(design) {
  const box = h("div", { class: "tpl-preview", inert: true, "aria-hidden": "true" });
  const inner = h("div", { class: "tpl-inner" });
  applyTheme(inner, design.theme);
  inner.append(renderPage(design.pages[0], { logo: "", go: () => {} }));
  box.append(inner);
  return box;
}

export class DesignerView {
  constructor({ onOpenChange } = {}) { this.el = h("div"); this.onOpenChange = onOpenChange; }

  /** Fetch what the gallery and the editor need. Returns an error message, or "" when all is well. */
  async ensure() {
    try {
      [this.spec, this.templates] = await Promise.all([api("/api/designs/blocks"), api("/api/designs/templates").then((r) => r.templates)]);
    } catch (e) { return e.message; }
    this.locked = ""; this.mine = []; this.apps = [];
    try { const r = await api("/api/designs"); this.mine = r.designs; this.apps = r.apps; }
    catch (e) { if (e instanceof ApiError && e.status === 402) this.locked = e.message; else return e.message; }
    return "";
  }

  async load() {
    if (this.editor) return;   // keep an open editor as it is
    this.el.replaceChildren(h("div", { class: "page" }, h("p", { class: "muted" }, "Loading…")));
    const problem = await this.ensure();
    if (this.editor) return;   // an editor was opened (from Manage app) while this was loading
    if (problem) { this.el.replaceChildren(h("div", { class: "page" }, h("div", { class: "form-error" }, problem))); return; }
    this.paint();
  }

  /** Open one design in the editor, for example from Manage app. */
  async openById(id) {
    const problem = await this.ensure();
    try {
      if (problem) throw new Error(problem);
      if (this.locked) throw new Error(this.locked);
      this.open(await api(`/api/designs/${id}`));
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  paint() {
    const lock = this.locked ? h("div", { class: "banner" }, icon("alert"), this.locked) : null;
    const start = (template) => async (e) => {
      if (this.locked) { toast(this.locked, { kind: "error" }); return; }
      e.currentTarget.disabled = true;
      try { this.open(await api("/api/designs", { method: "POST", body: { name: "", template } })); }
      catch (err) { toast(err.message, { kind: "error" }); e.currentTarget.disabled = false; }
    };
    const templates = this.templates.map((t) => h("div", { class: "tpl" }, preview(t.design), h("strong", null, t.name), h("p", { class: "muted" }, t.description),
      h("button", { class: "btn small primary", onclick: start(t.key) }, "Use this template")));
    const blank = h("div", { class: "tpl" }, h("div", { class: "tpl-preview blank" }, icon("plus")), h("strong", null, "Blank"), h("p", { class: "muted" }, "Start from an empty page."),
      h("button", { class: "btn small", onclick: start("") }, "Start blank"));
    const mine = this.mine.length ? this.mine.map((d) => h("div", { class: "row" },
      h("div", null, h("strong", null, d.name), h("div", { class: "muted" }, `${plural(d.pages, "page")} · ${d.apps.length ? `used by ${plural(d.apps.length, "app")}` : "not used yet"} · ${timeAgo(d.updated_at)}`)),
      h("span", { class: "row-actions" },
        h("button", { class: "btn small primary", onclick: async () => { try { this.open(await api(`/api/designs/${d.id}`)); } catch (e) { toast(e.message, { kind: "error" }); } } }, "Open"),
        h("button", { class: "btn small danger", title: "Delete", onclick: async () => {
          if (!(await confirmDialog("Delete this design?", `"${d.name}" will be removed${d.apps.length ? " and its apps go back to their own screens" : ""}.`, "Delete", true))) return;
          try { await api(`/api/designs/${d.id}`, { method: "DELETE" }); this.load(); } catch (e) { toast(e.message, { kind: "error" }); }
        } }, icon("trash")))))
      : [h("p", { class: "muted" }, this.locked ? "Designs are part of the Pro and Master plans." : "Nothing yet. Pick a template above.")];
    this.el.replaceChildren(h("div", { class: "page wide" }, h("h2", null, "Designer"),
      h("p", { class: "lede" }, "Pick a template or start blank, then drag blocks onto your pages. Use the finished design as the screens of one of your apps."), lock,
      h("h3", null, "Templates"), h("div", { class: "tpl-grid" }, blank, ...templates),
      h("h3", null, "Your designs"), h("div", { class: "list" }, ...mine)));
  }

  open(record) {
    this.editor = new Editor({ record, spec: this.spec, apps: this.apps, onExit: () => { this.editor = null; this.onOpenChange?.(false); this.load(); } });
    this.el.replaceChildren(this.editor.el);
    this.el.parentElement && (this.el.parentElement.scrollTop = 0);   // the list was scrolled; start the editor at its top
    this.onOpenChange?.(true);
  }
}
