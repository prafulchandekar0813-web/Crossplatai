// Android / Windows apps: the create form with its guidance, the credentials dialog, and the Manage-app pieces
// (usage, inbox, products, look and message, building the app, new version). Used by apps.js.
import { apiWith, downloadWith, postFile } from "./api.js";
import { h, icon, toast, confirmDialog, fmtBytes, timeAgo } from "./dom.js";

/** Set by the live preview when the user is happy with an app: Build app then packs that folder from Files. */
export const handoff = { folder: null };

export const fileToB64 = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(",")[1] || "");
  r.onerror = () => reject(new Error("Could not read that file."));
  r.readAsDataURL(file);
});

const tip = (title, ...body) => h("li", null, h("b", null, title), " ", ...body);

/** The "before you start" help: name, logo, version, what the project must contain, and the no-edit rule. */
export function kitGuide() {
  return h("details", { class: "guide", open: "" }, h("summary", null, "Before you start: name, logo and where your app runs"),
    h("ul", { class: "steps-list" },
      tip("App name.", "2 to 30 characters. It is shown under the icon on the phone or computer, so keep it short. Letters, numbers, spaces and - are fine."),
      tip("Logo.", "A square PNG, 512 × 512 or 1024 × 1024 pixels, under 2 MB. Keep the important part in the middle 70% because phones round the corners. No logo? We make a simple coloured icon."),
      tip("Version.", "Like 1.0.0. Use a higher number each time you build a new version."),
      tip("Your app's backend (optional).", "A .zip of your web app (for example Flask: ", h("code", null, "app.py"), ", your pages, ", h("code", null, "requirements.txt"), "). You host it on a platform you choose and keep your customers' accounts and data there. We give you the files back with deploy notes. Already have a website? Leave this empty."),
      tip("How it works.", "The Android or Windows app we build is a branded window that opens your app's https address. Heading, colour, announcement, products and updates come from Manage app, so you rarely need to rebuild.")));
}

/** Form that creates a managed app. onDone(app) is called with the id + password. */
export function kitForm({ maxMb, onDone }) {
  let project = null, logo = null;
  const name = h("input", { type: "text", maxlength: "30", placeholder: "For example: Anna's Candles", "aria-label": "App name" });
  const version = h("input", { type: "text", value: "1.0.0", maxlength: "11", "aria-label": "Version", style: "font-family:var(--mono)" });
  const logoPreview = h("img", { class: "logo-preview", alt: "", hidden: "" });
  const logoNote = h("p", { class: "faint", style: { fontSize: "13px" } }, "No logo chosen: a simple icon will be made for you.");
  const logoInput = h("input", { type: "file", accept: "image/png", hidden: "" });
  logoInput.addEventListener("change", () => {
    const f = logoInput.files[0]; if (!f) return;
    if (f.type !== "image/png") { toast("The logo must be a PNG.", { kind: "bad" }); return; }
    const url = URL.createObjectURL(f), img = new Image();
    img.onload = () => {
      if (img.width !== img.height || img.width < 256 || img.width > 2048) { toast(`Your logo is ${img.width} × ${img.height}. It must be square, between 256 and 2048 pixels (512 × 512 is ideal).`, { kind: "bad", timeout: 9000 }); URL.revokeObjectURL(url); return; }
      logo = f; logoPreview.src = url; logoPreview.hidden = false; logoNote.textContent = `${f.name} (${img.width} × ${img.height})`;
    };
    img.src = url;
  });
  const zipInput = h("input", { type: "file", accept: ".zip,application/zip", hidden: "" });
  const zipNote = h("p", { class: "faint", style: { fontSize: "13px" } }, handoff.folder != null ? `Using the folder “${handoff.folder || "(everything in Files)"}” you just previewed. Choose a .zip instead to use a different project.` : `Up to ${maxMb} MB.`);
  zipInput.addEventListener("change", () => { project = zipInput.files[0] || null; if (project) { zipNote.textContent = `${project.name} (${fmtBytes(project.size)})`; if (!name.value.trim()) name.value = project.name.replace(/\.zip$/i, "").slice(0, 30); } });
  const go = h("button", { class: "btn primary", type: "button", onclick: async () => {
    go.disabled = true; go.replaceChildren(h("span", { class: "spinner" }), "Preparing…");
    try {
      const r = await apiWith("/api/apps/create", {}, {
        name: name.value.trim(), version: version.value.trim(),
        project: project ? await fileToB64(project) : handoff.folder != null ? await folderB64(handoff.folder) : "", logo: logo ? await fileToB64(logo) : null });
      if (!project) handoff.folder = null;
      onDone(r);
    } catch (e) { toast(e.message, { kind: "bad", timeout: 9000 }); }
    go.disabled = false; go.replaceChildren(icon("package"), "Create app");
  } }, icon("package"), "Create app");
  const field = (label, ...kids) => h("div", { class: "field" }, h("label", null, label), ...kids);
  return h("div", { class: "fields", style: { marginBottom: "26px" } },
    field("App name", name), field("Version", version),
    field("Logo (optional)", h("div", { style: { display: "flex", gap: "12px", alignItems: "center" } }, logoPreview,
      h("button", { class: "btn small", type: "button", onclick: () => logoInput.click() }, icon("upload"), "Choose logo (PNG)")), logoNote, logoInput),
    field("Backend project .zip (optional)", h("button", { class: "btn small", type: "button", onclick: () => zipInput.click() }, icon("upload"), "Choose project .zip"), zipNote, zipInput), go);
}

async function folderB64(folder) {
  const res = await fetch(`/api/workspace/zip?path=${encodeURIComponent(folder)}`, { credentials: "same-origin" });
  if (!res.ok) throw new Error("Could not read that folder from Files.");
  return fileToB64(await res.blob());
}

export function kitBuildNotes(r) {
  const out = [];
  if (r.local_url) out.push(h("div", { class: "form-error", style: { background: "var(--warn-soft)", color: "var(--warn)" } },
    "This server is being used at a local address (localhost). The built app can't reach that. Set the public address (https) in Admin > Limits before building."));
  for (const w of r.warnings || []) out.push(h("p", { class: "muted" }, w));
  return out;
}

// ---------------------------------------------------------------------------------- Manage app: phone / computer apps
export function kitPanel({ app, creds, refresh, busy }) {
  const box = h("div"); let timer = null;
  const stat = (v, label) => h("div", { class: "stat" }, h("b", null, String(v)), h("span", null, label));
  const load = async () => {
    clearInterval(timer);
    let ov;
    try { ov = await apiWith("/api/apps/manage/overview", creds); } catch (e) { box.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    const kit = ov.app.kit || {}, u = ov.usage, pub = ov.publish;

    // --- Update: push drafts to every installed app, with a visible cool-down
    const status = h("p", { class: "muted" });
    const upBtn = h("button", { class: "btn primary", type: "button" }, icon("upload"), "Update");
    let left = pub.update.pending ? pub.update.seconds_left : 0;
    const paint = () => {
      upBtn.disabled = left > 0 || !pub.has_changes;
      status.textContent = left > 0 ? `Rolling out to your apps. They keep showing the old version for ${left} more second${left === 1 ? "" : "s"}, then switch. People who open the app now see “Updating app, wait or reopen in about a minute”.`
        : pub.has_changes ? "You have changes your customers can't see yet. Press Update to send them to every installed app."
        : pub.revision ? `Everything is live (update ${pub.revision}).` : "Add products or settings, then press Update to send them to your apps.";
    };
    paint();
    if (left > 0) timer = setInterval(() => { left -= 1; if (!box.isConnected) clearInterval(timer); else if (left <= 0) { clearInterval(timer); load(); } else paint(); }, 1000);
    upBtn.onclick = busy(upBtn, "Sending…", async () => {
      await apiWith("/api/apps/manage/publish", creds); toast("Update sent. Apps switch to it in about a minute."); load();
    });
    const updateCard = h("div", { class: "update-card" }, h("div", { class: "grow" }, h("b", null, left > 0 ? "Rolling out…" : pub.has_changes ? "Unpublished changes" : "Up to date"), status), upBtn);

    // --- who uses it
    const usage = h("div", { class: "stat-grid" }, stat(u.installs, "devices that opened it"), stat(u.active_24h, "active today"), stat(u.active_7d, "active this week"), stat(u.opens, "total opens"), stat(ov.inbox.new, "new in your inbox"));
    const split = [...u.platforms.map((x) => `${x.platform === "apk" ? "Android" : x.platform === "exe" ? "Windows" : "Unknown"}: ${x.devices}`),
      ...u.versions.map((x) => `version ${x.version}: ${x.devices}`)].join(" · ");
    const usageNote = h("p", { class: "faint", style: { fontSize: "13px", marginTop: "6px" } }, `${split || "No one has opened the app yet."} Counts are devices (a random id the app makes for itself), not people; no names or IP addresses are kept.`);

    // --- appearance settings (travel with Update)
    const st = ov.settings;
    const title = h("input", { type: "text", maxlength: "30", value: st.title, placeholder: "Heading shown at the top of the app", "aria-label": "Heading" });
    const accent = h("input", { type: "color", value: st.accent || "#2563eb", "aria-label": "Accent colour" });
    const note = h("input", { type: "text", maxlength: "200", value: st.announcement, placeholder: "For example: Free delivery this weekend", "aria-label": "Announcement" });
    const address = h("input", { type: "url", maxlength: "300", value: st.backend_url || "", placeholder: "https://your-app.example.com", "aria-label": "App address" });
    const save = h("button", { class: "btn", type: "button" }, "Save as draft");
    save.onclick = busy(save, "Saving…", async () => { await apiWith("/api/apps/manage/settings", creds, { title: title.value, accent: accent.value, announcement: note.value, backend_url: address.value }); toast("Saved. Press Update to send it."); load(); });
    const field = (label, ...kids) => h("div", { class: "field" }, h("label", null, label), ...kids);

    // --- download + steps
    const dlBtn = h("button", { class: "btn", type: "button" }, icon("download"), "Download backend to host");
    dlBtn.onclick = busy(dlBtn, "Preparing…", () => downloadWith("/api/apps/manage/backend", creds, `${app.app_id}-backend.zip`));
    const files = ov.files.map((f) => h("div", { class: "file-line" }, h("code", null, f.path), h("span", { class: "faint" }, fmtBytes(f.size))));

    box.replaceChildren(
      h("div", { class: "stat-grid" }, stat(`v${kit.kit_version || "?"}`, "app version"), stat(kit.builder || "no backend added", "framework")),
      updateCard,
      h("h3", { style: { margin: "26px 0 10px" } }, "Who is using it"), usage, usageNote,
      inboxPanel({ creds, onChange: load }),
      productsEditor({ app, creds, onChange: load }),
      h("h3", { style: { margin: "26px 0 6px" } }, "Look and message"),
      h("p", { class: "muted", style: { marginBottom: "10px", maxWidth: "62ch" } }, "The heading, accent colour and announcement are read by the app every time it opens, so they change with Update. Changing layout or code needs a new version (below)."),
      h("div", { class: "fields" }, field("Heading", title), field("Accent colour", accent), field("Announcement banner", note), field("App address (for the native app)", address, h("span", { class: "help" }, "Where your app runs, on your own hosting. Must start with https://. Changing it needs no rebuild.")), save),
      nativePanel({ app, creds, busy }),
      h("h3", { style: { margin: "26px 0 6px" } }, "Your backend and catalogue"),
      h("p", { class: "muted", style: { marginBottom: "8px" } }, `${ov.files.length} backend files · ${ov.collections.reduce((n, c) => n + c.items, 0)} catalogue items · ${ov.images} pictures`),
      h("details", { class: "guide" }, h("summary", null, "Show project files"), h("div", { class: "files-box" }, ...files)),
      ov.files.length ? h("div", { style: { margin: "16px 0" } }, dlBtn) : h("p", { class: "muted" }, "No backend files were added: this app opens a site you already host."), kitSteps(),
      versionForm({ app, creds, refresh, busy }));
  };
  load();
  return box;
}

function nativePanel({ app, creds, busy }) {
  const box = h("div"); let timer = null;
  const apk = h("input", { type: "checkbox", checked: "", id: "nb-apk" }), exe = h("input", { type: "checkbox", checked: "", id: "nb-exe" });
  const pkg = h("input", { type: "text", maxlength: "80", placeholder: `com.appworkbench.a${app.app_id} (default)`, "aria-label": "Package ID" });
  const go = h("button", { class: "btn primary", type: "button" }, icon("package"), "Build my app");
  const list = h("div", { style: { marginTop: "12px" } });
  const paint = async () => {
    clearInterval(timer);
    let r;
    try { r = await apiWith("/api/apps/manage/shell-builds", creds); } catch (e) { list.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    go.disabled = !r.available || r.builds.some((b) => b.status === "queued" || b.status === "building");
    if (!r.available) go.title = r.reason;
    const label = { queued: "Waiting to start…", building: "Building… this takes 10 to 20 minutes", done: "Ready", failed: "Failed" };
    list.replaceChildren(...(r.available ? [] : [h("p", { class: "muted" }, r.reason)]), ...r.builds.map((b) => h("div", { class: "file-line", style: { gap: "10px", flexWrap: "wrap" } },
      h("span", null, `${label[b.status]}${b.error ? `: ${b.error}` : ""}`), h("span", { class: "faint" }, `${b.platforms.replace("apk", "Android").replace("exe", "Windows")} · ${timeAgo(b.created_at)}`),
      ...b.files.map((k) => { const d = h("button", { class: "btn small", type: "button" }, icon("download"), k === "apk" ? "Android .apk" : "Windows .zip");
        d.onclick = busy(d, "Preparing…", () => downloadWith(`/api/apps/manage/shell-build/file?build_id=${b.id}&kind=${k}`, creds, k === "apk" ? `${app.name}.apk` : `${app.name}-windows.zip`)); return d; }),
      b.run_url ? h("a", { href: b.run_url, target: "_blank", rel: "noopener" }, "log") : null)));
    if (r.builds.some((b) => b.status === "queued" || b.status === "building")) timer = setInterval(() => (box.isConnected ? paint() : clearInterval(timer)), 20000);
  };
  go.onclick = busy(go, "Starting…", async () => {
    const platforms = [apk.checked && "apk", exe.checked && "exe"].filter(Boolean);
    await apiWith("/api/apps/manage/shell-build", creds, { platforms, package_id: pkg.value.trim() });
    toast("Build started"); paint();
  });
  box.append(h("h3", { style: { margin: "26px 0 6px" } }, "Your own app (Android and Windows)"),
    h("p", { class: "muted", style: { marginBottom: "10px", maxWidth: "62ch" } }, "We build a separate app with your name, logo and colour. It opens the app address you saved above and follows your heading, colour, announcement and Update button, so you rarely need to build again. The Android file installs directly; Google Play needs you to sign it with your own key. The Windows app is not signed, so Windows may warn on first start."),
    h("div", { class: "fields" }, h("label", { style: { display: "flex", gap: "8px", alignItems: "center" } }, apk, "Android (.apk)"), h("label", { style: { display: "flex", gap: "8px", alignItems: "center" } }, exe, "Windows"),
      h("div", { class: "field" }, h("label", null, "Package ID (optional)"), pkg, h("span", { class: "help" }, "Your app's unique name on phones, like com.yourshop.app. It can't be changed later without making a different app.")), go), list);
  paint();
  return box;
}

function kitSteps() {
  return h("ol", { class: "steps-list" },
    h("li", null, "Host your backend on a platform you choose (the downloaded files include DEPLOY.txt) and put its https address in ", h("b", null, "App address"), " above."),
    h("li", null, "Press ", h("b", null, "Build my app"), " in the section above and download the Android and Windows files when they are ready."),
    h("li", null, "Publish or share them. Products, heading, colour, announcement and the app address change with the Update button, with no rebuild."));
}

function inboxPanel({ creds, onChange }) {
  const box = h("div");
  const load = async () => {
    let data;
    try { data = await apiWith("/api/apps/manage/inbox/list", creds); } catch (e) { box.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    const rows = data.items.map((m) => h("div", { class: "list-row", style: { alignItems: "flex-start" } },
      h("div", { class: "grow" },
        h("b", null, `${m.collection} #${m.id}`, m.status === "new" ? " · new" : ""),
        h("small", null, `${new Date(m.created_at * 1000).toLocaleString()} · device ${m.from}`),
        h("div", { class: "inbox-fields" }, ...Object.entries(m.data).map(([k, v]) => h("div", null, h("span", { class: "faint" }, `${k}: `), String(v))))),
      h("button", { class: "btn small", onclick: async () => { try { await apiWith(`/api/apps/manage/inbox/status?item_id=${m.id}&status=${m.status === "new" ? "done" : "new"}`, creds); load(); onChange?.(); } catch (e) { toast(e.message, { kind: "bad" }); } } }, m.status === "new" ? "Mark done" : "Reopen"),
      h("button", { class: "btn small danger", "aria-label": "Delete", onclick: async () => {
        if (!(await confirmDialog("Delete this message?", "It is removed for good.", "Delete", true))) return;
        try { await apiWith(`/api/apps/manage/inbox/delete?item_id=${m.id}`, creds); load(); onChange?.(); } catch (e) { toast(e.message, { kind: "bad" }); }
      } }, icon("trash"))));
    box.replaceChildren(h("h3", { style: { margin: "26px 0 6px" } }, `Inbox (${data.items.length})`),
      h("p", { class: "muted", style: { marginBottom: "10px" } }, "Orders, messages and sign-ups your customers send from the app appear here."),
      h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, "Nothing yet"), "When a customer sends an order or message, it shows up here.")])));
  };
  load();
  return box;
}

function productsEditor({ app, creds, onChange }) {
  const box = h("div");
  let collection = "products";
  const load = async () => {
    let data;
    try { data = await apiWith(`/api/apps/manage/items/list?collection=${encodeURIComponent(collection)}`, creds); } catch (e) { box.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    const rows = data.items.map((it) => h("div", { class: "list-row" }, it.image_url ? h("img", { class: "thumb", src: it.image_url, alt: "" }) : icon("package"),
      h("div", { class: "grow" }, h("b", null, it.title), h("small", null, `${it.price == null ? "No price" : it.price.toFixed(2)}${it.in_stock ? "" : " · out of stock"}`)),
      h("button", { class: "btn small", onclick: () => edit(it) }, "Edit"),
      h("button", { class: "btn small danger", onclick: async () => {
        if (!(await confirmDialog(`Delete “${it.title}”?`, "It disappears from the app right away.", "Delete", true))) return;
        try { await apiWith(`/api/apps/manage/items/delete?item_id=${it.id}`, creds); load(); onChange?.(); } catch (e) { toast(e.message, { kind: "bad" }); }
      } }, icon("trash"))));
    box.replaceChildren(h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "8px", margin: "26px 0 6px" } },
      h("h3", null, `Products (${data.items.length})`), h("button", { class: "btn small primary", onclick: () => edit(null) }, "Add product")),
      h("p", { class: "muted", style: { marginBottom: "10px" } }, "These are drafts. Customers see them after you press Update (above)."),
      h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, "No products yet"), "Press “Add product”.")])));
  };
  const edit = (it) => {
    let image = it?.image || "";
    const title = h("input", { type: "text", maxlength: "120", value: it?.title || "", "aria-label": "Title" });
    const price = h("input", { type: "number", step: "0.01", min: "0", value: it?.price ?? "", "aria-label": "Price" });
    const desc = h("textarea", { rows: "3", maxlength: "2000", "aria-label": "Description" }, it?.description || "");
    const stock = h("input", { type: "checkbox" }); stock.checked = it ? it.in_stock : true;
    const imgUrl = h("input", { type: "text", placeholder: "or paste an https:// picture address", value: image.startsWith("http") ? image : "", "aria-label": "Picture address" });
    const note = h("p", { class: "faint", style: { fontSize: "13px" } }, image ? "Picture set." : "PNG, JPG, WEBP or GIF, up to 600 KB.");
    const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", hidden: "" });
    file.addEventListener("change", async () => {
      if (!file.files[0]) return;
      try { image = (await postFile("/api/apps/manage/image", file.files[0], creds)).image; imgUrl.value = ""; note.textContent = `${file.files[0].name} uploaded.`; }
      catch (e) { toast(e.message, { kind: "bad", timeout: 8000 }); }
    });
    const dlg = h("dialog", null, h("h3", null, it ? "Edit product" : "Add product"),
      h("div", { class: "fields" },
        h("div", { class: "field" }, h("label", null, "Title"), title), h("div", { class: "field" }, h("label", null, "Price"), price),
        h("div", { class: "field" }, h("label", null, "Description"), desc),
        h("div", { class: "field" }, h("label", null, "Picture"), h("button", { class: "btn small", type: "button", onclick: () => file.click() }, icon("upload"), "Upload picture"), imgUrl, note, file),
        h("label", { style: { display: "flex", gap: "8px", alignItems: "center" } }, stock, "In stock")),
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => dlg.close() }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          try {
            await apiWith("/api/apps/manage/items/save", creds, { id: it?.id, collection, title: title.value, price: price.value, description: desc.value,
              image: imgUrl.value.trim() || image, in_stock: stock.checked });
            dlg.close(); load(); onChange?.();
          } catch (e) { toast(e.message, { kind: "bad", timeout: 8000 }); }
        } }, "Save")));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg); dlg.showModal(); title.focus();
  };
  load();
  return box;
}

function versionForm({ app, creds, refresh, busy }) {
  const kit = app.kit || {};
  let project = null, logo = null;
  const version = h("input", { type: "text", value: "", placeholder: `higher than ${kit.kit_version}`, maxlength: "11", style: "font-family:var(--mono)", "aria-label": "New version" });
  const name = h("input", { type: "text", value: app.name, maxlength: "30", "aria-label": "App name" });
  const zip = h("input", { type: "file", accept: ".zip", hidden: "" }), png = h("input", { type: "file", accept: "image/png", hidden: "" });
  const zn = h("span", { class: "faint" }, "keep the current project"), pn = h("span", { class: "faint" }, "keep the current logo");
  zip.onchange = () => { project = zip.files[0] || null; zn.textContent = project ? project.name : "keep the current project"; };
  png.onchange = () => { logo = png.files[0] || null; pn.textContent = logo ? logo.name : "keep the current logo"; };
  const go = h("button", { class: "btn primary", type: "button" }, icon("upload"), "Save details");
  go.onclick = busy(go, "Preparing…", async () => {
    const r = await apiWith("/api/apps/manage/details", creds, { version: version.value.trim(), name: name.value.trim(),
      project: project ? await fileToB64(project) : null, logo: logo ? await fileToB64(logo) : null });
    toast("Saved. Build the app again to show a new name or logo."); refresh(r.app);
  });
  const field = (label, ...kids) => h("div", { class: "field" }, h("label", null, label), ...kids);
  return h("div", null, h("h3", { style: { margin: "26px 0 6px" } }, "Change the app itself"),
    h("p", { class: "muted", style: { marginBottom: "12px", maxWidth: "62ch" } }, "Products and the look don't need this. Use it to change the name or logo, replace the backend files, or raise the version. A new name or logo shows after you build the app again."),
    h("div", { class: "fields" }, field("New version", version), field("App name", name),
      field("New backend .zip (optional)", h("button", { class: "btn small", type: "button", onclick: () => zip.click() }, "Choose .zip"), zn, zip),
      field("New logo PNG (optional)", h("button", { class: "btn small", type: "button", onclick: () => png.click() }, "Choose PNG"), pn, png), go));
}

export function kitCredentialsDialog(app, { onClose } = {}) {
  const text = `${app.name}\nApp ID: ${app.app_id}\nManagement password: ${app.password}\n`;
  const row = (label, value) => h("div", { class: "secret-row" }, h("span", { class: "faint" }, label), h("code", null, value));
  const dlg = h("dialog", null, h("h3", null, "Your app is created"),
    h("p", { class: "muted", style: { marginBottom: "12px" } }, "Next: host your backend, enter its address in Manage app, then build your Android and Windows app there."),
    row("App ID", app.app_id), row("Password", app.password),
    h("div", { class: "form-error", style: { margin: "12px 0 0", background: "var(--warn-soft)", color: "var(--warn)" } },
      "The password is shown only this once. You need the app id and password in Manage app."),
    ...kitBuildNotes(app),
    h("div", { class: "actions" },
      h("button", { class: "btn", onclick: () => {
        const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: `${app.app_id}-credentials.txt` });
        document.body.append(a); a.click(); a.remove();
      } }, "Save id + password"),
      h("button", { class: "btn primary", onclick: () => dlg.close() }, "I have saved it")));
  dlg.addEventListener("close", () => { dlg.remove(); onClose?.(); });
  document.body.append(dlg); dlg.showModal();
}
