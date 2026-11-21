import test from "node:test";
import assert from "node:assert/strict";
import { newBlock, locate, remove, move, duplicate, dropActionsTo, walk, allIds } from "../../llm_agent/web_ui/js/design/model.js";

const spec = { blocks: {
  text: { container: false, fields: [{ key: "text", default: "Hello" }, { key: "size", default: "m" }] },
  button: { container: false, fields: [{ key: "action", default: { type: "none" } }] },
  group: { container: true, fields: [{ key: "gap", default: 12 }] } } };
const design = () => ({ theme: {}, pages: [{ id: "home", title: "Home", blocks: [] }, { id: "two", title: "Two", blocks: [] }] });

test("new blocks take the server's defaults, with an id nobody has", () => {
  const d = design();
  const a = newBlock(d, "text", spec); d.pages[0].blocks.push(a);
  const b = newBlock(d, "group", spec); d.pages[0].blocks.push(b);
  assert.equal(a.text, "Hello"); assert.deepEqual(b.children, []); assert.notEqual(a.id, b.id);
  const act = newBlock(d, "button", spec); act.action.type = "page";
  assert.equal(newBlock(d, "button", spec).action.type, "none");   // defaults are copies, not shared
});

test("move reorders, moves into a box, and refuses a box inside itself", () => {
  const d = design(); const page = d.pages[0];
  const [a, g, c] = ["text", "group", "text"].map((t) => { const b = newBlock(d, t, spec); page.blocks.push(b); return b; });
  assert.ok(move(page, a.id, page.blocks, 3));                      // to the end
  assert.deepEqual(page.blocks.map((b) => b.id), [g.id, c.id, a.id]);
  assert.ok(move(page, c.id, g.children, 0));                       // into the box
  assert.equal(locate(page, c.id).parent.id, g.id);
  assert.equal(move(page, g.id, g.children, 0), false);             // a box into itself
  assert.ok(move(page, a.id, page.blocks, 0));                      // to the top
  assert.equal(page.blocks[0].id, a.id);
});

test("duplicate gives the copy and everything inside it new ids", () => {
  const d = design(); const page = d.pages[0];
  const g = newBlock(d, "group", spec); page.blocks.push(g);
  const t = newBlock(d, "text", spec); g.children.push(t);
  const copy = duplicate(d, page, g.id);
  const ids = [...walk(page.blocks)].map((b) => b.id);
  assert.equal(new Set(ids).size, ids.length); assert.equal(ids.length, 4); assert.equal(page.blocks[1].id, copy.id);
});

test("removing a page's block and dropping actions that pointed at a deleted page", () => {
  const d = design(); const page = d.pages[0];
  const b = newBlock(d, "button", spec); b.action = { type: "page", target: "two" }; page.blocks.push(b);
  dropActionsTo(d, "two");
  assert.deepEqual(b.action, { type: "none" });
  assert.equal(remove(page, b.id).id, b.id); assert.equal(remove(page, "nope"), null);
  assert.ok(allIds(d).has("home"));
});

import { placeBeside, outOfRow, instantiate, depth } from "../../llm_agent/web_ui/js/design/model.js";

test("placing blocks side by side makes a row, joins an existing row, and can leave it again", () => {
  const spec2 = { blocks: { ...spec.blocks, group: { container: true, fields: [{ key: "direction", default: "column" }] } } };
  const d = design(); const page = d.pages[0];
  const [a, b, c] = ["text", "text", "text"].map((t) => { const x = newBlock(d, t, spec2); page.blocks.push(x); return x; });
  assert.ok(placeBeside(page, a.id, b.id, "left", newBlock(d, "group", spec2)));
  assert.equal(page.blocks.length, 2); assert.equal(page.blocks[0].direction, "row");
  assert.deepEqual(page.blocks[0].children.map((x) => x.id), [a.id, b.id]);
  assert.ok(placeBeside(page, c.id, b.id, "right", null));           // joins the existing row, no new group needed
  assert.deepEqual(page.blocks[0].children.map((x) => x.id), [a.id, b.id, c.id]); assert.equal(page.blocks.length, 1);
  assert.ok(outOfRow(page, a.id));
  assert.deepEqual(page.blocks.map((x) => x.id).length, 2); assert.equal(page.blocks[1].id, a.id);
  assert.equal(placeBeside(page, page.blocks[0].id, b.id, "left", null), false);   // a box beside its own content
});

test("layouts get new ids everywhere, and depth counts nested boxes", () => {
  const d = design(); const existing = newBlock(d, "text", spec); d.pages[0].blocks.push(existing);
  const copy = instantiate(d, { id: "x", type: "group", children: [{ id: "x", type: "group", children: [{ id: "x", type: "text" }] }] });
  const ids = [...walk([copy])].map((x) => x.id);
  assert.equal(new Set([...ids, existing.id]).size, 4); assert.equal(depth([copy]), 2);
});
