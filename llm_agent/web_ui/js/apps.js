// Build app (publish a zip under a shareable link) and Manage app (update it later with the app id + password).
import { api, apiWith, postFile } from "./api.js";
import { h, icon, toast, confirmDialog, copyText, fmtBytes, timeAgo, plural } from "./dom.js";
import { kitGuide, kitForm, kitPanel, kitCredentialsDialog } from "./kits.js";

const page = (title, lede, ...body) => h("div", { class: "page" }, h("h2", null, title), h("p", { class: "lede" }, lede), ...body);
const absolute = (url) => new URL(url, location.origin).href;

function copyButton(text, label = "Copy") {
  return h("button", { class: "btn small", type: "button", onclick: async () => toast((await copyText(text)) ? "Copied" : "Could not copy") }, icon("copy"), label);
}

function fieldRow(label, value, { mono = true, extra = null } = {}) {
  return h("div", { class: "secret-row" }, h("span", { class: "faint" }, label), h("code", { style: mono ? "" : "font-family:inherit" }, value), copyButton(value), extra);
}

function zipDrop(onFile, text = "Drop your .zip here, or") {
  const input = h("input", { type: "file", accept: ".zip,application/zip", hidden: true });
  const drop = h("div", { class: "drop" }, icon("upload"), h("p", null, text, " ", h("button", { class: "btn small", type: "button", onclick: () => input.click() }, "choose a file")));
  const label = h("p", { class: "faint", style: { fontSize: "13px", marginTop: "4px" } });
  drop.append(label);
  const pick = (f) => { if (!f) return; label.textContent = `${f.name} (${fmtBytes(f.size)})`; onFile(f); };
  input.addEventListener("change", () => pick(input.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); });
  return h("div", null, drop, input);
}

function secretDialog(title, intro, app) {
  const url = absolute(app.url);
  const text = `${app.name || "App"}\nApp ID: ${app.app_id}\nManagement password: ${app.password}\nShare link: ${url}\n`;
  const dlg = h("dialog", null, h("h3", null, title), h("p", { class: "muted", style: { marginBottom: "12px" } }, intro),
    fieldRow("App ID", app.app_id), fieldRow("Password", app.password), fieldRow("Share link", url, { mono: false }),
    h("div", { class: "form-error", style: { margin: "12px 0 0", background: "var(--warn-soft)", color: "var(--warn)" } },
      "The password is shown only this once. Save it somewhere safe; you need it with the app id to update this app."),
    h("div", { class: "actions" },
      h("button", { class: "btn", onclick: () => {
        const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: `${app.app_id}-credentials.txt` });
        document.body.append(a); a.click(); a.remove();
      } }, icon("download"), "Download as text"),
      h("button", { class: "btn primary", onclick: () => dlg.close() }, "I have saved it")));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg); dlg.showModal();
}

// ================================================================================ Build app
export class BuildView {
  constructor({ onManage }) { this.onManage = onManage; this.el = h("div"); this.file = null; this.mode = "web"; }

  async load() {
    let data;
    try { data = await api("/api/apps"); } catch (e) { this.el.replaceChildren(page("Build app", "", h("div", { class: "form-error" }, e.message))); return; }
    const name = h("input", { type: "text", maxlength: "60", placeholder: "For example: Anna's Candle Shop", "aria-label": "App name" });
    const drop = zipDrop((f) => { this.file = f; if (!name.value.trim()) name.value = f.name.replace(/\.zip$/i, ""); });
    const go = h("button", { class: "btn primary", type: "button", onclick: async () => {
      if (!this.file) { toast("Choose your .zip first.", { kind: "bad" }); return; }
      go.disabled = true; go.replaceChildren(h("span", { class: "spinner" }), "Building…");
      try {
        const r = await postFile(`/api/apps/build?name=${encodeURIComponent(name.value.trim())}`, this.file);
        this.file = null;
        secretDialog("Your app is live", "Share the link with your customers. Keep the id and password for yourself.", r);
        this.load();
      } catch (e) { toast(e.message, { kind: "bad", timeout: 9000 }); go.disabled = false; go.replaceChildren(icon("package"), "Build app"); }
    } }, icon("package"), "Build app");

    const rows = data.apps.map((a) => h("div", { class: "list-row", style: { alignItems: "flex-start" } }, icon("package"),
      h("div", { class: "grow" }, h("b", null, a.name), h("small", null, `${a.kind === "kit" ? "Android / Windows app" : "Web link"} · App ID ${a.app_id}, version ${a.kind === "kit" ? a.kit?.kit_version : a.version}, updated ${timeAgo(a.updated_at)}`),
        h("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap", marginTop: "8px" } },
          ...(a.url ? [h("a", { class: "btn small", href: a.url, target: "_blank", rel: "noopener" }, icon("external"), "Open"), copyButton(absolute(a.url), "Copy link")] : []),
          h("button", { class: "btn small", onclick: () => this.onManage(a.app_id) }, icon("wrench"), "Manage"),
          h("button", { class: "btn small", onclick: async () => {
            if (!(await confirmDialog("Make a new password?", "The old password stops working immediately.", "New password"))) return;
            try { const r = await api(`/api/apps/${a.app_id}/reset-password`, { method: "POST" }); secretDialog("New password", "Use this from now on.", { ...a, password: r.password }); }
            catch (e) { toast(e.message, { kind: "bad" }); }
          } }, "New password")))));

    const toggle = h("div", { class: "seg", role: "tablist" },
      ...[["web", "Web link"], ["kit", "Android / Windows app"]].map(([m, label]) =>
        h("button", { class: "btn small" + (this.mode === m ? " primary" : ""), role: "tab", "aria-selected": String(this.mode === m), onclick: () => { this.mode = m; this.load(); } }, label)));
    const list = [h("h3", { style: { marginBottom: "10px" } }, `Your apps (${data.apps.length} of ${data.limit})`),
      h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, "No apps yet"), "Apps you build appear here.")]))];
    if (this.mode === "kit") {
      this.el.replaceChildren(page("Build app", "Create your own installable Android app and Windows program for a web app (made with the assistant, or one you already host). It opens your app's address and is managed from Manage app.",
        toggle, kitGuide(), kitForm({ maxMb: data.max_mb, onDone: (r) => kitCredentialsDialog(r, { onClose: () => this.load() }) }), ...list));
      return;
    }
    this.el.replaceChildren(page("Build app", "Turn a web app you made into a shareable app with its own link that works on phones, tablets and computers. Customers can add it to their home screen.",
      toggle,
      h("ol", { class: "steps-list" },
        h("li", null, "Ask the assistant to build your app, then in ", h("b", null, "Files"), " download the folder as a .zip. Its main page must be ", h("code", null, "index.html"), "."),
        h("li", null, "Name it, add the .zip below and press ", h("b", null, "Build app"), "."),
        h("li", null, "Send the link to your customers. Keep the app id and password: you need them in ", h("b", null, "Manage app"), " to add products or change anything later.")),
      h("div", { class: "fields", style: { marginBottom: "26px" } }, h("div", { class: "field" }, h("label", null, "App name"), name), drop,
        h("p", { class: "faint", style: { fontSize: "13px" } }, `Up to ${data.max_mb} MB. Front-end apps only (HTML, CSS, JavaScript, images). Server code such as Flask or Node can't run here.`), go),
      ...list));
  }
}

// ================================================================================ Manage app
export class ManageView {
  constructor({ onBuild }) { this.onBuild = onBuild; this.el = h("div"); this.creds = null; this.app = null; this.file = null; }

  prefill(appId) { this.pre = appId; this.creds = null; this.app = null; }

  load() { this.creds && this.app ? this.panel() : this.form(); }

  form() {
    const id = h("input", { type: "text", id: "mg-id", autocomplete: "off", spellcheck: "false", value: this.pre || "", placeholder: "10 letters and numbers", "aria-label": "App ID", style: "font-family:var(--mono)" });
    const pw = h("input", { type: "password", id: "mg-pw", autocomplete: "off", placeholder: "xxxx-xxxx-xxxx-xxxx", "aria-label": "Management password", style: "font-family:var(--mono)" });
    const err = h("div", { class: "form-error", hidden: true });
    const btn = h("button", { class: "btn primary", type: "submit" }, icon("wrench"), "Open app");
    const submit = async (e) => {
      e.preventDefault(); err.hidden = true; btn.disabled = true;
      const creds = { "X-App-Id": id.value.trim(), "X-App-Password": pw.value.trim() };
      try { const r = await apiWith("/api/apps/manage/open", creds); this.creds = creds; this.app = r.app; this.panel(); }
      catch (x) { err.hidden = false; err.textContent = x.message; btn.disabled = false; }
    };
    this.el.replaceChildren(page("Manage app", "Update an app you built, for example to add a new product. Enter the app id and the password you received when you built it.",
      h("form", { class: "fields", onsubmit: submit }, err,
        h("div", { class: "field" }, h("label", { for: "mg-id" }, "App ID"), id),
        h("div", { class: "field" }, h("label", { for: "mg-pw" }, "Management password"), pw), btn),
      h("p", { class: "faint", style: { marginTop: "18px" } }, "Lost the password? Open ", h("a", { href: "#/build", onclick: (e) => { e.preventDefault(); this.onBuild(); } }, "Build app"), " and press “New password” next to your app.")));
    (this.pre ? pw : id).focus();
  }

  panel() {
    const a = this.app, url = a.url ? absolute(a.url) : "";
    const busy = (btn, label, fn) => async () => {
      btn.disabled = true; const old = [...btn.childNodes]; btn.replaceChildren(h("span", { class: "spinner" }), label);
      try { await fn(); } catch (e) { toast(e.message, { kind: "bad", timeout: 9000 }); }
      btn.disabled = false; btn.replaceChildren(...old);
    };
    const drop = zipDrop((f) => { this.file = f; }, "Drop the new version (.zip) here, or");
    const upBtn = h("button", { class: "btn primary", type: "button" }, icon("upload"), "Publish new version");
    upBtn.onclick = busy(upBtn, "Publishing…", async () => {
      if (!this.file) { toast("Choose the new .zip first.", { kind: "bad" }); return; }
      const r = await postFile("/api/apps/manage/update", this.file, this.creds);
      this.file = null; this.app = r.app; toast(`Updated to version ${r.app.version}. Customers see it now.`); this.panel();
    });
    const rbBtn = h("button", { class: "btn", type: "button", disabled: a.has_previous ? null : "" }, "Go back to the previous version");
    rbBtn.onclick = busy(rbBtn, "Restoring…", async () => {
      if (!(await confirmDialog("Go back to the previous version?", "Customers will see the earlier version right away. You can switch again.", "Go back"))) return;
      this.app = (await apiWith("/api/apps/manage/rollback", this.creds)).app; toast("Previous version restored"); this.panel();
    });
    const delBtn = h("button", { class: "btn danger", type: "button" }, icon("trash"), "Delete this app");
    delBtn.onclick = busy(delBtn, "Deleting…", async () => {
      if (!(await confirmDialog(`Delete “${a.name}”?`, "The link stops working for everyone and this cannot be undone.", "Delete app", true))) return;
      await apiWith("/api/apps/manage/delete", this.creds); toast("App deleted"); this.creds = this.app = null; this.pre = ""; this.form();
    });
    const close = h("button", { class: "btn quiet", onclick: () => { this.creds = this.app = null; this.pre = ""; this.form(); } }, "Close");
    if (a.kind === "kit") {
      this.el.replaceChildren(page(a.name, `App ID ${a.app_id} · phone / computer app`,
        kitPanel({ app: a, creds: this.creds, busy, refresh: (app) => { this.app = app; this.panel(); } }),
        h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", margin: "26px 0 0" } }, delBtn, close)));
      return;
    }
    this.el.replaceChildren(page(a.name, `App ID ${a.app_id}`,
      h("div", { class: "stat-grid" }, h("div", { class: "stat" }, h("b", null, `v${a.version}`), h("span", null, "current version")),
        h("div", { class: "stat" }, h("b", null, plural(a.files, "file")), h("span", null, fmtBytes(a.size_bytes))),
        h("div", { class: "stat" }, h("b", null, timeAgo(a.updated_at)), h("span", null, "last updated"))),
      fieldRow("Share link", url, { mono: false, extra: h("a", { class: "btn small", href: a.url, target: "_blank", rel: "noopener" }, icon("external"), "Open") }),
      h("h3", { style: { margin: "26px 0 6px" } }, "Update the app"),
      h("p", { class: "muted", style: { marginBottom: "12px", maxWidth: "62ch" } }, "Change your app (ask the assistant to add the product, download the folder as a .zip), then upload the whole new .zip. It replaces the live version; your customers don't need to do anything."),
      h("div", { class: "fields" }, drop, upBtn),
      h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", margin: "26px 0 0" } }, rbBtn, delBtn,
        h("button", { class: "btn quiet", onclick: () => { this.creds = this.app = null; this.pre = ""; this.form(); } }, "Close"))));
  }
}
