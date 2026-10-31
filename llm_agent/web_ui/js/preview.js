// Live preview window: runs the app the assistant wrote in a sandboxed frame at phone, tablet or desktop size.
import { api } from "./api.js";
import { h, icon, toast } from "./dom.js";

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

export async function openPreview(startPath) {
  let pages = [];
  try { pages = await listPages(); } catch (e) { toast(e.message, { kind: "bad" }); return; }
  if (!pages.length) { toast("There is no web page to preview yet. Ask the assistant to build one.", { kind: "bad" }); return; }
  let path = pages.includes(startPath) ? startPath : pages[0];
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

  const dlg = h("dialog", { class: "pv", "aria-label": "App preview" },
    h("div", { class: "pv-bar" }, h("b", { class: "pv-title" }, "Preview"), select, h("div", { class: "pv-devices" }, ...devBtns, rotate), sizeLabel, h("span", { style: { flex: 1 } }), reload, external, close),
    err, stage);

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
    try {
      const r = await api("/api/preview", { method: "POST", body: { path } });
      url = r.url; frame.src = url; external.href = url;
    } catch (e) { err.hidden = false; err.textContent = e.message; }
  }

  const ro = new ResizeObserver(paint);
  function done() { ro.disconnect(); dlg.close(); }
  dlg.addEventListener("close", () => { ro.disconnect(); dlg.remove(); });
  dlg.addEventListener("cancel", () => { ro.disconnect(); });
  document.body.append(dlg); dlg.showModal();
  ro.observe(stage); paint(); load();
}
