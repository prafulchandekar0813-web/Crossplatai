// Draws a design. The editor's canvas and the page an installed app opens use this same code, so what you design is what
// people see. Nothing here builds HTML from data: text goes in as text, styles go through the style object.
import { iconSvg } from "./icons.js";

const FONTS = {
  system: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  serif: 'Georgia, "Times New Roman", serif',
  rounded: '"Trebuchet MS", "Segoe UI", system-ui, sans-serif',
  mono: 'ui-monospace, Menlo, Consolas, monospace',
};
const SIZE = { heading: { s: 18, m: 24, l: 32 }, text: { s: 13, m: 16, l: 20 } };
const FLEX = { start: "flex-start", center: "center", end: "flex-end", stretch: "stretch", left: "flex-start", right: "flex-end" };
const JUSTIFY = { start: "flex-start", center: "center", end: "flex-end", between: "space-between" };
const RADIUS = { square: "4px", soft: "12px", round: "24px" };
const SAMPLE_ITEMS = [1, 2, 3].map((n) => ({ id: n, heading: `Sample item ${n}`, price: 9.99, description: "Your own items from Manage app appear here.", image_url: "" }));
const SAFE_LINK = /^(https:|tel:|mailto:)/;


function node(tag, css = {}, text) {
  const n = document.createElement(tag);
  Object.assign(n.style, css);
  if (text != null) n.textContent = text;
  return n;
}

/** Put the design's colours and font on an element; blocks read them through CSS variables. */
export function applyTheme(root, theme) {
  root.style.setProperty("--d-accent", theme.accent);
  root.style.setProperty("--d-bg", theme.bg);
  root.style.setProperty("--d-text", theme.text);
  root.style.setProperty("--d-surface", theme.surface || "#f1f4fa");
  root.style.setProperty("--d-radius", RADIUS[theme.radius] || RADIUS.soft);
  Object.assign(root.style, { background: theme.bg, color: theme.text, fontFamily: FONTS[theme.font] || FONTS.system });
}

function runAction(action, ctx) {
  if (ctx.editable) return;                                   // in the editor, tapping a button only selects it
  if (action.type === "page") ctx.go(action.target);
  else if (action.type === "link" && SAFE_LINK.test(action.url)) window.open(action.url, "_blank", "noopener,noreferrer");
}

function body(b, ctx) {
  const color = b.color || "";
  switch (b.type) {
    case "heading":
      return node("div", { fontSize: `${SIZE.heading[b.size] || 24}px`, fontWeight: 700, lineHeight: 1.2, textAlign: b.align, color, overflowWrap: "anywhere" }, b.text);
    case "text":
      return node("div", { fontSize: `${SIZE.text[b.size] || 16}px`, lineHeight: 1.5, textAlign: b.align, color, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }, b.text);
    case "button": {
      const wrap = node("div", { textAlign: b.align });
      const solid = b.style === "solid";
      const btn = node("button", {
        font: "inherit", fontWeight: 600, padding: "12px 20px", minHeight: "44px", borderRadius: "var(--d-radius)", cursor: "pointer", width: b.full ? "100%" : "auto", maxWidth: "100%",
        background: solid ? "var(--d-accent)" : "transparent", color: solid ? "#fff" : "var(--d-accent)", border: "2px solid var(--d-accent)",
        display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "8px",
      });
      if (b.icon) btn.append(iconSvg(b.icon, 18));
      btn.append(document.createTextNode(b.label));
      btn.type = "button";
      btn.addEventListener("click", () => runAction(b.action, ctx));
      wrap.append(btn);
      return wrap;
    }
    case "image": {
      const src = b.src === "@logo" ? ctx.logo || "" : b.src;
      const box = { width: "100%", height: `${b.height}px`, borderRadius: `${b.radius}px`, objectFit: b.fit, display: "block" };
      const holder = () => node("div", { ...box, background: "rgba(128,128,128,.18)", display: "grid", placeItems: "center", fontSize: "13px", opacity: ".8" }, "Image");
      if (!src) return ctx.editable ? holder() : node("div");
      const img = node("img", box);
      img.alt = b.alt; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = src;
      img.addEventListener("error", () => img.replaceWith(ctx.editable ? holder() : node("div")));
      return img;
    }
    case "shape": {
      const wrap = node("div", { display: "flex", justifyContent: FLEX[b.align] });
      const fill = b.color || "var(--d-accent)";
      const css = b.shape === "circle" ? { width: `${b.height}px`, height: `${b.height}px`, borderRadius: "50%" }
        : b.shape === "line" ? { width: `${b.width}%`, height: "3px", borderRadius: "2px" } : { width: `${b.width}%`, height: `${b.height}px`, borderRadius: "10px" };
      wrap.append(node("div", { ...css, background: fill }));
      return wrap;
    }
    case "group": {
      const row = b.direction === "row";
      const g = node("div", { display: "flex", flexDirection: row ? "row" : "column", flexWrap: row && b.wrap ? "wrap" : "nowrap", justifyContent: row ? JUSTIFY[b.justify] : "", gap: `${b.gap}px`, padding: `${b.pad}px`,
        background: b.bg || (b.card ? "var(--d-surface)" : ""), borderRadius: `${b.radius}px`, alignItems: FLEX[b.align], minHeight: ctx.editable ? "44px" : "0",
        border: b.card ? "1px solid rgba(128,128,128,.25)" : "0", boxSizing: "border-box" });
      for (const c of b.children) {
        const child = renderBlock(c, ctx);
        if (row) Object.assign(child.style, { flex: b.sizing === "natural" ? "0 1 auto" : b.wrap ? "1 1 140px" : "1 1 0", minWidth: "0" });
        g.append(child);
      }
      if (ctx.editable && !b.children.length) g.append(node("div", { opacity: ".55", fontSize: "13px", margin: "auto" }, "Drop blocks here"));
      return g;
    }
    case "icon": {
      const wrap = node("div", { display: "flex", justifyContent: FLEX[b.align], color: b.color || "var(--d-accent)" });
      wrap.append(iconSvg(b.icon, b.size));
      return wrap;
    }
    case "badge": {
      const wrap = node("div", { display: "flex", justifyContent: FLEX[b.align] });
      wrap.append(node("span", { background: b.bg || "var(--d-accent)", color: "#fff", fontWeight: 600, fontSize: "13px", padding: "4px 12px", borderRadius: "999px", overflowWrap: "anywhere" }, b.text));
      return wrap;
    }
    case "banner":
      return node("div", { background: b.bg || "var(--d-accent)", color: b.color || "#fff", textAlign: b.align, padding: "12px 16px", borderRadius: "var(--d-radius)", fontWeight: 600, overflowWrap: "anywhere" }, b.text);
    case "items":
      return itemsBlock(b, ctx);
    case "quote": {
      const q = node("div", { textAlign: b.align, color: b.color || "", borderLeft: b.align === "left" ? "4px solid var(--d-accent)" : "0", padding: "4px 12px" });
      q.append(node("div", { fontSize: "18px", fontStyle: "italic", lineHeight: "1.45", overflowWrap: "anywhere" }, `“${b.text}”`));
      if (b.author) q.append(node("div", { marginTop: "6px", fontSize: "14px", opacity: ".7" }, `— ${b.author}`));
      return q;
    }
    case "faq": {
      const wrap = node("div", { display: "flex", flexDirection: "column", gap: "8px" });
      for (const [q, a] of pairs(b.items)) {
        const d = node("details", { background: "var(--d-surface)", borderRadius: "var(--d-radius)", padding: "10px 14px" });
        d.append(node("summary", { fontWeight: 600, cursor: "pointer", overflowWrap: "anywhere" }, q), node("div", { marginTop: "8px", opacity: ".85", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }, a));
        wrap.append(d);
      }
      return wrap;
    }
    case "table": {
      const wrap = node("div", { display: "flex", flexDirection: "column", background: "var(--d-surface)", borderRadius: "var(--d-radius)", padding: "4px 14px" });
      pairs(b.rows).forEach(([k, v], i) => {
        const r = node("div", { display: "flex", justifyContent: "space-between", gap: "12px", padding: "9px 0", borderTop: i ? "1px solid rgba(128,128,128,.25)" : "0" });
        r.append(node("span", { opacity: ".8", overflowWrap: "anywhere" }, k), node("span", { fontWeight: 600, textAlign: "right", overflowWrap: "anywhere" }, v));
        wrap.append(r);
      });
      return wrap;
    }
    case "list": {
      const wrap = node("div", { display: "flex", flexDirection: "column", gap: "8px" });
      for (const line of b.lines.split("\n").map((x) => x.trim()).filter(Boolean)) {
        const r = node("div", { display: "flex", gap: "10px", alignItems: "flex-start" });
        const ic = iconSvg(b.icon || "check", 20, b.color || "var(--d-accent)"); ic.style.marginTop = "2px";
        r.append(ic, node("span", { overflowWrap: "anywhere" }, line));
        wrap.append(r);
      }
      return wrap;
    }
    case "stat": {
      const s2 = node("div", { textAlign: b.align });
      s2.append(node("div", { fontSize: "34px", fontWeight: 800, lineHeight: "1.1", color: b.color || "var(--d-accent)", overflowWrap: "anywhere" }, b.value), node("div", { fontSize: "14px", opacity: ".75" }, b.label));
      return s2;
    }
    case "rating": {
      const r = node("div", { display: "flex", flexDirection: "column", alignItems: FLEX[b.align], gap: "4px" });
      const stars = node("div", { display: "flex", gap: "2px", color: "var(--d-accent)" });
      for (let i = 1; i <= 5; i++) { const st = iconSvg("star", 22); st.style.fill = i <= b.value ? "currentColor" : "none"; stars.append(st); }
      stars.setAttribute("role", "img"); stars.setAttribute("aria-label", `${b.value} out of 5 stars`);
      r.append(stars); if (b.label) r.append(node("div", { fontSize: "14px", opacity: ".75" }, b.label));
      return r;
    }
    case "progress": {
      const p = node("div", { display: "flex", flexDirection: "column", gap: "6px" });
      if (b.label) p.append(node("div", { display: "flex", justifyContent: "space-between", fontSize: "14px" }, ...[node("span", {}, b.label), node("span", { fontWeight: 600 }, `${b.value}%`)]));
      const track = node("div", { height: "10px", borderRadius: "999px", background: "var(--d-surface)", overflow: "hidden" });
      track.append(node("div", { width: `${b.value}%`, height: "100%", background: b.color || "var(--d-accent)", borderRadius: "999px" }));
      p.append(track);
      return p;
    }
    case "form": {
      const box = node("div", { background: "var(--d-surface)", borderRadius: "var(--d-radius)", padding: "16px", display: "flex", flexDirection: "column", gap: "10px" });
      if (b.title) box.append(node("div", { fontSize: "18px", fontWeight: 700 }, b.title));
      box.append(formEl({ labels: b.fields, big: b.big_last, button: b.button, success: b.success, ctx, send: (data) => ctx.submit?.(b.collection, data) }));
      return box;
    }
    case "spacer":
      return node("div", { height: `${b.height}px` });
    case "divider":
      return node("div", { height: "1px", background: b.color || "rgba(128,128,128,.35)" });
    default:
      return node("div");
  }
}

/** "question :: answer" lines as [question, answer] pairs. */
function pairs(text) {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const i = l.indexOf("::"); return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 2).trim()]; });
}

/** A small form. `labels` is a text with one question per line ("Name*" is required). `send(data)` returns a promise; without ctx.submit (the editor) nothing is sent. */
function formEl({ labels, big, button, success, ctx, send, extra = {} }) {
  const lines = (Array.isArray(labels) ? labels : labels.split("\n")).map((l) => l.trim()).filter(Boolean).slice(0, 8);
  const form = node("form", { display: "flex", flexDirection: "column", gap: "10px" });
  form.noValidate = false;
  const inputs = lines.map((raw, i) => {
    const req = raw.endsWith("*"), label = raw.replace(/\*$/, "").trim().slice(0, 60);
    const long = big && i === lines.length - 1 && lines.length > 1;
    const f = node(long ? "textarea" : "input", { font: "inherit", padding: "10px 12px", borderRadius: "var(--d-radius)", border: "1px solid rgba(128,128,128,.45)", background: "var(--d-bg)", color: "var(--d-text)", width: "100%", boxSizing: "border-box", minHeight: "44px" });
    if (long) f.rows = 4; else f.type = /phone|mobile|whatsapp/i.test(label) ? "tel" : /e-?mail/i.test(label) ? "email" : "text";
    f.required = req; f.maxLength = long ? 600 : 120; f.placeholder = label + (req ? " *" : ""); f.setAttribute("aria-label", label);
    form.append(f);
    return [label, f];
  });
  const msg = node("div", { fontSize: "14px", minHeight: "0" });
  const btn = node("button", { font: "inherit", fontWeight: 600, padding: "12px 20px", minHeight: "44px", borderRadius: "var(--d-radius)", border: "0", cursor: "pointer", background: "var(--d-accent)", color: "#fff" }, button || "Send");
  btn.type = "submit";
  form.append(btn, msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!ctx.submit) { msg.textContent = "This is a preview. In the app, this is sent to your inbox."; return; }
    const data = { ...extra }; for (const [label, f] of inputs) if (f.value.trim()) data[label] = f.value.trim();
    btn.disabled = true; msg.textContent = "Sending…";
    try { await send(data); form.replaceChildren(node("div", { fontWeight: 600, padding: "8px 0" }, success || "Thank you!")); }
    catch (err) { btn.disabled = false; msg.textContent = err.message || "Could not send. Please try again."; msg.style.color = "#c92a2a"; }
  });
  return form;
}

/** The details of one item, as a sheet over the page: picture, name, price, every field, and the order form when the block asks for it. */
function openDetail(it, b, ctx, fields) {
  const host = ctx.root || document.body;
  const overlay = node("div", { position: "fixed", inset: "0", background: "rgba(0,0,0,.55)", zIndex: "1000", display: "flex", alignItems: "flex-end", justifyContent: "center" });
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const sheet = node("div", { background: "var(--d-bg)", color: "var(--d-text)", width: "100%", maxWidth: "560px", maxHeight: "92vh", overflowY: "auto", borderRadius: "var(--d-radius) var(--d-radius) 0 0", padding: "0 0 20px", position: "relative", boxSizing: "border-box" });
  sheet.setAttribute("role", "dialog"); sheet.setAttribute("aria-modal", "true"); sheet.setAttribute("aria-label", it.heading || "Details");
  if (it.image_url) { const img = node("img", { width: "100%", maxHeight: "45vh", objectFit: "cover", display: "block" }); img.alt = ""; img.referrerPolicy = "no-referrer"; img.src = it.image_url; img.addEventListener("error", () => img.remove()); sheet.append(img); }
  const x = node("button", { position: "absolute", top: "10px", right: "10px", width: "40px", height: "40px", borderRadius: "50%", border: "0", background: "rgba(0,0,0,.55)", color: "#fff", fontSize: "22px", cursor: "pointer", lineHeight: "1" }, "×");
  x.setAttribute("aria-label", "Close"); x.addEventListener("click", close);
  const body = node("div", { padding: "16px 18px 0", display: "flex", flexDirection: "column", gap: "10px" });
  body.append(node("div", { fontSize: "22px", fontWeight: 800, overflowWrap: "anywhere" }, it.heading || ""));
  if (typeof it.price === "number") body.append(node("div", { fontSize: "20px", fontWeight: 700, color: "var(--d-accent)" }, String(it.price)));
  if (it.description) body.append(node("div", { whiteSpace: "pre-wrap", overflowWrap: "anywhere", lineHeight: "1.5" }, it.description));
  for (const f of fields || []) {
    if (["image", "price", "description"].includes(f.type) || f.key === "price" || f.key === "description" || f.label === it.heading) continue;
    let v = it[f.key]; if (v === undefined || v === null || v === "" || (f.type === "text" && v === it.heading)) continue;
    if (f.type === "yesno") v = v ? "Yes" : "No";
    const row = node("div", { display: "flex", justifyContent: "space-between", gap: "12px", borderTop: "1px solid rgba(128,128,128,.25)", paddingTop: "8px" });
    const val = f.type === "link" && /^https:\/\//.test(String(v)) ? node("a", { color: "var(--d-accent)", overflowWrap: "anywhere", textAlign: "right" }, String(v)) : node("span", { fontWeight: 600, textAlign: "right", overflowWrap: "anywhere" }, String(v));
    if (val.tagName === "A") { val.href = String(v); val.target = "_blank"; val.rel = "noopener noreferrer"; }
    row.append(node("span", { opacity: ".7" }, f.label), val);
    body.append(row);
  }
  if (b.order) {
    const slot = node("div", { marginTop: "8px" });
    const open = node("button", { font: "inherit", fontWeight: 600, padding: "12px 20px", minHeight: "44px", borderRadius: "var(--d-radius)", border: "0", cursor: "pointer", background: "var(--d-accent)", color: "#fff", width: "100%" }, b.order_label || "Order this");
    open.addEventListener("click", () => {
      open.remove();
      slot.append(formEl({ labels: ["Your name*", "Phone*", "Note"], big: true, button: "Send", success: "Thank you! We will contact you soon.", ctx,
        extra: { Item: it.heading || "", ...(typeof it.price === "number" ? { Price: it.price } : {}) }, send: (data) => ctx.submit(b.order_collection || "orders", data) }));
      slot.querySelector("input")?.focus();
    });
    slot.append(open); body.append(slot);
  }
  sheet.append(x, body); overlay.append(sheet);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
  host.append(overlay);
  x.focus();
}

function itemCard(it, b) {
  const grid = b.layout === "grid";
  const card = node("div", { background: "var(--d-surface)", borderRadius: "var(--d-radius)", overflow: "hidden", display: "flex", flexDirection: grid ? "column" : "row", minWidth: "0" });
  if (b.show_image && it.image_url) {
    const img = node("img", grid ? { width: "100%", aspectRatio: "4 / 3", objectFit: "cover", display: "block" } : { width: "88px", height: "88px", objectFit: "cover", flex: "none" });
    img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.src = it.image_url;
    img.addEventListener("error", () => img.remove());
    card.append(img);
  }
  const info = node("div", { padding: "10px 12px", display: "flex", flexDirection: "column", gap: "4px", minWidth: "0", flex: "1" });
  info.append(node("div", { fontWeight: 700, overflowWrap: "anywhere" }, it.heading || it.title || ""));
  if (typeof it.price === "number") info.append(node("div", { color: "var(--d-accent)", fontWeight: 700 }, String(it.price)));
  const more = b.show_details ? (it.description || "") : "";
  if (more) info.append(node("div", { fontSize: "13px", opacity: ".75", overflowWrap: "anywhere", display: "-webkit-box", WebkitLineClamp: "3", WebkitBoxOrient: "vertical", overflow: "hidden" }, more));
  if (it.in_stock === false) info.append(node("div", { fontSize: "12px", fontWeight: 600, opacity: ".7" }, "Out of stock"));
  card.append(info);
  return card;
}

function openable(card, it, b, ctx, fields) {
  if (!b.detail || !ctx.items || ctx.editable) return;
  card.style.cursor = "pointer"; card.tabIndex = 0; card.setAttribute("role", "button");
  const go = () => openDetail(it, b, ctx, fields);
  card.addEventListener("click", go);
  card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
}

/** Items the owner manages (products, menu, services...). The viewer supplies ctx.items(section); the editor shows samples. */
function itemsBlock(b, ctx) {
  const cols = Math.max(1, Math.min(4, b.columns || 2));
  const wrap = node("div", b.layout === "grid"
    ? { display: "grid", gap: "12px", gridTemplateColumns: `repeat(auto-fit, minmax(max(140px, calc(${100 / cols}% - 12px)), 1fr))` }
    : { display: "flex", flexDirection: "column", gap: "10px" });
  const fill = (list, fields) => {
    wrap.replaceChildren();
    if (!list.length) { wrap.style.display = "block"; wrap.append(node("div", { opacity: ".65", textAlign: "center", padding: "16px 0" }, b.empty)); return; }
    for (const it of list.slice(0, b.limit)) { const card = itemCard(it, b); openable(card, it, b, ctx, fields); wrap.append(card); }
  };
  if (!ctx.items) fill(SAMPLE_ITEMS);
  else {
    wrap.append(node("div", { opacity: ".6" }, "Loading…"));
    ctx.items(b.collection).then((d) => fill(d.items, d.fields), () => { wrap.style.display = "block"; wrap.replaceChildren(node("div", { opacity: ".65", textAlign: "center" }, "Could not load. Pull down to try again.")); });
  }
  return wrap;
}

/** One block as a DOM element. The editor passes ctx.wrap to add selection and drag handling around each block. */
export function renderBlock(b, ctx) {
  const n = body(b, ctx);
  return ctx.wrap ? ctx.wrap(b, n) : n;
}

export function renderPage(page, ctx) {
  const root = node("div", { display: "flex", flexDirection: "column", gap: "12px", padding: "16px", minHeight: "100%", boxSizing: "border-box" });
  for (const b of page.blocks) root.append(renderBlock(b, ctx));
  return root;
}

/** A whole app: the current page and, when the design asks for it, a tab bar along the bottom. */
export function renderApp(root, design, ctx) {
  root.replaceChildren();
  applyTheme(root, design.theme);
  Object.assign(root.style, { display: "flex", flexDirection: "column", height: "100%", minHeight: "100vh" });
  const main = node("div", { flex: "1", overflowY: "auto" });
  const bar = node("nav", { display: "flex", borderTop: "1px solid rgba(128,128,128,.3)", background: design.theme.bg, position: "sticky", bottom: "0" });
  root.append(main, ...(design.nav === "tabs" && design.pages.length > 1 ? [bar] : []));
  let current = design.pages[0].id;
  const show = (id) => {
    current = design.pages.some((p) => p.id === id) ? id : design.pages[0].id;
    const frame = node("div", { maxWidth: "760px", margin: "0 auto" });          // phones fill the width; on a computer the page stays readable
    frame.append(renderPage(design.pages.find((p) => p.id === current), { ...ctx, go: show }));
    main.replaceChildren(frame);
    main.scrollTop = 0;
    for (const t of bar.children) {
      const on = t.dataset.page === current;
      Object.assign(t.style, { color: on ? "var(--d-accent)" : "inherit", opacity: on ? "1" : ".65", borderTop: on ? "3px solid var(--d-accent)" : "3px solid transparent" });
    }
    ctx.onPage?.(current);
  };
  for (const p of design.pages) {
    const t = node("button", { flex: "1", font: "inherit", fontWeight: 600, fontSize: "13px", padding: "12px 4px", background: "none", border: "0", color: "inherit", cursor: "pointer" }, p.title);
    t.type = "button"; t.dataset.page = p.id;
    t.addEventListener("click", () => show(p.id));
    bar.append(t);
  }
  show(ctx.start && design.pages.some((p) => p.id === ctx.start) ? ctx.start : current);
  return { go: show };
}
