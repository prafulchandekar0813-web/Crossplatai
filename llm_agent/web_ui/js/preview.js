// Live preview window: runs the app the assistant wrote in a sandboxed frame at phone, tablet or desktop size.
import { api } from "./api.js";
import { h, icon, toast } from "./dom.js";
import { handoff } from "./kits.js";

const DEVICES = {
  mobile: { label: "Phone", w: 390, h: 844, ico: "phone" },
  tablet: { label: "Tablet", w: 820, h: 1180, ico: "tablet" },
  desktop: { label: "Desktop", w: 1280, h: 800, ico: "desktop" },
};
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

export async function listPages() {
  const { entries } = await api("/api/workspace");
  return entries.filter((e) => e.type === "file" && /\.html?$/i.test(e.path)).map((e) => e.path)
    .sort((a, b) => (/(^|\/)index\.html?$/i.test(b) - /(^|\/)index\.html?$/i.test(a)) || a.split("/").length - b.split("/").length || a.localeCompare(b));
}

/** Folders that look like a runnable app (have app.py, main.py, server.py or run.json). "" is the workspace root. */
export async function listApps() {
  const { entries } = await api("/api/workspace");
  const dirs = new Set();
  for (const e of entries) {
    const m = e.type === "file" && e.path.match(/^(?:(.*)\/)?(app\.py|main\.py|server\.py|run\.json)$/);
    if (m && (m[1] || "").split("/").filter(Boolean).length <= 2) dirs.add(m[1] || "");
  }
  return [...dirs].sort();
}

export async function openPreview(startPath, { onBuild, sessionId } = {}) {
  const sid = (sessionId && sessionId()) || "";
  let pages = [], apps = [], status = { enabled: false, run: null };
  try { [pages, apps, status] = await Promise.all([listPages(), listApps(), api(`/api/run?session_id=${encodeURIComponent(sid)}`).catch(() => status)]); } catch (e) { toast(e.message, { kind: "bad" }); return; }
  const canRun = status.enabled && apps.length > 0 && !!sid;
  if (!pages.length && !canRun) { toast(status.enabled ? "Nothing to preview yet. Ask the assistant to build an app." : "There is no web page to preview yet. Ask the assistant to build one.", { kind: "bad" }); return; }
  const notice = !status.available && status.enabled ? status.reason : "";
  let mode = !pages.length || (canRun && status.run) ? "app" : "page";
  let runId = status.run?.id || null, runTimer = null, left = status.run?.seconds_left || 0;
  let path = pages.includes(startPath) ? startPath : pages[0] || "";
  let device = store.get("workbench.device") in DEVICES ? store.get("workbench.device") : "mobile";
  let landscape = false, url = "";

  const frame = h("iframe", { title: "App preview", sandbox: "allow-scripts allow-forms allow-modals allow-popups", referrerpolicy: "no-referrer" });
  const holder = h("div", { class: "pv-device" }, frame);
  const stage = h("div", { class: "pv-stage" }, holder);
  const sizeLabel = h("span", { class: "pv-size" });
  const err = h("div", { class: "form-error", hidden: true, style: { margin: "10px 16px 0" } });

  const select = h("select", { "aria-label": "Page to preview", onchange: () => { path = select.value; load(); } },
    ...pages.map((p) => h("option", { value: p, selected: p === path ? "" : null }, p)));
  const devBtns = Object.entries(DEVICES).map(([id, d]) =>
    h("button", { class: "btn small", "aria-pressed": String(id === device), title: `${d.label} (${d.w} × ${d.h})`, onclick: () => { device = id; store.set("workbench.device", id); paint(); } }, icon(d.ico), h("span", { class: "pv-lbl" }, d.label)));
  const rotate = h("button", { class: "icon-btn", title: "Rotate", "aria-label": "Rotate device", onclick: () => { landscape = !landscape; paint(); } }, icon("rotate"));
  const reload = h("button", { class: "icon-btn", title: "Reload", "aria-label": "Reload preview", onclick: () => load() }, icon("refresh"));
  const external = h("a", { class: "icon-btn", title: "Open in a new tab", "aria-label": "Open in a new tab", target: "_blank", rel: "noopener" }, icon("external"));
  const close = h("button", { class: "icon-btn", title: "Close", "aria-label": "Close preview", onclick: () => done() }, icon("x"));

  // ---- run the real app (backend + frontend) in this chat's environment
  const appSel = h("select", { "aria-label": "App folder to run" }, ...apps.map((a) => h("option", { value: a }, a || "(everything in Files)")));
  if (status.run && apps.includes(status.run.path)) appSel.value = status.run.path;
  const runBtn = h("button", { class: "btn small primary", type: "button", onclick: () => startRun() }, icon("play"), "Run app");
  const stopBtn = h("button", { class: "btn small", type: "button", onclick: () => stopRun() }, "Stop");
  const logsBtn = h("button", { class: "btn small", type: "button", onclick: () => toggleLogs() }, "Log");
  const timeLbl = h("span", { class: "pv-size" });
  const buildBtn = h("button", { class: "btn small primary", type: "button", onclick: () => { handoff.folder = appSel.value; done(); onBuild?.(); } }, icon("package"), "Looks good, build it");
  const logBox = h("pre", { class: "pv-log", hidden: "" });
  const connBtn = h("button", { class: "btn small", type: "button", onclick: () => toggleConn() }, "Connections");
  const connBox = h("div", { class: "pv-log", hidden: "", style: { whiteSpace: "normal", font: "inherit", padding: "12px" } });
  const modeBtns = [["page", "Page"], ["app", "Run app"]].filter(([m]) => (m === "page" ? pages.length : canRun))
    .map(([m, label]) => h("button", { class: "btn small", "data-mode": m, onclick: () => { mode = m; paintMode(); if (m === "page") load(); else if (runId) showRun(); } }, label));
  const pageBar = h("span", { class: "pv-group" }, select);
  if (!status.available) { runBtn.disabled = true; runBtn.title = status.reason; }
  const appBar = h("span", { class: "pv-group" }, appSel, runBtn, stopBtn, logsBtn, connBtn, timeLbl, buildBtn);

  const dlg = h("dialog", { class: "pv", "aria-label": "App preview" },
    h("div", { class: "pv-bar" }, h("b", { class: "pv-title" }, "Preview"), ...(modeBtns.length > 1 ? [h("div", { class: "pv-devices" }, ...modeBtns)] : []), pageBar, appBar,
      h("div", { class: "pv-devices" }, ...devBtns, rotate), sizeLabel, h("span", { style: { flex: 1 } }), reload, external, close),
    err, stage, logBox, connBox);

  function paintMode() {
    pageBar.hidden = mode !== "page"; appBar.hidden = mode !== "app";
    modeBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    const running = !!runId;
    runBtn.hidden = running; stopBtn.hidden = !running; logsBtn.hidden = !running; buildBtn.hidden = !running; timeLbl.hidden = !running;
    if (!running) logBox.hidden = true;
    connBtn.hidden = !canRun;
  }
  function tick() {
    clearInterval(runTimer);
    if (!runId) return;
    const paintTime = () => { timeLbl.textContent = left > 0 ? `stops in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "stopped"; };
    paintTime();
    runTimer = setInterval(() => { left -= 1; if (left <= 0 || !dlg.isConnected) { clearInterval(runTimer); if (left <= 0) { runId = null; frame.removeAttribute("src"); paintMode(); } } else paintTime(); }, 1000);
  }
  async function showRun() {
    try { const r = await api(`/api/run/${runId}/link`, { method: "POST" }); frame.src = r.url; external.href = r.url; left = r.seconds_left; tick(); }
    catch (e) { runId = null; paintMode(); err.hidden = false; err.textContent = e.message; }
  }
  async function startRun() {
    err.hidden = true; runBtn.disabled = true; runBtn.replaceChildren(h("span", { class: "spinner" }), "Starting…");
    try {
      const r = await api("/api/run", { method: "POST", body: { path: appSel.value, session_id: sid } });
      runId = r.id; frame.src = r.url; external.href = r.url; left = r.seconds_left; tick();
    } catch (e) { err.hidden = false; err.textContent = e.message; err.style.whiteSpace = "pre-wrap"; }
    runBtn.disabled = false; runBtn.replaceChildren(icon("play"), "Run app"); paintMode();
  }
  async function stopRun() { const id = runId; runId = null; clearInterval(runTimer); frame.removeAttribute("src"); paintMode(); try { await api(`/api/run/${id}`, { method: "DELETE" }); } catch { /* already gone */ } }
  async function toggleConn() {
    if (!connBox.hidden) { connBox.hidden = true; return; }
    let items;
    try { items = (await api(`/api/run/connections?session_id=${encodeURIComponent(sid)}`)).items; } catch (e) { toast(e.message, { kind: "bad" }); return; }
    const rows = h("div", { style: { display: "grid", gap: "6px", margin: "8px 0" } });
    const addRow = (it = { name: "", secret: false, set: false, value: "" }) => {
      const n = h("input", { placeholder: "NAME, e.g. API_BASE_URL", value: it.name, "aria-label": "Setting name", style: { flex: "1 1 180px" } });
      const v = h("input", { placeholder: it.set && it.secret ? "Saved. Type to replace." : "value", value: it.value, type: /KEY|SECRET|TOKEN|PASS/i.test(it.name) ? "password" : "text", autocomplete: "off", "aria-label": "Setting value", style: { flex: "2 1 240px" } });
      n.addEventListener("input", () => { v.type = /KEY|SECRET|TOKEN|PASS/i.test(n.value) ? "password" : "text"; });
      const row = h("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap" } }, n, v, h("button", { class: "icon-btn", type: "button", title: "Remove", "aria-label": "Remove setting", onclick: () => row.remove() }, icon("x")));
      row._get = () => [n.value.trim(), v.value];
      rows.append(row);
    };
    items.forEach(addRow);
    if (!items.length) addRow();
    const save = h("button", { class: "btn small primary", type: "button", onclick: async () => {
      const body = {}; rows.querySelectorAll(":scope > div").forEach((r) => { const [k, val] = r._get(); if (k) body[k] = val; });
      save.disabled = true;
      try { const r = await api("/api/run/connections", { method: "PUT", body: { session_id: sid, items: body } }); toast(r.restart ? "Saved. Stop and run the app again to use them." : "Saved."); connBox.hidden = true; }
      catch (e) { toast(e.message, { kind: "bad", timeout: 8000 }); } finally { save.disabled = false; }
    } }, "Save");
    connBox.replaceChildren(h("b", null, "Connections for this preview"),
      h("p", { class: "muted", style: { margin: "4px 0" } }, "Your app reads these as environment variables (os.environ). Use test or sandbox keys here. They stay on this chat's preview only and are never put into the app you build; set the real ones where you host the app."),
      rows, h("div", { style: { display: "flex", gap: "8px" } }, h("button", { class: "btn small", type: "button", onclick: () => addRow() }, "Add setting"), save));
    connBox.hidden = false;
  }
  async function toggleLogs() {
    if (!logBox.hidden) { logBox.hidden = true; return; }
    try { logBox.textContent = (await api(`/api/run/${runId}/logs`)).logs || "(no output yet)"; logBox.hidden = false; logBox.scrollTop = logBox.scrollHeight; }
    catch (e) { toast(e.message, { kind: "bad" }); }
  }

  function paint() {
    const d = DEVICES[device];
    let w = d.w, hh = d.h;
    if (landscape && device !== "desktop") [w, hh] = [hh, w];
    const sw = Math.max(100, stage.clientWidth - 32), sh = Math.max(100, stage.clientHeight - 32);
    const k = Math.min(1, sw / w, sh / hh);
    holder.style.width = `${w * k}px`; holder.style.height = `${hh * k}px`;
    holder.dataset.device = device;
    frame.style.width = `${w}px`; frame.style.height = `${hh}px`; frame.style.transform = `scale(${k})`;
    sizeLabel.textContent = `${w} × ${hh}${k < 1 ? `, shown at ${Math.round(k * 100)}%` : ""}`;
    devBtns.forEach((b, i) => b.setAttribute("aria-pressed", String(Object.keys(DEVICES)[i] === device)));
    rotate.disabled = device === "desktop";
  }

  async function load() {
    err.hidden = true;
    if (mode === "app") { if (runId) showRun(); return; }
    try {
      const r = await api("/api/preview", { method: "POST", body: { path } });
      url = r.url; frame.src = url; external.href = url;
    } catch (e) { err.hidden = false; err.textContent = e.message; }
  }

  const ro = new ResizeObserver(paint);
  function done() { ro.disconnect(); clearInterval(runTimer); dlg.close(); }
  dlg.addEventListener("close", () => { ro.disconnect(); clearInterval(runTimer); dlg.remove(); });
  dlg.addEventListener("cancel", () => { ro.disconnect(); });
  document.body.append(dlg); dlg.showModal();
  ro.observe(stage); paint(); paintMode(); load();
  if (notice && mode === "app") { err.hidden = false; err.textContent = notice; }
}
