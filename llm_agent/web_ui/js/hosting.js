// Manage app > Hosting: run the app's backend on this platform (one container per app), choose where its data lives,
// and keep the owner's provider keys. Keys are write-only: once saved, only the names are shown.
import { apiWith } from "./api.js";
import { h, icon, toast, confirmDialog, timeAgo, copyText } from "./dom.js";

const STATE = { running: "Running", sleeping: "Asleep (wakes on the next visit)", stopped: "Stopped", failed: "Failed to start", "not deployed": "Not deployed yet" };

export function hostingPanel({ app, creds, busy, onChange }) {
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

    // data location
    const mode = (value, title, text) => {
      const r = h("input", { type: "radio", name: "dm", value, ...(s.data_mode === value ? { checked: "" } : {}) });
      r.onchange = async () => { try { await post("/data", { mode: value }); toast("Saved. Restart the app to apply."); paint(); } catch (e) { fail(e); } };
      return h("label", { class: "choice", style: { display: "flex", gap: "10px", alignItems: "flex-start", margin: "6px 0" } }, r, h("span", null, h("b", null, title), h("br"), h("span", { class: "muted" }, text)));
    };

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
      h("h4", { style: { margin: "18px 0 4px" } }, "Where your app's data lives"),
      mode("local", "A database on our server", "Simple. The app keeps its files and SQLite database in its private data folder. Download backups from your own copy of the data before big changes; we do not read it."),
      mode("provider", "Your own provider", "Use a database or sign-in service you trust (for example Supabase, Neon, MongoDB Atlas, Firebase). Save its address and keys below; they are sent only to your app's container."),
      h("h4", { style: { margin: "18px 0 4px" } }, "Your keys"),
      h("p", { class: "muted", style: { marginBottom: "8px", maxWidth: "62ch" } }, "Encrypted before they are stored, never shown again and never put in your files. Your app reads them as environment variables, for example os.environ[\"DATABASE_URL\"]. Use CAPITAL_LETTERS for names."),
      s.vault ? null : h("div", { class: "form-error" }, `Saving keys is not set up: ${s.vault_problem || "SECRETS_MASTER_KEY is missing."} (Only needed if you choose your own provider.)`),
      h("div", { class: "list" }, ...(names.length ? names : [h("div", { class: "empty-note" }, h("b", null, "No keys saved"))])),
      h("div", { class: "fields", style: { marginTop: "10px" } }, h("div", { class: "field" }, h("label", null, "Name"), name), h("div", { class: "field" }, h("label", null, "Value"), value), save)].filter(Boolean));   // replaceChildren would print a null as the word "null"
  };
  paint();
  return box;
}
