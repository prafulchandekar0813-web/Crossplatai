// The design editor: palette, canvas with drag and drop, page tabs, and a properties panel built from the server's block definitions.
import { api, postFile } from "../api.js";
import { h, icon, toast, confirmDialog } from "../dom.js";
import { applyTheme, renderPage } from "./render.js";
import { iconSvg } from "./icons.js";
import { newBlock, find, locate, remove, move, insert, duplicate, dropActionsTo, freshId, instantiate, placeBeside, outOfRow, depth, count, walk } from "./model.js";

const AUTOSAVE_MS = 800;
const COALESCE_MS = 700;
const label = (s) => s.charAt(0).toUpperCase() + s.slice(1);

export class Editor {
  /** record: {id, name, design, apps}; spec: from /api/designs/blocks; apps: the user's apps; onExit(): back to the list. */
  constructor({ record, spec, apps, onExit }) {
    this.id = record.id; this.name = record.name; this.design = record.design; this.linked = record.apps || [];
    this.sections = record.sections || [{ name: "products", label: "Products" }];
    this.spec = spec; this.apps = apps; this.onExit = onExit;
    this.pageId = this.design.pages[0].id; this.selected = null; this.device = "phone";
    this.undo = []; this.redo = []; this.lastKey = ""; this.lastAt = 0; this.timer = null; this.dirty = false;
    this.status = h("span", { class: "muted ed-status" }, "Saved");
    this.el = h("div", { class: "editor" });
    this.paint();
  }

  get page() { return this.design.pages.find((p) => p.id === this.pageId) || this.design.pages[0]; }

  // ---------------------------------------------------------------- changing the design (history + autosave)
  change(key) {
    const now = Date.now();
    if (!(key && key === this.lastKey && now - this.lastAt < COALESCE_MS)) { this.undo.push(this.before); if (this.undo.length > 100) this.undo.shift(); }
    this.redo = []; this.lastKey = key || ""; this.lastAt = now;
    this.after();
  }
  /** Run an edit: remember the state before it, apply it, redraw. `key` lets a burst of typing count as one step. */
  edit(fn, key = "", { props = false } = {}) {
    this.before = JSON.stringify(this.design);
    fn();
    const lim = this.spec.limits;
    const problem = this.design.pages.some((p) => depth(p.blocks) >= lim.depth) ? `Boxes can be nested only ${lim.depth - 1} deep.`
      : count(this.design) > lim.blocks ? `A design can hold up to ${lim.blocks} blocks.` : "";
    if (problem) { this.design = JSON.parse(this.before); toast(problem, { kind: "error" }); this.drawAll(); return; }     // undo the change that went too far
    this.change(key);
    this.drawCanvas(); this.drawTabs(); this.drawActions();
    if (props) this.drawProps();
  }
  after() {
    this.dirty = true; this.status.textContent = "Unsaved changes";
    clearTimeout(this.timer); this.timer = setTimeout(() => this.save(), AUTOSAVE_MS);
    this.undoBtn && (this.undoBtn.disabled = !this.undo.length); this.redoBtn && (this.redoBtn.disabled = !this.redo.length);
  }
  restore(json) {
    this.design = JSON.parse(json);
    if (!this.design.pages.some((p) => p.id === this.pageId)) this.pageId = this.design.pages[0].id;
    if (this.selected && !find(this.page, this.selected)) this.selected = null;
    this.lastKey = ""; this.drawAll(); this.after();
  }
  doUndo() { if (!this.undo.length) return; this.redo.push(JSON.stringify(this.design)); this.restore(this.undo.pop()); }
  doRedo() { if (!this.redo.length) return; this.undo.push(JSON.stringify(this.design)); this.restore(this.redo.pop()); }

  async save() {
    clearTimeout(this.timer);
    if (!this.dirty) return true;
    this.status.textContent = "Saving…";
    const sent = JSON.stringify(this.design);
    try {
      await api(`/api/designs/${this.id}`, { method: "PUT", body: { name: this.name, design: this.design } });
      if (JSON.stringify(this.design) === sent) { this.dirty = false; this.status.textContent = "Saved"; }
      else this.after();
      return true;
    } catch (e) { this.status.textContent = `Not saved: ${e.message}`; return false; }
  }
  async exit() { if (await this.save()) this.onExit(); else if (await confirmDialog("Leave without saving?", "Your latest changes were not saved.", "Leave", true)) this.onExit(); }

  // ---------------------------------------------------------------- layout
  paint() {
    this.undoBtn = h("button", { class: "btn small quiet", title: "Undo", disabled: true, onclick: () => this.doUndo() }, icon("undo"));
    this.redoBtn = h("button", { class: "btn small quiet", title: "Redo", disabled: true, onclick: () => this.doRedo() }, icon("redo"));
    const name = h("input", { class: "ed-name", value: this.name, maxlength: "60", "aria-label": "Design name", oninput: (e) => { this.before = JSON.stringify(this.design); this.name = e.target.value; this.after(); } });
    const dev = (d, ic) => h("button", { class: "btn small quiet dev", "aria-pressed": String(this.device === d), title: label(d), "data-d": d, onclick: () => {
      this.device = d; this.drawCanvas();
      this.el.querySelectorAll(".dev").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.d === d)));
    } }, icon(ic));
    this.palette = h("div", { class: "ed-palette" });
    this.tabs = h("div", { class: "ed-tabs" });
    this.canvas = h("div", { class: "ed-canvas" });
    this.props = h("div", { class: "ed-props" });
    this.actions = h("div", { class: "ed-actions" });
    const bar = h("div", { class: "ed-bar" },
      h("button", { class: "btn small quiet", onclick: () => this.exit() }, icon("chevron", "flip"), "Designs"), name,
      this.undoBtn, this.redoBtn, h("span", { class: "ed-devs" }, dev("phone", "phone"), dev("desktop", "desktop")),
      this.status, h("button", { class: "btn small primary", onclick: () => this.useDialog() }, "Use for my app"));
    this.el.replaceChildren(bar, h("div", { class: "ed-body" }, this.palette, h("div", { class: "ed-center" }, this.tabs, this.canvas, this.actions), this.props));
    this.drawAll();
  }
  drawAll() { this.drawPalette(); this.drawTabs(); this.drawCanvas(); this.drawActions(); this.drawProps(); this.undoBtn.disabled = !this.undo.length; this.redoBtn.disabled = !this.redo.length; }

  // ---------------------------------------------------------------- palette (drag a block out, or tap it to add)
  drawPalette() {
    this.palette.replaceChildren(h("div", { class: "ed-title" }, "Blocks"),
      ...Object.entries(this.spec.blocks).map(([type, def]) => h("button", { class: "ed-chip", draggable: "true", title: `Drag onto the page, or tap to add`,
        ondragstart: (e) => { e.dataTransfer.setData("text/x-new", type); e.dataTransfer.effectAllowed = "copy"; },
        onclick: () => this.addBlock(type) }, icon("plus"), def.label)),
      h("div", { class: "ed-title" }, "Side by side"),
      ...this.spec.layouts.map((l) => h("button", { class: "ed-chip", draggable: "true", title: "Drag onto the page, or tap to add",
        ondragstart: (e) => { e.dataTransfer.setData("text/x-layout", l.name); e.dataTransfer.effectAllowed = "copy"; },
        onclick: () => this.addBlock(null, l) }, icon("columns"), l.label)));
  }

  /** A fresh row group whose id cannot clash with the blocks about to be placed. */
  makeGroup(avoid = []) {
    const g = newBlock(this.design, "group", this.spec);
    const taken = new Set(avoid);
    while (taken.has(g.id)) g.id += "x";
    return g;
  }
  blockFor(type, layout) {
    return layout ? instantiate(this.design, layout.block) : newBlock(this.design, type, this.spec);
  }

  addBlock(type, layout = null) {
    this.edit(() => {
      const block = this.blockFor(type, layout);
      const sel = this.selected && locate(this.page, this.selected);
      const at = sel?.list[sel.index];
      if (at?.children && !at.children.length) at.children.push(block);                  // an empty box: put it inside
      else if (sel?.parent?.direction === "row") { const row = locate(this.page, sel.parent.id); insert(row.list, row.index + 1, block); }   // never squeeze more into a line by accident
      else if (sel) insert(sel.list, sel.index + 1, block);
      else this.page.blocks.push(block);
      this.selected = block.id;
    }, "", { props: true });
    this.scrollToSelected();
  }

  depthOf(id) { // how many boxes deep a block's own children would sit
    const walkTo = (list, d) => { for (const b of list) { if (b.id === id) return d + 1; if (b.children) { const r = walkTo(b.children, d + 1); if (r) return r; } } return 0; };
    return walkTo(this.page.blocks, 0);
  }

  scrollToSelected() { requestAnimationFrame(() => this.canvas.querySelector(".ed-sel")?.scrollIntoView({ block: "nearest" })); }

  // ---------------------------------------------------------------- page tabs
  drawTabs() {
    this.tabs.replaceChildren(...this.design.pages.map((p) => h("button", { class: "ed-tab", "aria-selected": String(p.id === this.page.id), onclick: () => { this.pageId = p.id; this.selected = null; this.drawTabs(); this.drawCanvas(); this.drawProps(); } }, p.title)),
      this.design.pages.length < this.spec.limits.pages ? h("button", { class: "ed-tab add", title: "Add a page", onclick: () => this.addPage() }, icon("plus")) : null);
  }
  addPage() {
    this.edit(() => { const id = freshId(this.design, "p"); this.design.pages.push({ id, title: `Page ${this.design.pages.length + 1}`, blocks: [] }); this.pageId = id; this.selected = null; }, "", { props: true });
  }

  // ---------------------------------------------------------------- canvas
  drawCanvas() {
    const frame = h("div", { class: `ed-frame ${this.device}` });
    applyTheme(frame, this.design.theme);
    const ctx = { editable: true, logo: "", go: () => {}, wrap: (b, n) => this.wrap(b, n) };
    const page = renderPage(this.page, ctx);
    frame.append(page);
    // dropping on empty space at the end of the page
    this.hookDrop(frame, () => ({ list: this.page.blocks, index: this.page.blocks.length }), true);
    frame.addEventListener("click", () => { if (this.selected) { this.selected = null; this.drawCanvas(); this.drawActions(); this.drawProps(); } });
    this.canvas.replaceChildren(frame);
  }

  wrap(b, inner) {
    const w = h("div", { class: "ed-block" + (b.id === this.selected ? " ed-sel" : ""), draggable: "true", "data-id": b.id }, inner);
    w.addEventListener("click", (e) => { e.stopPropagation(); this.select(b.id); });
    w.addEventListener("dragstart", (e) => { e.stopPropagation(); e.dataTransfer.setData("text/x-move", b.id); e.dataTransfer.effectAllowed = "move"; });
    this.hookDrop(w, (e) => this.target(b, w, e));
    return w;
  }

  select(id) { this.selected = id; this.drawCanvas(); this.drawActions(); this.drawProps(); }

  /** Where a drop on this block would land: before it, after it, or (for a box) inside it. */
  target(b, el, e) {
    const at = locate(this.page, b.id), r = el.getBoundingClientRect(), y = (e.clientY - r.top) / Math.max(1, r.height), x = (e.clientX - r.left) / Math.max(1, r.width);
    if ((x < 0.18 || x > 0.82) && r.width > 120) {                                     // the left or right edge: put the new block beside this one, in one line
      const side = x < 0.5 ? "left" : "right";
      if (at.parent?.direction === "row") return { list: at.list, index: side === "left" ? at.index : at.index + 1, edge: side === "left" ? "before" : "after", side };
      return { beside: b.id, side };
    }
    if (b.children && y > 0.25 && y < 0.75) return { list: b.children, index: b.children.length, inside: true };
    return y < 0.5 ? { list: at.list, index: at.index, edge: "before" } : { list: at.list, index: at.index + 1, edge: "after" };
  }

  hookDrop(el, where, isFrame = false) {
    const mark = (cls) => { for (const c of ["drop-before", "drop-after", "drop-in", "drop-left", "drop-right"]) el.classList.toggle(c, c === cls); };
    el.addEventListener("dragover", (e) => {
      const t = e.dataTransfer.types;
      if (!(t.includes("text/x-new") || t.includes("text/x-move"))) return;
      e.preventDefault(); e.stopPropagation();
      const w = where(e);
      this.clearMarks(); mark(w.beside ? "drop-" + w.side : w.inside ? "drop-in" : w.edge ? "drop-" + w.edge : isFrame ? "drop-after" : "");
    });
    el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) mark(""); });
    el.addEventListener("drop", (e) => {
      e.preventDefault(); e.stopPropagation(); this.clearMarks();
      const w = where(e), type = e.dataTransfer.getData("text/x-new"), layoutName = e.dataTransfer.getData("text/x-layout"), id = e.dataTransfer.getData("text/x-move");
      const layout = layoutName && this.spec.layouts.find((l) => l.name === layoutName);
      if (!(type && this.spec.blocks[type]) && !layout && !id) return;
      this.edit(() => {
        if (id) {
          if (w.beside) { if (!placeBeside(this.page, id, w.beside, w.side, this.makeGroup())) return; }
          else move(this.page, id, w.list, w.index);
          this.selected = id; return;
        }
        const block = this.blockFor(type, layout);
        if (w.beside) {
          const all = [...walk([block])].map((x) => x.id);
          placeBeside(this.page, block, w.beside, w.side, this.makeGroup(all));
        } else insert(w.list, w.index, block);
        this.selected = block.id;
      }, "", { props: true });
    });
  }
  clearMarks() { this.canvas.querySelectorAll(".drop-before,.drop-after,.drop-in,.drop-left,.drop-right").forEach((x) => x.classList.remove("drop-before", "drop-after", "drop-in", "drop-left", "drop-right")); }

  // ---------------------------------------------------------------- action bar for the selected block (the touch-friendly way to arrange)
  drawActions() {
    const id = this.selected, at = id && locate(this.page, id);
    if (!at) { this.actions.replaceChildren(h("span", { class: "muted" }, "Tap a block to select it. Drag blocks from the left, or tap one to add it.")); return; }
    const b = at.list[at.index], btn = (ic, title, fn, dis) => h("button", { class: "btn small", title, disabled: dis, onclick: fn }, icon(ic));
    this.actions.replaceChildren(h("strong", null, this.spec.blocks[b.type].label),
      btn("up", "Move up", () => this.edit(() => move(this.page, id, at.list, at.index - 1), "", { props: true }), at.index === 0),
      btn("down", "Move down", () => this.edit(() => move(this.page, id, at.list, at.index + 2), "", { props: true }), at.index === at.list.length - 1),
      btn("columns", "Put beside the one above, in one line", () => this.edit(() => {
        const prev = at.list[at.index - 1];
        if (prev.type === "group" && prev.direction === "row") prev.children.push(remove(this.page, id));
        else placeBeside(this.page, id, prev.id, "right", this.makeGroup());
        this.selected = id;
      }, "", { props: true }), at.index === 0),
      at.parent?.direction === "row" ? btn("outdent", "Take out of this line", () => this.edit(() => { outOfRow(this.page, id); }, "", { props: true })) : null,
      btn("copy", "Duplicate", () => this.edit(() => { this.selected = duplicate(this.design, this.page, id).id; }, "", { props: true })),
      btn("trash", "Delete", () => this.edit(() => { remove(this.page, id); this.selected = null; }, "", { props: true })));
  }

  // ---------------------------------------------------------------- properties
  drawProps() {
    const b = this.selected && find(this.page, this.selected);
    if (!b) return this.props.replaceChildren(...this.designSettings());
    const def = this.spec.blocks[b.type];
    this.props.replaceChildren(h("div", { class: "ed-title" }, def.label), ...def.fields.map((f) => this.field(f, b[f.key], (v) => this.edit(() => { b[f.key] = v; }, `${b.id}.${f.key}`))));
  }

  /** One control for one field; `set` is called with the new value. Controls update the canvas but are not themselves redrawn. */
  field(f, value, set) {
    const row = (control, extra) => h("div", { class: "field" }, h("label", null, f.label), control, extra);
    switch (f.kind) {
      case "text": return row(h("input", { type: "text", value, maxlength: String(f.max_length || 200), oninput: (e) => set(e.target.value) }));
      case "longtext": return row(h("textarea", { rows: "4", maxlength: String(f.max_length || 2000), oninput: (e) => set(e.target.value) }, value));
      case "int": return row(h("input", { type: "number", value: String(value), oninput: (e) => { const n = parseInt(e.target.value, 10); if (!Number.isNaN(n)) set(n); } }));
      case "bool": return h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!value, onchange: (e) => set(e.target.checked) }), f.label);
      case "choice": return row(h("div", { class: "seg" }, ...f.options.map((o) => h("button", { class: "btn small", "aria-pressed": String(o === value), onclick: (e) => { e.currentTarget.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); set(o); } }, label(o)))));
      case "color": {
        const t = this.design.theme;
        const input = h("input", { type: "color", value: value || "#888888", "aria-label": "Pick a colour", oninput: (e) => set(e.target.value) });
        const swatch = (c, title) => h("button", { class: "swatch", title, "aria-label": title, style: { background: c }, onclick: () => { set(c); input.value = c; } });
        return row(h("div", { class: "form-row tight" }, input, swatch(t.accent, "Main colour"), swatch(t.text, "Text colour"), swatch(t.surface, "Cards colour"), swatch(t.bg, "Background"),
          h("button", { class: "btn small", onclick: () => { set(""); input.value = "#888888"; } }, "Default")));
      }
      case "icon": {
        const grid = h("div", { class: "icon-grid" }, ...(f.options || this.spec.icons).map((n) => h("button", { class: "icon-pick", title: n, "aria-label": n, "aria-pressed": String(n === value),
          onclick: (e) => { grid.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); set(n); } }, iconSvg(n, 20))));
        const none = h("button", { class: "btn small", onclick: () => { grid.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false")); set(""); } }, "No icon");
        return row(grid, f.default === "" ? none : null);
      }
      case "slug": {
        const names = this.sections;
        if (!names.some((x) => x.name === value)) names.push({ name: value, label: value });
        return row(h("select", { onchange: (e) => set(e.target.value) }, ...names.map((x) => h("option", { value: x.name, selected: x.name === value }, x.label))),
          h("span", { class: "help" }, "The content you manage under Content in Manage app. Add items there, then press Update."));
      }
      case "image": {
        const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", hidden: "" });
        const up = h("button", { class: "btn small", type: "button", onclick: () => file.click() }, icon("upload"), "Upload picture");
        file.onchange = async () => {
          if (!file.files[0]) return;
          up.disabled = true; up.textContent = "Uploading…";
          try { set((await postFile("/api/designs/image", file.files[0])).url); this.drawProps(); }
          catch (e) { toast(e.message, { kind: "error" }); up.disabled = false; up.replaceChildren(icon("upload"), "Upload picture"); }
        };
        const shown = value.startsWith("/api/") ? "" : value;
        return row(h("div", null, h("div", { class: "form-row tight" }, up, h("button", { class: "btn small", type: "button", onclick: () => { set("@logo"); this.drawProps(); } }, "Use my app's logo")),
          h("input", { type: "url", value: shown, placeholder: "or paste an https:// address", "aria-label": "Picture address", oninput: (e) => set(e.target.value.trim()) }),
          value ? h("p", { class: "muted", style: { fontSize: "13px", margin: "6px 0 0" } }, value === "@logo" ? "Showing your app's logo." : value.startsWith("/api/") ? "Uploaded picture in use." : "") : null, file));
      }
      case "action": return this.actionField(f, value, set);
      default: return h("div");
    }
  }

  actionField(f, value, set) {
    const pages = this.design.pages;
    const kind = h("select", { onchange: (e) => { const t = e.target.value; set(t === "page" ? { type: "page", target: pages[0].id } : t === "link" ? { type: "link", url: "" } : { type: "none" }); this.drawProps(); } },
      ...[["none", "Nothing"], ["page", "Go to a page"], ["link", "Open a link"]].map(([v, l]) => h("option", { value: v, selected: v === value.type }, l)));
    const more = value.type === "page"
      ? h("select", { onchange: (e) => set({ type: "page", target: e.target.value }) }, ...pages.map((p) => h("option", { value: p.id, selected: p.id === value.target }, p.title)))
      : value.type === "link" ? h("input", { type: "url", value: value.url || "", placeholder: "https://, tel: or mailto:", oninput: (e) => set({ type: "link", url: e.target.value.trim() }) }) : null;
    return h("div", { class: "field" }, h("label", null, f.label), kind, more);
  }

  designSettings() {
    const t = this.design.theme, page = this.page;
    return [
      h("div", { class: "ed-title" }, "Page"),
      h("div", { class: "field" }, h("label", null, "Page name"), h("input", { type: "text", value: page.title, maxlength: "30", oninput: (e) => this.edit(() => { page.title = e.target.value || "Page"; }, `page.${page.id}`) })),
      this.design.pages.length > 1 ? h("button", { class: "btn small danger", onclick: async () => {
        if (!(await confirmDialog("Delete this page?", `"${page.title}" and everything on it will be removed.`, "Delete", true))) return;
        this.edit(() => { const i = this.design.pages.indexOf(page); this.design.pages.splice(i, 1); dropActionsTo(this.design, page.id); this.pageId = this.design.pages[Math.max(0, i - 1)].id; this.selected = null; }, "", { props: true });
      } }, icon("trash"), "Delete page") : null,
      h("div", { class: "ed-title" }, "Colour themes"),
      h("div", { class: "preset-grid" }, ...this.spec.presets.map((p) => h("button", { class: "preset", title: p.name, "aria-label": `${p.name} theme`,
        style: { background: p.theme.bg, color: p.theme.text, borderColor: p.theme.accent },
        onclick: () => this.edit(() => { Object.assign(this.design.theme, p.theme); }, "", { props: true }) },
        h("span", { class: "dot", style: { background: p.theme.accent } }), p.name))),
      h("div", { class: "ed-title" }, "Your own colours"),
      ...this.spec.theme.map((f) => this.field(f, t[f.key], (v) => this.edit(() => { t[f.key] = v; }, `theme.${f.key}`))),
      h("div", { class: "field" }, h("label", null, "Page tabs"), h("div", { class: "seg" }, ...[["tabs", "Show at bottom"], ["none", "Hide"]].map(([v, l]) =>
        h("button", { class: "btn small", "aria-pressed": String((this.design.nav || "tabs") === v), onclick: (e) => { e.currentTarget.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); this.edit(() => { this.design.nav = v; }); } }, l)))),
    ];
  }

  // ---------------------------------------------------------------- use it for an app
  async useDialog() {
    if (!(await this.save())) { toast("Fix the save problem first.", { kind: "error" }); return; }
    const linked = new Set(this.linked);
    const list = h("div", { class: "ed-apps" });
    const paint = () => list.replaceChildren(...(this.apps.length ? this.apps.map((a) => {
      const on = linked.has(a.app_id);
      return h("div", { class: "row" }, h("span", null, a.name), h("button", { class: "btn small" + (on ? "" : " primary"), onclick: async (e) => {
        e.currentTarget.disabled = true;
        try {
          await api(on ? "/api/designs/unapply" : `/api/designs/${this.id}/apply`, { method: "POST", body: { app_id: a.app_id } });
          if (on) linked.delete(a.app_id); else linked.add(a.app_id);
          this.linked = [...linked]; toast(on ? "Removed from the app." : "The app now shows this design.");
        } catch (err) { toast(err.message, { kind: "error" }); }
        paint();
      } }, on ? "Stop using" : "Use")); }) : [h("p", { class: "muted" }, "You have no apps yet. Build one first, then come back.")]));
    paint();
    const dlg = h("dialog", null, h("h3", null, "Use this design for an app"), h("p", { class: "muted" }, "The app opens these screens, and later edits show up in it straight away."), list,
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => dlg.close() }, "Done")));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg); dlg.showModal();
  }
}
