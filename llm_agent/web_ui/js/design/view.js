// The page an installed app opens when its owner has put a design on it: /ui/<app id>/
import { renderApp } from "./render.js";

const root = document.getElementById("root");
/** A random 24-hex id for this device, kept so the owner's numbers count a returning visitor once. */
function deviceId() {
  const make = () => [...crypto.getRandomValues(new Uint8Array(12))].map((x) => x.toString(16).padStart(2, "0")).join("");
  try { let d = localStorage.getItem("ui_device"); if (!/^[0-9a-f]{24}$/.test(d || "")) { d = make(); localStorage.setItem("ui_device", d); } return d; } catch { return make(); }
}
const id = location.pathname.split("/").filter(Boolean)[1] || "";

(async () => {
  try {
    const res = await fetch(`/api/ui/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("none");
    const data = await res.json();
    document.title = data.name;
    const device = deviceId();
    const items = new Map();   // one request per section, shared by every block that shows it
    const load = (section) => { if (!items.has(section)) items.set(section, fetch(`/api/catalog/${encodeURIComponent(id)}/${encodeURIComponent(section)}`, { headers: { "x-device-id": device, "x-platform": "web" } }).then((r) => { if (!r.ok) throw new Error("x"); return r.json(); })); return items.get(section); };
    const submit = async (section, body) => {
      const r = await fetch(`/api/catalog/${encodeURIComponent(id)}/submit/${encodeURIComponent(section)}`, { method: "POST", headers: { "content-type": "application/json", "x-device-id": device }, body: JSON.stringify(body) });
      if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || e.detail || "Could not send. Please try again."); }
    };
    renderApp(root, data.design, { logo: data.logo, items: load, submit, root, start: location.hash.slice(1), onPage: (p) => history.replaceState(null, "", `#${p}`) });
  } catch {
    root.replaceChildren(Object.assign(document.createElement("div"), { className: "msg", textContent: "This app has no screens yet." }));
  }
})();
