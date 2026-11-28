// Manage app > Hosting: run the app's backend on this platform (one container per app), choose where its data lives,
// and keep the owner's provider keys. Keys are write-only: once saved, only the names are shown.
import { apiWith } from "./api.js";
import { h, icon, toast, confirmDialog, timeAgo, copyText } from "./dom.js";

const STATE = { running: "Running", sleeping: "Asleep (wakes on the next visit)", stopped: "Stopped", failed: "Failed to start", "not deployed": "Not deployed yet" };

export function hostingPanel({ app, creds, busy, onChange, askAssistant }) {
  const box = h("div");
  const post = (path, body) => apiWith(`/api/apps/manage/hosting${path}`, creds, body);
  const fail = (e) => toast(e.message, { kind: "bad" });
  let logsBox = null;

  const paint = async () => {
    let s;
    try { s = await post(""); } catch (e) { box.replaceChildren(h("div", { class: "form-error" }, e.message)); return; }
    const act = (label, action, primary) => {
      const b = h("button", { class: `btn${primary ? " primary" : ""}`, type: "button" }, label);
      b.onclick = busy(b, "Working…", async () => { try { await post("/action", { action }); toast(action === "deploy" ? "Deployed" : "Done"); await paint(); onChange?.(); } catch (e) { fail(e); } });
      return b;
    };
    const deployed = s.state !== "not deployed";
    const chip = h("span", { class: `tag ${s.state === "running" ? "ok" : s.state === "failed" ? "bad" : ""}` }, STATE[s.state] || s.state);

    // data location: on our server (which database) or the owner's own online provider
    const st = { mode: s.data.mode, engine: s.data.engine, provider: s.data.provider };
    const dataBox = h("div");
    const drawData = () => {
      const d = s.data;
      const choice = (value, title, text) => {
        const r = h("input", { type: "radio", name: "dm", value, ...(st.mode === value ? { checked: "" } : {}) });
        r.onchange = () => {
          st.mode = value;
          if (value === "local" && !d.engines.some((e) => e.id === st.engine && e.local)) st.engine = "sqlite";
          if (value === "provider") { const q = d.providers.find((x) => x.id === st.provider) || d.providers[0]; st.provider = q.id; if (st.engine === "sqlite" && q.engine) st.engine = q.engine; }
          drawData();
        };
        return h("label", { class: "choice", style: { display: "flex", gap: "10px", alignItems: "flex-start", margin: "6px 0" } }, r, h("span", null, h("b", null, title), h("br"), h("span", { class: "muted" }, text)));
      };
      const select = (label, options, current, onPick) => {
        const el = h("select", { "aria-label": label }, ...options.map(([v, t]) => h("option", { value: v, ...(v === current ? { selected: "" } : {}) }, t)));
        el.onchange = () => onPick(el.value);
        return h("div", { class: "field" }, h("label", null, label), el);
      };
      const parts = [
        choice("local", "On our server", "One .db file on our server, private to this app. Daily backups are kept for 30 days."),
        choice("provider", "In my own online database", "Supabase, Neon, MongoDB Atlas, Upstash, Firebase or any other provider. You keep the account; we only keep its address, encrypted."),
      ];
      let value = null, result = null;
      if (st.mode === "local") {
        parts.push(select("Database", d.engines.filter((e) => e.local).map((e) => [e.id, e.label]), st.engine, (v) => { st.engine = v; drawData(); }),
          h("p", { class: "faint", style: { margin: "4px 0 8px", maxWidth: "62ch" } }, st.engine === "sqlite" ? "The simplest choice, and enough for most apps. The file lives in the app's private data folder." : "Needs Docker on this server. It starts with your app and is only reachable from your app."));
      } else {
        const p = d.providers.find((x) => x.id === st.provider) || d.providers[0];
        parts.push(select("Provider", d.providers.map((x) => [x.id, x.label]), p.id, (v) => { st.provider = v; const q = d.providers.find((x) => x.id === v); if (q && q.engine) st.engine = q.engine; drawData(); }),
          select("Database type", d.engines.map((e) => [e.id, e.label]), st.engine, (v) => { st.engine = v; drawData(); }),
          h("p", { class: "faint", style: { margin: "4px 0 8px", maxWidth: "62ch" } }, p.hint));
        const saved = d.has_address && s.data.mode === "provider" && s.data.engine === st.engine;
        const attrs = { maxlength: "8000", autocomplete: "new-password", "aria-label": "Database address", placeholder: saved ? "Saved. Paste a new one only to replace it." : (st.engine === "firestore" ? "Paste the whole key file" : "Paste the address") };
        value = st.engine === "firestore" ? h("textarea", { ...attrs, rows: "4", style: { fontFamily: "monospace" } }) : h("input", { type: "password", ...attrs });
        result = h("div", { class: "faint", role: "status", style: { margin: "6px 0" } });
        const test = h("button", { class: "btn", type: "button" }, "Test connection");
        test.onclick = busy(test, "Testing…", async () => {
          try {
            const r = await post("/data/test", { engine: st.engine, value: value.value });
            result.className = r.ok ? "tag ok" : "form-error"; result.textContent = r.message;
          } catch (e) { fail(e); }
        });
        parts.push(h("div", { class: "field" }, h("label", null, "Address (kept secret)"), value), test, result);
      }
      const save = h("button", { class: "btn primary", type: "button", style: { marginTop: "8px" } }, "Save");
      save.onclick = busy(save, "Saving…", async () => {
        try {
          const r = await post("/data", { mode: st.mode, engine: st.engine, provider: st.provider, value: value ? value.value : "" });
          if (value) value.value = "";
          toast(r.changed ? "Saved. Existing data is not copied to the new place." : "Saved. Restart the app to apply.");
          paint();
        } catch (e) { fail(e); }
      });
      parts.push(save);
      dataBox.replaceChildren(...parts);
    };
    drawData();

    // backups
    const backupsBox = h("div");
    const showBackups = (list) => {
      const when = (n) => `${n.slice(0, 4)}-${n.slice(4, 6)}-${n.slice(6, 8)} ${n.slice(9, 11)}:${n.slice(11, 13)} UTC`;
      const rows = list.map((b) => h("div", { class: "list-row" }, h("div", { class: "grow" }, h("b", null, when(b.name)), h("small", null, `${(b.bytes / 1024).toFixed(0)} KB`)),
        h("button", { class: "btn small", type: "button", onclick: async () => {
          if (!(await confirmDialog("Restore this backup?", "The app's current data is replaced by this copy, and the app restarts. Make a new backup first if you may want the current data.", "Restore", true))) return;
          try { await post("/backups/restore", { name: b.name }); toast("Restored"); paint(); } catch (e) { fail(e); }
        } }, "Restore")));
      const make = h("button", { class: "btn small", type: "button" }, "Back up now");
      make.onclick = busy(make, "Backing up…", async () => { try { showBackups((await post("/backups/create")).backups); toast("Backup saved"); } catch (e) { fail(e); } });
      backupsBox.replaceChildren(h("div", { class: "list" }, ...(rows.length ? rows : [h("div", { class: "empty-note" }, h("b", null, "No backups yet"))])), h("div", { style: { marginTop: "8px" } }, make));
    };
    post("/backups").then((r) => showBackups(r.backups)).catch(() => backupsBox.replaceChildren(h("p", { class: "faint" }, "Backups need the app to be deployed and Docker on this server.")));

    // keys
    const name = h("input", { type: "text", maxlength: "64", placeholder: "DATABASE_URL", autocomplete: "off", "aria-label": "Key name" });
    const value = h("input", { type: "password", maxlength: "8000", placeholder: "Paste the value", autocomplete: "new-password", "aria-label": "Key value" });
    const save = h("button", { class: "btn", type: "button" }, "Save key");
    save.onclick = busy(save, "Saving…", async () => {
      try { await post("/secret", { name: name.value.trim(), value: value.value }); value.value = ""; name.value = ""; toast("Saved. Restart the app to use it."); paint(); } catch (e) { fail(e); }
    });
    const names = s.secrets.map((k) => h("div", { class: "list-row" }, h("div", { class: "grow" }, h("b", null, k.name), h("small", null, `saved ${timeAgo(k.updated_at)} · the value is hidden`)),
      h("button", { class: "btn small danger", type: "button", "aria-label": `Remove ${k.name}`, onclick: async () => {
        if (!(await confirmDialog(`Remove ${k.name}?`, "The app will not get this key at its next start.", "Remove", true))) return;
        try { await post("/secret/delete", { name: k.name }); paint(); } catch (e) { fail(e); }
      } }, icon("trash"))));

    const logBtn = h("button", { class: "btn small", type: "button" }, "Show log");
    logsBox = h("pre", { class: "files-box", style: { maxHeight: "240px", overflow: "auto", whiteSpace: "pre-wrap", display: "none" } });
    logBtn.onclick = busy(logBtn, "Loading…", async () => { try { logsBox.textContent = (await post("/logs")).logs || "(nothing yet)"; logsBox.style.display = "block"; } catch (e) { fail(e); } });

    const adminBtn = h("button", { class: "btn primary", type: "button", ...(deployed ? {} : { disabled: "" }) }, "Open admin");
    adminBtn.onclick = async () => {
      const win = window.open("", "_blank");                       // opened on the tap itself so a phone does not block it
      try {
        const r = await apiWith("/api/apps/manage/admin-session", creds, {});
        if (win) { win.opener = null; win.location.href = r.url; } else { location.assign(r.url); }
        toast(`Admin open for ${Math.round(r.expires_in / 60)} minutes`);
      } catch (e) {
        if (win) win.close();
        if (askAssistant && /no admin (page|API) yet/.test(e.message || "")) {                 // the app has none: the assistant builds it, the owner does not have to ask
          toast("This app has no admin yet. Asking the assistant to build it…", { timeout: 6000 });
          askAssistant(app.kit?.session_id || "", `My app "${app.name || app.app_id}" (app id ${app.app_id}) has no admin yet: Open admin says it was not found. Add its admin dashboard now. `
            + "Find the app's project folder in my files, call analyze_project if it is a copied project, use_skill admin-access (section B or A), and write admin.json (everything I manage inside the app: content, orders, users, with pictures and videos) plus the /admin-api routes, "
            + "guarded only by the X-Workbench-Admin header. The platform draws the dashboard, so write no admin pages. Do not add any admin button or login to the customer screens. Then run check_build and tell me to press Update in Manage app.");
          return;
        }
        fail(e);
      }
    };

    const address = s.address ? h("div", { class: "file-line", style: { gap: "10px", flexWrap: "wrap" } }, h("code", null, s.address),
      h("button", { class: "btn small", type: "button", onclick: () => copyText(s.address).then(() => toast("Copied")) }, "Copy")) : null;

    box.replaceChildren(...[
      h("h3", { style: { margin: "26px 0 6px" } }, "Hosting: we run your app for you"),
      h("p", { class: "muted", style: { marginBottom: "10px", maxWidth: "62ch" } }, "Your backend runs on this platform in its own private container, so your customers' apps keep working after you publish. It sleeps when nobody uses it and wakes on the next visit."),
      s.available ? null : h("div", { class: "form-error" }, s.reason),
      h("div", { class: "file-line", style: { gap: "10px", flexWrap: "wrap" } }, chip, s.last_started ? h("span", { class: "faint" }, `started ${timeAgo(s.last_started)}`) : null, s.isolation === "process" ? h("span", { class: "faint" }, "test mode: no container isolation") : null),
      s.error ? h("div", { class: "form-error" }, s.error) : null, address,
      s.address && s.path_mode ? h("p", { class: "faint", style: { margin: "6px 0" } }, "Test address: the page runs in a restricted frame, so sign-in cookies and browser storage do not work here. A server with its own domain removes this limit.") : null,
      h("div", { class: "btn-row", style: { display: "flex", gap: "8px", flexWrap: "wrap", margin: "12px 0" } },
        act(deployed ? "Deploy latest files" : "Deploy", "deploy", !deployed), deployed ? act("Restart", "restart") : null, deployed && s.state !== "stopped" ? act("Stop", "stop") : null, deployed && s.state === "stopped" ? act("Start", "start") : null, logBtn),
      logsBox,
      h("h4", { style: { margin: "18px 0 4px" } }, "Your app's admin"),
      h("p", { class: "muted", style: { marginBottom: "8px", maxWidth: "62ch" } }, "Everything inside your app (content, orders, users) is managed in its admin, not here: this opens it, after you entered the app id and password. This section only deploys, builds and updates versions. The admin is never part of the installed app and is refused on the app's public address."),
      h("div", { class: "btn-row" }, adminBtn),
      h("h4", { style: { margin: "18px 0 4px" } }, "Where your app's data lives"),
      dataBox,
      h("h4", { style: { margin: "18px 0 4px" } }, "Backups"),
      h("p", { class: "muted", style: { marginBottom: "8px", maxWidth: "62ch" } }, "A copy of the app's saved data (not an online database) is made every day and kept for 30 days. The app pauses for a few seconds while it is packed."),
      backupsBox,
      h("h4", { style: { margin: "18px 0 4px" } }, "Your keys"),
      h("p", { class: "muted", style: { marginBottom: "8px", maxWidth: "62ch" } }, "Encrypted before they are stored, never shown again and never put in your files. Your app reads them as environment variables, for example os.environ[\"DATABASE_URL\"]. Use CAPITAL_LETTERS for names."),
      s.vault ? null : h("div", { class: "form-error" }, `Saving keys is not set up: ${s.vault_problem || "SECRETS_MASTER_KEY is missing."} (Only needed if you choose your own provider.)`),
      h("div", { class: "list" }, ...(names.length ? names : [h("div", { class: "empty-note" }, h("b", null, "No keys saved"))])),
      h("div", { class: "fields", style: { marginTop: "10px" } }, h("div", { class: "field" }, h("label", null, "Name"), name), h("div", { class: "field" }, h("label", null, "Value"), value), save)].filter(Boolean));   // replaceChildren would print a null as the word "null"
  };
  paint();
  return box;
}
