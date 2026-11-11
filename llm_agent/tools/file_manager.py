"""Per-user sandboxed workspace.

Every path is resolved (symlinks included) and must stay inside the user's root directory. Functions here are
synchronous; async callers run them with asyncio.to_thread.
"""
import difflib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Tuple, Any, Dict, List

MAX_PATH_CHARS = 400
MAX_LIST_DEPTH = 8


class WorkspaceError(Exception):
    """A problem the caller (and the model) can act on."""


ALIASES = {"old": ("old", "old_text", "old_string", "oldText", "oldString", "search", "find", "from"),
           "new": ("new", "new_text", "new_string", "newText", "newString", "replace", "replacement", "to"),
           "start_line": ("start_line", "start", "line", "from_line", "startLine"),
           "end_line": ("end_line", "end", "to_line", "endLine"),
           "new_text": ("new_text", "text", "new", "content", "newText", "replacement")}


def _items(value: Any, what: str) -> list:
    """The list a model meant: also accepts a JSON string of a list, or a single object instead of a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise WorkspaceError(f"{what} must be a JSON list of objects, not text")
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise WorkspaceError(f"{what} must be a non-empty list of objects")
    return value


def _field(obj: dict, name: str, default: Any = None) -> Any:
    for key in ALIASES[name]:
        if key in obj:
            return obj[key]
    return default


def _int(value: Any) -> Any:
    """Whole numbers sent as "12" or 12.0 are accepted; anything else is returned unchanged for the caller to reject."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return value


class WorkspaceFiles:
    def __init__(self, root: str, max_file_bytes: int = 1024 * 1024, quota_bytes: int = 50 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.quota_bytes = quota_bytes

    # ---- path safety -----------------------------------------------------------------------------
    def resolve(self, rel: Any, allow_root: bool = False) -> Path:
        if not isinstance(rel, str):
            raise WorkspaceError("path must be a string")
        rel = rel.replace("\\", "/").strip()
        if "\x00" in rel or len(rel) > MAX_PATH_CHARS:
            raise WorkspaceError("invalid path")
        parts = [p for p in rel.split("/") if p not in ("", ".")]  # a leading "/" simply means "workspace root"
        if any(p == ".." for p in parts):
            raise WorkspaceError("Path may not contain '..'; use paths relative to the workspace root")
        target = self.root.joinpath(*parts).resolve() if parts else self.root
        if target != self.root and not target.is_relative_to(self.root):
            raise WorkspaceError("Access denied: path is outside the workspace")
        if target == self.root and not allow_root:
            raise WorkspaceError("A file path is required")
        return target

    def _all_files(self, limit: int = 5000) -> List[str]:
        out: List[str] = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", "venv", ".venv")]
            for f in files:
                out.append(Path(dirpath, f).relative_to(self.root).as_posix())
                if len(out) >= limit:
                    return out
        return out

    def existing(self, rel: Any) -> Path:
        """Resolve a path that must already exist. Models often get the folder slightly wrong ("src/app.py" for "app.py",
        a leading "workspace/"): when exactly one file matches by its ending, use it; otherwise say which files are close."""
        target = self.resolve(rel)
        if target.is_file():
            return target
        want = target.relative_to(self.root).as_posix() if target != self.root else ""
        names = self._all_files()
        tail = want
        for prefix in ("workspace/", "app/", "project/"):
            if tail.startswith(prefix):
                tail = tail[len(prefix):]
        hits = [n for n in names if n == tail or n.endswith("/" + tail)]
        if len(hits) == 1:
            return self.root / hits[0]
        if target.is_dir():
            raise WorkspaceError(f"'{rel}' is a folder, not a file. Use list_files to see what is inside.")
        leaf = tail.rsplit("/", 1)[-1]
        close = hits[:6]
        if not close:
            close = difflib.get_close_matches(want, names, n=5, cutoff=0.5)
        if not close:
            like = difflib.get_close_matches(leaf, [n.rsplit("/", 1)[-1] for n in names], n=4, cutoff=0.6)
            close = [n for n in names if n.rsplit("/", 1)[-1] in like][:5]
        more = (" Did you mean: " + ", ".join(close) + "?") if close else (" The workspace has: " + ", ".join(names[:12]) + ("…" if len(names) > 12 else "")) if names else " The workspace is empty."
        raise WorkspaceError(f"File not found: {rel}.{more} Use list_files to check the exact path, or write_file to create it.")

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else ""

    # ---- operations ------------------------------------------------------------------------------
    def usage_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        return total

    def write(self, path: str, content: Any) -> Dict[str, Any]:
        if not isinstance(content, str):
            raise WorkspaceError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise WorkspaceError(
                f"File is {len(data)} bytes; the limit is {self.max_file_bytes}. Split it into smaller files.")
        target = self.resolve(path)
        if target.is_dir():
            raise WorkspaceError(f"'{path}' is a directory")
        existed = target.exists()
        existing = target.stat().st_size if existed else 0
        if self.usage_bytes() - existing + len(data) > self.quota_bytes:
            raise WorkspaceError(f"Workspace quota of {self.quota_bytes // (1024 * 1024)} MB would be exceeded")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.parent.resolve().is_relative_to(self.root):  # re-check after creating directories
                raise WorkspaceError("Access denied: path is outside the workspace")
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{uuid.uuid4().hex[:8]}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise WorkspaceError(f"Could not write file: {e.strerror or e}")
        return {"status": "success", "path": self.rel(target), "created": not existed, "bytes": len(data),
                "overwritten": existed}

    @staticmethod
    def _find(text: str, old: str) -> List[Tuple[int, int, bool]]:
        """Where `old` occurs in `text`: [(start, end, loose)]. Exact matches first; if none, lines that match when
        trailing spaces (then indentation too) are ignored, so a multi-line block still lands after small whitespace slips."""
        out, i = [], text.find(old)
        while i != -1:
            out.append((i, i + len(old), False))
            i = text.find(old, i + len(old))
        if out:
            return out
        want = old.replace("\r\n", "\n").strip("\n").split("\n")
        if not any(w.strip() for w in want):
            return []
        lines = text.splitlines(keepends=True)
        offsets = [0]
        for ln in lines:
            offsets.append(offsets[-1] + len(ln))
        for norm in (lambda x: x.rstrip(), lambda x: x.strip()):
            target = [norm(w) for w in want]
            for k in range(len(lines) - len(want) + 1):
                if all(norm(lines[k + j].rstrip("\r\n")) == target[j] for j in range(len(want))):
                    end = offsets[k + len(want)]
                    out.append((offsets[k], end, True))
            if out:
                return out
        return []

    @staticmethod
    def _hint(text: str, old: str) -> str:
        first = next((l.strip() for l in old.splitlines() if l.strip()), "")
        if not first:
            return ""
        lines = text.splitlines()
        close = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.6)
        if not close:
            return ""
        n = next(i for i, l in enumerate(lines, 1) if l.strip() == close[0])
        ctx = "\n".join(f"{i}| {lines[i - 1][:160]}" for i in range(max(1, n - 1), min(len(lines), n + 2) + 1))
        return f" The closest match is at line {n}; the file actually says:\n{ctx}\nUse read_file with line_numbers=true on that area and copy the text exactly, or use edit_lines with those line numbers."

    def _apply_edits(self, text: str, edits: Any, label: str = "") -> Tuple[str, int, List[str]]:
        edits = _items(edits, "edits")
        done, notes = 0, []
        for n, e in enumerate(edits, 1):
            where = f"{label}Edit {n}"
            if not isinstance(e, dict):
                raise WorkspaceError(f"{where}: each edit must be an object with 'old' and 'new'")
            old, new = _field(e, "old"), _field(e, "new")
            if not isinstance(old, str) or not isinstance(new, str):
                raise WorkspaceError(f"{where}: each edit needs string fields 'old' and 'new' (use an empty string for 'new' to delete)")
            if not old.strip("\r\n"):
                raise WorkspaceError(f"{where}: 'old' must not be empty (to add text, include the neighbouring line in 'old')")
            if old not in text and "\r\n" in text and "\r\n" not in old:      # the file uses Windows line endings, the model sent plain ones
                old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            hits = self._find(text, old)
            if not hits:
                raise WorkspaceError(f"{where}: the text to replace was not found.{self._hint(text, old)} "
                                     "Copy it exactly from read_file; nothing was changed.")
            replace_all = bool(e.get("replace_all"))
            if len(hits) > 1 and not replace_all:
                raise WorkspaceError(f"{where}: the text appears {len(hits)} times. Include more surrounding lines to make it "
                                     "unique, or set replace_all to true; nothing was changed.")
            for start, end, loose in (hits if replace_all else hits[:1])[::-1]:
                repl = new
                if loose:
                    repl = self._reindent(text[start:end], old, new)
                    if text[start:end].endswith(("\n", "\r\n")) and not repl.endswith("\n"):
                        repl += "\r\n" if "\r\n" in text else "\n"
                    notes.append(f"{where} matched ignoring whitespace differences")
                text = text[:start] + repl + text[end:]
            done += len(hits) if replace_all else 1
        return text, done, notes

    @staticmethod
    def _reindent(found: str, old: str, new: str) -> str:
        """The file's indentation differs from the model's: shift the replacement by the same amount."""
        f = next((l for l in found.splitlines() if l.strip()), "")
        o = next((l for l in old.splitlines() if l.strip()), "")
        fi, oi = f[:len(f) - len(f.lstrip())], o[:len(o) - len(o.lstrip())]
        if fi == oi or not fi.startswith(oi):
            return new
        extra = fi[len(oi):]
        return "".join((extra + l if l.strip() else l) for l in new.splitlines(keepends=True))

    def _read_text(self, path: str, verb: str) -> Tuple[Path, str]:
        target = self.existing(path)
        try:
            return target, target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError(f"Only UTF-8 text files can be {verb}")

    def patch(self, path: str, edits: Any) -> Dict[str, Any]:
        """Apply exact-text replacements to an existing text file. All edits apply or none do."""
        target, text = self._read_text(path, "patched")
        text, done, notes = self._apply_edits(text, edits)
        res = self.write(self.rel(target), text)
        res.update(replacements=done, edits=len(_items(edits, "edits")), created=False)
        if notes:
            res["notes"] = notes
        return res

    MAX_BATCH = 20

    def patch_many(self, files: Any) -> Dict[str, Any]:
        """Patch several files in one call: [{path, edits}]. Every file is checked before any is written, so a mistake changes nothing."""
        files = _items(files, "files")
        if len(files) > self.MAX_BATCH:
            raise WorkspaceError(f"At most {self.MAX_BATCH} files per call")
        planned: Dict[str, Tuple[str, int, List[str]]] = {}
        for n, f in enumerate(files, 1):
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                raise WorkspaceError(f"File {n}: each entry needs a 'path' and a list of 'edits'")
            key = self.rel(self.existing(f["path"]))
            current = planned[key][0] if key in planned else self._read_text(f["path"], "patched")[1]
            text, done, notes = self._apply_edits(current, f.get("edits"), label=f"{f['path']}: ")
            prev = planned.get(key, ("", 0, []))
            planned[key] = (text, prev[1] + done, prev[2] + notes)
        out = [self.write(k, v[0]) | {"replacements": v[1], "created": False} | ({"notes": v[2]} if v[2] else {}) for k, v in planned.items()]
        return {"status": "success", "files": out, "replacements": sum(r["replacements"] for r in out)}

    def write_many(self, files: Any) -> Dict[str, Any]:
        """Create or overwrite several files in one call: [{path, content}]. Sizes and quota are checked for all before any is written."""
        files = _items(files, "files")
        if len(files) > self.MAX_BATCH:
            raise WorkspaceError(f"At most {self.MAX_BATCH} files per call")
        total, seen = 0, set()
        for n, f in enumerate(files, 1):
            if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
                raise WorkspaceError(f"File {n}: each entry needs a 'path' and string 'content'")
            target = self.resolve(f["path"])
            if target.is_dir():
                raise WorkspaceError(f"'{f['path']}' is a directory")
            size = len(f["content"].encode("utf-8"))
            if size > self.max_file_bytes:
                raise WorkspaceError(f"{f['path']} is {size} bytes; the limit is {self.max_file_bytes}. Split it into smaller files.")
            if target in seen:
                raise WorkspaceError(f"{f['path']} appears twice in this call")
            seen.add(target)
            total += size - (target.stat().st_size if target.exists() else 0)
        if self.usage_bytes() + total > self.quota_bytes:
            raise WorkspaceError(f"Workspace quota of {self.quota_bytes // (1024 * 1024)} MB would be exceeded")
        out = [self.write(f["path"], f["content"]) for f in files]
        return {"status": "success", "files": out, "bytes": sum(r["bytes"] for r in out)}

    def edit_lines(self, path: str, changes: Any) -> Dict[str, Any]:
        """Replace, insert or delete lines by line number (1-based, as shown by read_file with line_numbers).

        Every change refers to the file as it is NOW; they are applied together, bottom first, so earlier numbers stay valid.
        replace: start_line..end_line (inclusive) become new_text;  delete: new_text empty;
        insert: end_line = start_line - 1 puts new_text before start_line (start_line = last line + 1 appends).
        """
        target = self.existing(path)
        changes = _items(changes, "changes")
        try:
            text = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("Only UTF-8 text files can be edited")
        crlf = "\r\n" in text
        lines = text.splitlines(keepends=True)
        n = len(lines)
        parsed = []
        for i, c in enumerate(changes, 1):
            if not isinstance(c, dict):
                raise WorkspaceError(f"Change {i}: must be an object")
            a = _int(_field(c, "start_line"))
            b = _int(_field(c, "end_line", a))
            new = _field(c, "new_text", "")
            new = "" if new is None else new
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b)) or not isinstance(new, str):
                raise WorkspaceError(f"Change {i}: start_line and end_line must be integers and new_text a string")
            if not (1 <= a <= n + 1) or not (a - 1 <= b <= n):
                raise WorkspaceError(f"Change {i}: lines {a}-{b} are outside the file, which has {n} line{'s' if n != 1 else ''}. "
                                     "Read the file again with line_numbers.")
            parsed.append((a, b, new))
        parsed.sort(key=lambda t: (t[0], t[1]))
        for (a1, b1, _), (a2, b2, _) in zip(parsed, parsed[1:]):
            if a2 <= b1 or (a2 == a1):
                raise WorkspaceError(f"Changes overlap around line {a2}. Merge them into one change.")
        for a, b, new in reversed(parsed):
            block = new.replace("\r\n", "\n").splitlines(keepends=True) if new else []
            if block:
                eol = "\r\n" if crlf else "\n"
                if crlf:
                    block = [ln[:-1] + eol if ln.endswith("\n") else ln for ln in block]
                at_end = b == len(lines)
                if not block[-1].endswith("\n") and not (at_end and lines and not lines[-1].endswith("\n") and a <= b):
                    block[-1] += eol
                if a == len(lines) + 1 and lines and not lines[-1].endswith("\n"):
                    lines[-1] += eol  # appending after a last line that had no newline
            lines[a - 1:b] = block
        res = self.write(self.rel(target), "".join(lines))
        res.update(changes=len(parsed), total_lines="".join(lines).count("\n") + (0 if not lines or lines[-1].endswith("\n") else 1), created=False)
        return res

    INSERT_WHERE = ("start", "end", "after_line", "before_line", "after_text", "before_text")

    def _position(self, lines: List[str], where: Any, line: Any, anchor: Any) -> int:
        n = len(lines)
        line = _int(line)
        if where not in self.INSERT_WHERE:
            raise WorkspaceError("where must be one of: " + ", ".join(self.INSERT_WHERE))
        if where == "start":
            return 1
        if where == "end":
            return n + 1
        if where in ("after_line", "before_line"):
            if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= max(n, 1):
                raise WorkspaceError(f"line must be an integer from 1 to {max(n, 1)} for where={where}")
            return line + 1 if where == "after_line" else line
        if not isinstance(anchor, str) or not anchor.strip():
            raise WorkspaceError(f"anchor (text on the line to insert next to) is required for where={where}")
        hits = [i for i, ln in enumerate(lines) if anchor in ln]
        if not hits:
            raise WorkspaceError("The anchor text was not found in any line. Copy it exactly from read_file.")
        if len(hits) > 1:
            raise WorkspaceError(f"The anchor text appears on {len(hits)} lines (e.g. lines {', '.join(str(h + 1) for h in hits[:4])}). "
                                 "Use a longer anchor, or where=after_line/before_line with a line number.")
        return hits[0] + 2 if where == "after_text" else hits[0] + 1

    def insert_many(self, path: str, inserts: Any) -> Dict[str, Any]:
        """Several insertions into one file in one call. Every position refers to the file as it is now; inserts at the same place keep their order."""
        _, text = self._read_text(path, "edited")
        lines = text.splitlines()
        n = len(lines)
        grouped: Dict[int, List[str]] = {}
        spots = []
        for i, ins in enumerate(_items(inserts, "inserts"), 1):
            if not isinstance(ins, dict):
                raise WorkspaceError(f"Insert {i}: must be an object with text and where")
            body = _field(ins, "new_text") if "text" not in ins else ins["text"]
            if not isinstance(body, str) or not body.strip("\n"):
                raise WorkspaceError(f"Insert {i}: text must be a non-empty string")
            try:
                pos = self._position(lines, ins.get("where"), ins.get("line"), ins.get("anchor"))
            except WorkspaceError as e:
                raise WorkspaceError(f"Insert {i}: {e}")
            new = body
            if ins.get("paragraph"):                 # keep one blank line around the new block, without doubling existing blank lines
                new = body.strip("\n")
                if pos > 1 and lines[pos - 2].strip():
                    new = "\n" + new
                if pos <= n and lines[pos - 1].strip():
                    new = new + "\n"
            grouped.setdefault(pos, []).append(new.rstrip("\n") if len(grouped.get(pos, [])) or True else new)
            spots.append(pos + (1 if new.startswith("\n") else 0))
        changes = [{"start_line": pos, "end_line": pos - 1, "new_text": "\n".join(parts) + "\n"} for pos, parts in grouped.items()]
        res = self.edit_lines(path, changes)
        res["inserted_at_line"] = spots[0]
        res["inserts"] = len(spots)
        return res

    def insert(self, path: str, text: Any, where: str, line: Any = None, anchor: Any = None, paragraph: bool = False) -> Dict[str, Any]:
        """Insert whole lines (or a paragraph) at a position in an existing text file; nothing else is touched."""
        return self.insert_many(path, [{"text": text, "where": where, "line": line, "anchor": anchor, "paragraph": paragraph}])

    def read(self, path: str, offset: int = 0, max_chars: int = 200_000, numbered: bool = False) -> Dict[str, Any]:
        target = self.resolve(path)
        if target.is_dir():
            raise WorkspaceError(f"'{path}' is a directory; use list_files")
        if not target.exists():
            target = self.existing(path)
        try:
            size = target.stat().st_size
            if size > 20 * 1024 * 1024:
                raise WorkspaceError("File is too large to read")
            raw = target.read_bytes()
        except OSError as e:
            raise WorkspaceError(f"Could not read file: {e.strerror or e}")
        if b"\x00" in raw[:4096]:
            return {"status": "success", "path": self.rel(target), "binary": True, "size": size, "content": "",
                    "total_chars": 0, "offset": 0, "next_offset": None}
        text = raw.decode("utf-8", errors="replace")
        offset = max(0, min(int(offset or 0), len(text)))
        chunk = text[offset:offset + max_chars]
        end = offset + len(chunk)
        out = {"status": "success", "path": self.rel(target), "binary": False, "size": size, "content": chunk,
               "total_chars": len(text), "offset": offset, "next_offset": end if end < len(text) else None}
        if numbered:  # "  12| text" -- the numbers are display only and are not part of the file
            first = text.count("\n", 0, offset) + 1
            out["content"] = "\n".join(f"{first + i:>5}| {ln}" for i, ln in enumerate(chunk.split("\n")))
            out["total_lines"] = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        return out

    def list(self, path: str = "", max_entries: int = 300) -> Dict[str, Any]:
        base = self.resolve(path or "", allow_root=True)
        if not base.exists():
            raise WorkspaceError(f"Directory not found: {path or '/'}")
        if not base.is_dir():
            raise WorkspaceError(f"'{path}' is a file, not a directory")
        entries: List[Dict[str, Any]] = []
        truncated = False

        def walk(directory: Path, depth: int) -> None:
            nonlocal truncated
            try:
                children = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError:
                return
            for entry in children:
                if entry.is_symlink() or entry.name.endswith(".tmp") and entry.name.startswith("."):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    return
                p = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    entries.append({"path": self.rel(p), "type": "dir"})
                    if depth < MAX_LIST_DEPTH:
                        walk(p, depth + 1)
                else:
                    st = entry.stat(follow_symlinks=False)
                    entries.append({"path": self.rel(p), "type": "file", "size": st.st_size, "modified": st.st_mtime})
                if truncated:
                    return

        walk(base, 0)
        return {"status": "success", "path": self.rel(base) or "/", "entries": entries, "truncated": truncated}

    def delete(self, path: str) -> Dict[str, Any]:
        target = self.resolve(path)
        if not target.exists() and not target.is_symlink():
            raise WorkspaceError(f"Not found: {path}")
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as e:
            raise WorkspaceError(f"Could not delete: {e.strerror or e}")
        return {"status": "success", "path": path}

    def move(self, src: str, dst: str, overwrite: bool = False) -> Dict[str, Any]:
        """Move or rename a file or folder inside the workspace. If dst is an existing folder, src goes inside it."""
        a = self.resolve(src)
        if not a.exists() and not a.is_symlink():
            a = self.existing(src) if not a.is_dir() else a
        b = self.resolve(dst, allow_root=True)
        if a == self.root:
            raise WorkspaceError("The workspace folder itself cannot be moved")
        if b.is_dir() and not str(dst).strip().endswith(("/", "\\")) or (b == self.root):
            b = b / a.name                                  # moving "into" a folder that exists
        elif str(dst).strip().endswith(("/", "\\")):
            b = b / a.name
        if b == a:
            raise WorkspaceError("The file is already there")
        if a.is_dir() and (b == a or b.is_relative_to(a)):
            raise WorkspaceError("A folder cannot be moved into itself")
        if b.exists() or b.is_symlink():
            if not overwrite:
                raise WorkspaceError(f"{self.rel(b)} already exists. Choose another name, or set overwrite to true to replace it.")
            if b.is_dir():
                raise WorkspaceError("An existing folder is never replaced; move the contents instead")
        was_dir = a.is_dir()
        files = [Path(d, f) for d, _, fs in os.walk(a) for f in fs] if was_dir else [a]
        old = [self.rel(f) for f in files]
        try:
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(a), str(b))
        except OSError as e:
            raise WorkspaceError(f"Could not move: {e.strerror or e}")
        base_a, base_b = self.rel(a), self.rel(b)
        new = [base_b + n[len(base_a):] if was_dir else base_b for n in old]
        return {"status": "success", "from": base_a, "to": base_b, "folder": was_dir, "moved": len(new), "old_paths": old, "new_paths": new}

    def summary_lines(self, limit: int = 40) -> List[str]:
        """Short listing for the system prompt so the model knows what already exists."""
        try:
            listing = self.list("", max_entries=limit + 1)
        except WorkspaceError:
            return []
        lines = [e["path"] + ("/" if e["type"] == "dir" else f" ({e['size']} B)") for e in listing["entries"][:limit]]
        if len(listing["entries"]) > limit or listing["truncated"]:
            lines.append("… (more files; use list_files)")
        return lines
