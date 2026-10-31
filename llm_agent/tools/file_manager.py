"""Per-user sandboxed workspace.

Every path is resolved (symlinks included) and must stay inside the user's root directory. Functions here are
synchronous; async callers run them with asyncio.to_thread.
"""
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

MAX_PATH_CHARS = 400
MAX_LIST_DEPTH = 8


class WorkspaceError(Exception):
    """A problem the caller (and the model) can act on."""


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

    def patch(self, path: str, edits: Any) -> Dict[str, Any]:
        """Apply exact-text replacements to an existing text file. All edits apply or none do."""
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"File not found: {path}. Use write_file to create it.")
        if not isinstance(edits, list) or not edits:
            raise WorkspaceError("edits must be a non-empty list of {old, new} objects")
        try:
            text = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("Only UTF-8 text files can be patched")
        done = 0
        for n, e in enumerate(edits, 1):
            if not isinstance(e, dict) or not isinstance(e.get("old"), str) or not isinstance(e.get("new"), str):
                raise WorkspaceError(f"Edit {n}: each edit needs string fields 'old' and 'new'")
            old, new = e["old"], e["new"]
            if not old:
                raise WorkspaceError(f"Edit {n}: 'old' must not be empty (to add text, include the neighbouring line in 'old')")
            count = text.count(old)
            if count == 0:
                raise WorkspaceError(f"Edit {n}: the text to replace was not found. Copy it exactly (whitespace included) "
                                     "from read_file; nothing was changed.")
            if count > 1 and not e.get("replace_all"):
                raise WorkspaceError(f"Edit {n}: the text appears {count} times. Include more surrounding lines to make it "
                                     "unique, or set replace_all to true; nothing was changed.")
            text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
            done += count if e.get("replace_all") else 1
        res = self.write(path, text)
        res.update(replacements=done, edits=len(edits), created=False)
        return res

    def edit_lines(self, path: str, changes: Any) -> Dict[str, Any]:
        """Replace, insert or delete lines by line number (1-based, as shown by read_file with line_numbers).

        Every change refers to the file as it is NOW; they are applied together, bottom first, so earlier numbers stay valid.
        replace: start_line..end_line (inclusive) become new_text;  delete: new_text empty;
        insert: end_line = start_line - 1 puts new_text before start_line (start_line = last line + 1 appends).
        """
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"File not found: {path}. Use write_file to create it.")
        if not isinstance(changes, list) or not changes:
            raise WorkspaceError("changes must be a non-empty list of {start_line, end_line, new_text} objects")
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
            a, b, new = c.get("start_line"), c.get("end_line", c.get("start_line")), c.get("new_text", "")
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
        res = self.write(path, "".join(lines))
        res.update(changes=len(parsed), total_lines="".join(lines).count("\n") + (0 if not lines or lines[-1].endswith("\n") else 1), created=False)
        return res

    INSERT_WHERE = ("start", "end", "after_line", "before_line", "after_text", "before_text")

    def insert(self, path: str, text: Any, where: str, line: Any = None, anchor: Any = None, paragraph: bool = False) -> Dict[str, Any]:
        """Insert whole lines (or a paragraph) at a position in an existing text file; nothing else is touched."""
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"File not found: {path}. Use write_file to create it.")
        if not isinstance(text, str) or not text.strip("\n"):
            raise WorkspaceError("text must be a non-empty string")
        if where not in self.INSERT_WHERE:
            raise WorkspaceError("where must be one of: " + ", ".join(self.INSERT_WHERE))
        try:
            lines = target.read_bytes().decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise WorkspaceError("Only UTF-8 text files can be edited")
        n = len(lines)
        if where == "start":
            pos = 1
        elif where == "end":
            pos = n + 1
        elif where in ("after_line", "before_line"):
            if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= max(n, 1):
                raise WorkspaceError(f"line must be an integer from 1 to {max(n, 1)} for where={where}")
            pos = line + 1 if where == "after_line" else line
        else:
            if not isinstance(anchor, str) or not anchor.strip():
                raise WorkspaceError(f"anchor (text on the line to insert next to) is required for where={where}")
            hits = [i for i, ln in enumerate(lines) if anchor in ln]
            if not hits:
                raise WorkspaceError("The anchor text was not found in any line. Copy it exactly from read_file.")
            if len(hits) > 1:
                raise WorkspaceError(f"The anchor text appears on {len(hits)} lines (e.g. lines {', '.join(str(h + 1) for h in hits[:4])}). "
                                     "Use a longer anchor, or where=after_line/before_line with a line number.")
            pos = hits[0] + 2 if where == "after_text" else hits[0] + 1
        new = text
        if paragraph:  # keep one blank line around the new block, without doubling existing blank lines
            new = text.strip("\n")
            if pos > 1 and lines[pos - 2].strip():
                new = "\n" + new
            if pos <= n and lines[pos - 1].strip():
                new = new + "\n"
        res = self.edit_lines(path, [{"start_line": pos, "end_line": pos - 1, "new_text": new}])
        res["inserted_at_line"] = pos + (1 if paragraph and new.startswith("\n") else 0)
        return res

    def read(self, path: str, offset: int = 0, max_chars: int = 200_000, numbered: bool = False) -> Dict[str, Any]:
        target = self.resolve(path)
        if not target.exists():
            raise WorkspaceError(f"File not found: {path}")
        if target.is_dir():
            raise WorkspaceError(f"'{path}' is a directory; use list_files")
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
