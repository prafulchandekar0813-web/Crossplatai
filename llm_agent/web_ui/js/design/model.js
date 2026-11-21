// Editing a design: small pure functions on the design object (no DOM), so they can be tested on their own.

export const clone = (x) => JSON.parse(JSON.stringify(x));

/** Every block of a page, depth first. */
export function* walk(blocks) {
  for (const b of blocks) { yield b; if (b.children) yield* walk(b.children); }
}

export function allIds(design) {
  const ids = new Set(design.pages.map((p) => p.id));
  for (const p of design.pages) for (const b of walk(p.blocks)) ids.add(b.id);
  return ids;
}

/** A short id nobody in this design uses yet. */
export function freshId(design, prefix = "b") {
  const used = allIds(design);
  let n = used.size + 1;
  while (used.has(prefix + n)) n += 1;
  return prefix + n;
}

/** A new block with every field at its default, taken from the definitions the server sent. */
export function newBlock(design, type, spec) {
  const def = spec.blocks[type];
  const block = { id: freshId(design), type };
  for (const f of def.fields) block[f.key] = clone(f.default);
  if (def.container) block.children = [];
  return block;
}

/** Where a block sits: the list holding it, its place in that list, and its parent block (null at the top of the page). */
export function locate(page, id, list = page.blocks, parent = null) {
  for (let i = 0; i < list.length; i++) {
    if (list[i].id === id) return { list, index: i, parent };
    if (list[i].children) { const hit = locate(page, id, list[i].children, list[i]); if (hit) return hit; }
  }
  return null;
}

export const find = (page, id) => { const at = locate(page, id); return at ? at.list[at.index] : null; };

export function remove(page, id) {
  const at = locate(page, id);
  if (!at) return null;
  return at.list.splice(at.index, 1)[0];
}

/** Put a block into a list. Returns false (and changes nothing) when a box would end up inside itself. */
export function insert(list, index, block) {
  list.splice(Math.max(0, Math.min(index, list.length)), 0, block);
  return true;
}

/** Move an existing block to `index` of `list`, which belongs to the page. A box cannot be moved into itself. */
export function move(page, id, list, index) {
  const at = locate(page, id);
  if (!at) return false;
  const block = at.list[at.index];
  if (block.children && [...walk([block])].some((b) => b.children === list)) return false;
  at.list.splice(at.index, 1);
  const target = at.list === list && at.index < index ? index - 1 : index;   // the list shrank by one before the block was put back
  insert(list, target, block);
  return true;
}

export function duplicate(design, page, id) {
  const at = locate(page, id);
  if (!at) return null;
  const copy = clone(at.list[at.index]);
  // the copy and everything inside it get ids nobody uses yet
  const used = allIds(design);
  for (const b of walk([copy])) { let n = used.size + 1; while (used.has("b" + n)) n += 1; b.id = "b" + n; used.add(b.id); }
  at.list.splice(at.index + 1, 0, copy);
  return copy;
}

/** Buttons that pointed at a page that is gone stop pointing anywhere. */
export function dropActionsTo(design, pageId) {
  for (const p of design.pages) for (const b of walk(p.blocks)) if (b.action?.type === "page" && b.action.target === pageId) b.action = { type: "none" };
}

/** A copy of a layout template with an id nobody uses on every block in it. */
export function instantiate(design, template) {
  const used = allIds(design);
  const fresh = () => { let n = used.size + 1; while (used.has("b" + n)) n += 1; used.add("b" + n); return "b" + n; };
  const copy = clone(template);
  for (const b of walk([copy])) b.id = fresh();
  return copy;
}

/** How many boxes are nested inside each other at most (a page's top level is 0). */
export function depth(blocks, d = 0) {
  let deepest = d;
  for (const b of blocks) if (b.children) deepest = Math.max(deepest, depth(b.children, d + 1));
  return deepest;
}

export const count = (design) => design.pages.reduce((n, p) => n + [...walk(p.blocks)].length, 0);

/**
 * Put a block next to another one, in one line. `moving` is an existing block's id or a new block object.
 * If the other block already sits in a row, the block joins that row; otherwise both go into a new row.
 * `group` is a ready-made row (from newBlock) used only when a new row is needed. Returns false when it cannot be done.
 */
export function placeBeside(page, moving, targetId, side, group) {
  const block = typeof moving === "string" ? find(page, moving) : moving;
  const target = find(page, targetId);
  if (!block || !target || block === target) return false;
  if (block.children && [...walk([block])].some((b) => b.id === targetId)) return false;   // a box cannot go beside its own content
  if (typeof moving === "string") remove(page, moving);
  const at = locate(page, targetId);
  if (at.parent?.direction === "row") {
    insert(at.list, side === "left" ? at.index : at.index + 1, block);
    return true;
  }
  group.direction = "row";
  group.children = side === "left" ? [block, target] : [target, block];
  at.list[at.index] = group;
  return true;
}

/** Take a block out of the row it is in and put it right after that row. */
export function outOfRow(page, id) {
  const at = locate(page, id);
  if (!at?.parent || at.parent.direction !== "row") return false;
  const parentAt = locate(page, at.parent.id);
  const [block] = at.list.splice(at.index, 1);
  parentAt.list.splice(parentAt.index + 1, 0, block);
  return true;
}
