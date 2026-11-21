// Manage app > Content: one tab per section the app defines (products, bookings, posts...), a form built from the
// section's fields, and a layout editor so the owner can add, rename and remove sections and fields freely.
import { apiWith, postFile } from "./api.js";
import { h, icon, toast, confirmDialog } from "./dom.js";

const TYPES = [["text", "Short text"], ["longtext", "Long text"], ["number", "Number"], ["money", "Price / amount"], ["date", "Date"],
  ["yesno", "Yes / no"], ["choice", "Choice list"], ["image", "Picture"], ["link", "Web link"]];

export function contentPanel({ creds, onChange }) {
  const box = h("div");
  let collection = "";
  const bad = (e) => toast(e.message, { kind: "bad", timeout: 8000 });

  const load = async () => {
    let data;
    try { data = await apiWith(`/api/apps/manage/items/list${collection ? `?collection=${encodeURIComponent(collection)}` : ""}`, creds); }
    catch (e) { box.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    const sections = data.schema.sections;
    if (!collection || !sections.some((s) => s.name === collection)) { collection = sections[0].name; return load(); }
    const sec = data.section;
    const tabs = h("div", { class: "tabs", role: "tablist", style: { display: "flex", gap: "6px", flexWrap: "wrap", margin: "8px 0" } },
      ...sections.map((s) => h("button", { class: `btn small${s.name === collection ? " primary" : ""}`, type: "button", role: "tab", "aria-selected": String(s.name === collection),
        onclick: () => { collection = s.name; load(); } }, s.label)));
    const rows = data.items.map((it) => h("div", { class: "list-row" }, it.image_url ? h("img", { class: "thumb", src: it.image_url, alt: "" }) : icon("package"),
      h("div", { class: "grow" }, h("b", null, it.heading), it.line ? h("small", null, it.line) : null),
      h("button", { class: "btn small", type: "button", onclick: () => edit(sec, it) }, "Edit"),
      h("button", { class: "btn small danger", type: "button", "aria-label": `Delete ${it.heading}`, onclick: async () => {
        if (!(await confirmDialog(`Delete “${it.heading}”?`, "It disappears from the app after you press Update.", "Delete", true))) return;
        try { await apiWith(`/api/apps/manage/items/delete?item_id=${it.id}`, creds); load(); onChange?.(); } catch (e) { bad(e); }
      } }, icon("trash"))));
    box.replaceChildren(...[
      h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", margin: "26px 0 0", flexWrap: "wrap" } },
        h("h3", null, "Your content"),
        h("div", { style: { display: "flex", gap: "6px" } }, h("button", { class: "btn small", type: "button", onclick: () => customise(data.schema) }, "Customise sections"),
          h("button", { class: "btn small primary", type: "button", onclick: () => edit(sec, null) }, `Add ${sec.item || "item"}`))),
      tabs,
      h("p", { class: "muted", style: { marginBottom: "10px" } }, `${sec.label}: ${data.items.length}. Changes are drafts; customers see them after you press Update (above).`),
      h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, `No ${sec.label.toLowerCase()} yet`), `Press “Add ${sec.item || "item"}”.`)])),
    ].filter(Boolean));
  };

  // ---- a form built from the section's fields ----
  const edit = (sec, it) => {
    const vals = it?.values || {};
    const getters = [];
    const fields = sec.fields.map((f) => {
      const v = vals[f.key];
      let control, get;
      if (f.type === "longtext") { control = h("textarea", { rows: "3", maxlength: "2000", "aria-label": f.label }, v ?? ""); get = () => control.value; }
      else if (f.type === "yesno") { control = h("input", { type: "checkbox" }); control.checked = v === undefined ? !!f.default : !!v; get = () => control.checked; }
      else if (f.type === "choice") {
        control = h("select", { "aria-label": f.label }, h("option", { value: "" }, "Choose…"), ...f.options.map((o) => h("option", { value: o }, o)));
        control.value = v ?? ""; get = () => control.value;
      } else if (f.type === "image") {
        let image = v || "";
        const url = h("input", { type: "text", placeholder: "or paste an https:// picture address", value: image.startsWith("http") ? image : "", "aria-label": `${f.label} address` });
        const note = h("p", { class: "faint", style: { fontSize: "13px" } }, image ? "Picture set." : "Any photo (PNG, JPG, WEBP or GIF, up to 10 MB). We resize it to fit.");
        const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", hidden: "" });
        file.addEventListener("change", async () => {
          if (!file.files[0]) return;
          try { image = (await postFile("/api/apps/manage/image", file.files[0], creds)).image; url.value = ""; note.textContent = `${file.files[0].name} uploaded.`; } catch (e) { bad(e); }
        });
        control = h("div", null, h("button", { class: "btn small", type: "button", onclick: () => file.click() }, icon("upload"), "Upload picture"), url, note, file);
        get = () => url.value.trim() || image;
      } else {
        const type = { number: "number", money: "number", date: "date", link: "url" }[f.type] || "text";
        control = h("input", { type, maxlength: "200", ...(f.type === "money" ? { step: "0.01", min: "0" } : f.type === "number" ? { step: "any" } : {}), value: v ?? "", "aria-label": f.label });
        get = () => control.value;
      }
      getters.push([f.key, get]);
      const label = h("label", null, f.label, f.required ? " *" : "");
      return f.type === "yesno" ? h("label", { style: { display: "flex", gap: "8px", alignItems: "center" } }, control, f.label) : h("div", { class: "field" }, label, control);
    });
    const dlg = h("dialog", null, h("h3", null, it ? `Edit ${sec.item || "item"}` : `Add ${sec.item || "item"}`), h("div", { class: "fields" }, ...fields),
      h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Cancel"),
        h("button", { class: "btn primary", type: "button", onclick: async () => {
          try {
            await apiWith("/api/apps/manage/items/save", creds, { id: it?.id, collection, values: Object.fromEntries(getters.map(([k, g]) => [k, g()])) });
            dlg.close(); load(); onChange?.();
          } catch (e) { bad(e); }
        } }, "Save")));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg); dlg.showModal();
  };

  // ---- the layout editor: sections and fields, freely ----
  const customise = (schema) => {
    const draft = JSON.parse(JSON.stringify(schema));
    const body = h("div");
    const paint = () => {
      body.replaceChildren(...draft.sections.map((s, si) => {
        const label = h("input", { type: "text", maxlength: "40", value: s.label, "aria-label": "Section name" });
        label.oninput = () => { s.label = label.value; };
        const item = h("input", { type: "text", maxlength: "30", value: s.item || "", placeholder: "what one entry is called, e.g. booking", "aria-label": "One entry is called" });
        item.oninput = () => { s.item = item.value; };
        const fieldRows = s.fields.map((f, fi) => {
          const fl = h("input", { type: "text", maxlength: "40", value: f.label, "aria-label": "Field name" }); fl.oninput = () => { f.label = fl.value; };
          const ty = h("select", { "aria-label": "Field type" }, ...TYPES.map(([v, t]) => h("option", { value: v }, t))); ty.value = f.type;
          ty.onchange = () => { f.type = ty.value; if (f.type === "choice" && !f.options) f.options = ["Option 1", "Option 2"]; paint(); };
          const req = h("input", { type: "checkbox", "aria-label": "Required" }); req.checked = !!f.required; req.onchange = () => { f.required = req.checked; };
          const opts = f.type === "choice" ? h("input", { type: "text", value: (f.options || []).join(", "), placeholder: "options, separated by commas", "aria-label": "Options" }) : null;
          if (opts) opts.oninput = () => { f.options = opts.value.split(",").map((x) => x.trim()).filter(Boolean); };
          const move = (d) => { const j = fi + d; if (j < 0 || j >= s.fields.length) return; [s.fields[fi], s.fields[j]] = [s.fields[j], s.fields[fi]]; paint(); };
          return h("div", { class: "list-row", style: { flexWrap: "wrap", gap: "6px" } }, fl, ty, h("label", { style: { display: "flex", gap: "4px", alignItems: "center" } }, req, "Required"), opts,
            h("button", { class: "btn small", type: "button", "aria-label": "Move up", onclick: () => move(-1) }, "↑"), h("button", { class: "btn small", type: "button", "aria-label": "Move down", onclick: () => move(1) }, "↓"),
            h("button", { class: "btn small danger", type: "button", "aria-label": `Remove ${f.label}`, onclick: () => { s.fields.splice(fi, 1); paint(); } }, icon("trash")));
        });
        return h("div", { style: { border: "1px solid var(--border, #ccc)", borderRadius: "10px", padding: "10px", margin: "10px 0" } },
          h("div", { class: "fields" }, h("div", { class: "field" }, h("label", null, "Section"), label), h("div", { class: "field" }, h("label", null, "One entry is called"), item)),
          h("div", { class: "list" }, ...fieldRows),
          h("div", { style: { display: "flex", gap: "6px", marginTop: "8px" } },
            h("button", { class: "btn small", type: "button", onclick: () => { s.fields.push({ label: "New field", type: "text" }); paint(); } }, icon("plus"), "Add field"),
            draft.sections.length > 1 ? h("button", { class: "btn small danger", type: "button", onclick: async () => {
              if (!(await confirmDialog(`Remove the section “${s.label}”?`, "Entries already saved stay stored but are no longer shown.", "Remove", true))) return;
              draft.sections.splice(si, 1); paint();
            } }, "Remove section") : null));
      }), h("button", { class: "btn small", type: "button", onclick: () => { draft.sections.push({ label: "New section", item: "item", fields: [{ label: "Title", type: "text", required: true }] }); paint(); } }, icon("plus"), "Add section"));
    };
    paint();
    const dlg = h("dialog", { style: { maxWidth: "720px", width: "calc(100vw - 32px)" } }, h("h3", null, "Customise your content"),
      h("p", { class: "muted" }, "Choose what you manage and which details each entry has. Entries you already saved keep their details."), body,
      h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Cancel"),
        h("button", { class: "btn primary", type: "button", onclick: async () => {
          try { await apiWith("/api/apps/manage/schema", creds, draft); dlg.close(); toast("Saved. Press Update to show it in the app."); load(); onChange?.(); } catch (e) { bad(e); }
        } }, "Save")));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg); dlg.showModal();
  };

  load();
  return box;
}
