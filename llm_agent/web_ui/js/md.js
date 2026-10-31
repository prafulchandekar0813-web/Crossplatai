// Small, safe Markdown renderer: builds DOM nodes (text via textContent), so model output can never inject HTML.
import { h, icon, copyText, toast } from "./dom.js";

const SAFE_URL = /^(https?:\/\/|mailto:)/i;
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^)\s]+\))|(\*[^*\n]+?\*)|((?<![\w])_[^_\n]+?_(?![\w]))|(https?:\/\/[^\s<>)]*[^\s<>).,;:!?'"])/g;

export function inline(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const s = m[0];
    if (m[1]) out.push(h("code", null, s.slice(1, -1)));
    else if (m[2]) out.push(h("strong", null, ...inline(s.slice(2, -2))));
    else if (m[3]) {
      const mid = s.indexOf("](");
      out.push(h("a", { href: s.slice(mid + 2, -1), target: "_blank", rel: "noopener noreferrer" }, ...inline(s.slice(1, mid))));
    } else if (m[4] || m[5]) out.push(h("em", null, ...inline(s.slice(1, -1))));
    else if (m[6]) out.push(h("a", { href: s, target: "_blank", rel: "noopener noreferrer" }, s));
    last = m.index + s.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function codeBlock(lang, code) {
  const copy = h("button", { class: "btn quiet small", title: "Copy code", onclick: async () => {
    toast((await copyText(code)) ? "Copied to clipboard" : "Could not copy. Select the text and copy it manually.");
  } }, icon("copy"), "Copy");
  return h("div", { class: "code" }, h("div", { class: "code-head" }, h("span", null, lang || "text"), copy), h("pre", null, h("code", null, code)));
}

const isBlank = (l) => /^\s*$/.test(l);
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const startsBlock = (l) => /^(#{1,6}\s|```|>\s?|(-{3,}|\*{3,}|_{3,})\s*$)/.test(l) || LIST_ITEM.test(l);

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function parseList(lines, i, indent) {
  const ordered = /\d/.test(LIST_ITEM.exec(lines[i])[2]);
  const list = h(ordered ? "ol" : "ul");
  while (i < lines.length) {
    const m = LIST_ITEM.exec(lines[i]);
    if (!m || m[1].length < indent) break;
    if (m[1].length > indent) {  // nested list belongs to the previous item
      const [sub, next] = parseList(lines, i, m[1].length);
      (list.lastElementChild || list.appendChild(h("li"))).append(sub);
      i = next; continue;
    }
    const li = h("li", null, ...inline(m[3]));
    list.append(li);
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !LIST_ITEM.test(lines[i]) && /^\s+\S/.test(lines[i])) {
      li.append(" ", ...inline(lines[i].trim())); i++;   // wrapped continuation line
    }
  }
  return [list, i];
}

export function renderMarkdown(src) {
  const root = h("div", { class: "md" });
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i++; continue; }
    let m = line.match(/^```\s*([\w+#.-]*)\s*$/);
    if (m) {
      const buf = []; i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      root.append(codeBlock(m[1], buf.join("\n")));
      continue;
    }
    m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (m) { root.append(h("h" + Math.min(6, m[1].length + 2), null, ...inline(m[2]))); i++; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { root.append(h("hr")); i++; continue; }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line); i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && !isBlank(lines[i])) rows.push(splitRow(lines[i++]));
      root.append(h("table", null,
        h("thead", null, h("tr", null, ...head.map((c) => h("th", null, ...inline(c))))),
        h("tbody", null, ...rows.map((r) => h("tr", null, ...head.map((_, k) => h("td", null, ...inline(r[k] ?? ""))))))));
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      root.append(h("blockquote", null, renderMarkdown(buf.join("\n"))));
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const [list, next] = parseList(lines, i, LIST_ITEM.exec(line)[1].length);
      root.append(list); i = next; continue;
    }
    const buf = [line]; i++;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) buf.push(lines[i++]);
    const p = h("p");
    buf.forEach((l, k) => { if (k) p.append(" "); p.append(...inline(l.trim())); });
    root.append(p);
  }
  return root;
}
