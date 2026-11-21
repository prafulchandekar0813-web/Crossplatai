// Manage app > Screens: choose the design the installed app shows (a template or one of your own) and jump into the Designer to edit it.
import { apiWith } from "../api.js";
import { h, icon, toast, confirmDialog, plural } from "../dom.js";
import { preview } from "./designer.js";

export function screensPanel({ creds, onEdit }) {
  const box = h("div");
  const say = (e) => toast(e.message || String(e), { kind: "bad", timeout: 9000 });

  async function load() {
    let st;
    try { st = await apiWith("/api/apps/manage/screens/state", creds, {}); }
    catch (e) { box.replaceChildren(h("h3", { style: { margin: "26px 0 6px" } }, "Screens"), h("p", { class: "muted" }, e.message)); return; }
    paint(st);
  }

  async function use(body, done) {
    try { const st = await apiWith("/api/apps/manage/screens/use", creds, body); toast(done); paint(st); return st; }
    catch (e) { say(e); }
  }

  function paint(st) {
    const cur = st.current;
    const head = cur
      ? h("div", { class: "update-card" },
          h("div", { class: "grow" }, h("b", null, `Showing: ${cur.name}`),
            h("p", { class: "muted" }, "The installed app opens these screens. Edits you make in the Designer show up in the app straight away. Items from the Content section above appear in the design's “Items” blocks after you press Update.")),
          h("span", { class: "row-actions" },
            h("button", { class: "btn primary", onclick: () => onEdit?.(cur.id) }, icon("layout"), "Edit in Designer"),
            h("a", { class: "btn", href: st.view_url, target: "_blank", rel: "noopener" }, icon("external"), "Open"),
            h("button", { class: "btn", onclick: async () => { if (await confirmDialog("Stop using this design?", "The app goes back to its own address. Your design is kept.", "Stop using")) use({}, "The app no longer uses a design."); } }, "Stop using")))
      : h("p", { class: "muted", style: { maxWidth: "62ch" } }, "No design is used. The app opens its own address. Pick a template below, or one of your own designs, and the app shows those screens instead (this replaces the address while it is in use).");

    const mine = st.designs.length ? h("div", { class: "field" }, h("label", null, "Use one of your designs"),
      h("div", { class: "form-row" }, (() => {
        const sel = h("select", { "aria-label": "Your designs" }, ...st.designs.map((d) => h("option", { value: d.id, selected: cur?.id === d.id }, `${d.name} (${plural(d.pages, "page")})`)));
        const go = h("button", { class: "btn primary", type: "button", onclick: () => use({ design_id: sel.value }, "The app now shows that design.") }, "Use");
        return h("span", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } }, sel, go);
      })())) : null;

    const tpls = h("div", { class: "tpl-grid" }, ...st.templates.map((t) => h("div", { class: "tpl" }, preview(t.design), h("strong", null, t.name), h("p", { class: "muted" }, t.description),
      h("button", { class: "btn small primary", type: "button", onclick: () => use({ template: t.key }, `“${t.name}” is now your app's screens. Tap Edit in Designer to change it.`) }, "Use this template"))));

    box.replaceChildren(h("h3", { style: { margin: "26px 0 6px" } }, "Screens (templates and Designer)"), head, mine,
      h("h4", { style: { margin: "18px 0 8px" } }, "Start from a template"), tpls);
  }

  load();
  return box;
}
