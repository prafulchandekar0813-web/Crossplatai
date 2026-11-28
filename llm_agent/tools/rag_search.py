"""Offline knowledge base: upload -> text extraction -> chunking -> BM25 ranked search.

Layout (all inside the server's data dir, never visible to agents' file tools):
    knowledge_base/sources/<name>   the uploaded original
    knowledge_base/text/<name>.md   extracted text that is actually searched
"""
import importlib.util
import math
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
import tempfile
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm_agent.security import safe_filename, valid_identifier

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}
BINARY_EXT = {".pdf", ".docx", ".pptx", ".xlsx"}
ALLOWED_EXT = TEXT_EXT | BINARY_EXT
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("a an and are as at be but by for from has have he her his i if in into is it its of on or she so "
                  "than that the their them then there these they this to was we were what when where which who "
                  "will with you your".split())


class KnowledgeError(Exception):
    pass


def _tokens(text: str) -> List[str]:
    out = []
    for w in _TOKEN.findall(text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _office_text(path: Path, ext: str) -> str:
    """Dependency-free text extraction for .docx / .pptx / .xlsx (they are zip files of XML)."""
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def texts(xml: bytes, para: str) -> List[str]:
        out = []
        for p in ET.fromstring(xml).iter():
            if local(p.tag) == para:
                t = "".join((e.text or "") for e in p.iter() if local(e.tag) == "t")
                if t.strip():
                    out.append(t.strip())
        return out

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
            raise KnowledgeError("The file expands to an unreasonable size")
        if ext == ".docx":
            return "\n\n".join(texts(z.read("word/document.xml"), "p"))
        if ext == ".pptx":
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
            return "\n\n".join(f"## Slide {i}\n\n" + "\n".join(texts(z.read(n), "p")) for i, n in enumerate(slides, 1))
        shared = texts(z.read("xl/sharedStrings.xml"), "si") if "xl/sharedStrings.xml" in names else []
        out = []
        for n in sorted(x for x in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x)):
            rows = []
            for row in (e for e in ET.fromstring(z.read(n)).iter() if local(e.tag) == "row"):
                cells = []
                for c in (e for e in row if local(e.tag) == "c"):
                    v = next((e.text for e in c if local(e.tag) == "v"), None)
                    if v is None:
                        v = "".join((e.text or "") for e in c.iter() if local(e.tag) == "t") or None
                    elif c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    if v:
                        cells.append(v)
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                out.append(f"## {Path(n).stem}\n\n" + "\n".join(rows))
        return "\n\n".join(out)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _chunk(text: str, size: int = 900, overlap: int = 150) -> List[Tuple[str, str]]:
    """Split into ~size-character chunks along paragraph boundaries; remember the nearest markdown heading."""
    chunks: List[Tuple[str, str]] = []
    section, buf, buf_section = "", "", ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            section = block.lstrip("#").strip().split("\n")[0][:80]
        while len(block) > size * 2:  # very long paragraph: cut it
            if buf:
                chunks.append((buf_section, buf))
                buf = ""
            chunks.append((section, block[:size]))
            block = block[size - overlap:]
        if buf and len(buf) + len(block) > size:
            chunks.append((buf_section, buf))
            buf = buf[-overlap:] + "\n\n" + block
            buf_section = section
        else:
            if not buf:
                buf_section = section
            buf = (buf + "\n\n" + block) if buf else block
    if buf.strip():
        chunks.append((buf_section, buf))
    return chunks


class KnowledgeBase:
    def __init__(self, root: str, max_upload_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.sources = self.root / "sources"
        self.texts = self.root / "text"
        self.sources.mkdir(parents=True, exist_ok=True)
        self.texts.mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = max_upload_bytes
        self._lock = threading.Lock()
        self._sig: Optional[tuple] = None
        self._chunks: List[Dict[str, Any]] = []
        self._df: Dict[str, int] = {}
        self._avg_len = 1.0
        self._converter = None

    # ---- conversion ------------------------------------------------------------------------------
    @staticmethod
    def docling_available() -> bool:
        return importlib.util.find_spec("docling") is not None

    def _docling_markdown(self, path: Path) -> str:
        if self._converter is None:
            from docling.document_converter import DocumentConverter
            self._converter = DocumentConverter()
        return self._converter.convert(str(path)).document.export_to_markdown()

    def _extract(self, path: Path, ext: str) -> Tuple[str, str]:
        """Return (text, converter name). Blocking; run in a thread."""
        if ext in (".html", ".htm"):
            parser = _HTMLText()
            parser.feed(path.read_text("utf-8", errors="replace"))
            return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip(), "html"
        if ext in TEXT_EXT:
            return path.read_text("utf-8", errors="replace"), "text"
        docling_error = ""
        if self.docling_available():
            try:
                return self._docling_markdown(path), "docling"
            except Exception as e:  # docling failed on this file; fall through to simpler parsers
                docling_error = f" (docling failed: {e})"
        if ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as e:
                raise KnowledgeError(f"Could not read the PDF: {e}{docling_error}")
            if not text.strip():
                raise KnowledgeError("The PDF has no extractable text (scanned?). Install docling with OCR to index it.")
            return text, "pypdf"
        try:
            return _office_text(path, ext), "office"
        except KnowledgeError:
            raise
        except Exception as e:
            raise KnowledgeError(f"Could not read this {ext} file: {e}{docling_error}")

    async def add_document(self, filename: str, data: bytes) -> Dict[str, Any]:
        import asyncio
        name = safe_filename(filename)
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in ALLOWED_EXT:
            raise KnowledgeError("Unsupported file type. Allowed: " + ", ".join(sorted(ALLOWED_EXT)))
        if not data:
            raise KnowledgeError("The file is empty")
        if len(data) > self.max_upload_bytes:
            raise KnowledgeError(f"File exceeds the {self.max_upload_bytes // (1024 * 1024)} MB upload limit")

        def work() -> Dict[str, Any]:
            src = self.sources / name
            fd, tmp = tempfile.mkstemp(dir=self.sources, prefix=".upload-", suffix=ext)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            try:
                text, converter = self._extract(Path(tmp), ext)
            except BaseException:
                os.unlink(tmp)
                raise
            if not text.strip():
                os.unlink(tmp)
                raise KnowledgeError("No text could be extracted from this file")
            os.replace(tmp, src)
            (self.texts / f"{name}.md").write_text(text, encoding="utf-8")
            return {"status": "success", "filename": name, "chars": len(text), "converter": converter}

        return await asyncio.to_thread(work)

    # ---- management ------------------------------------------------------------------------------
    def list_documents(self) -> List[Dict[str, Any]]:
        docs = []
        for p in sorted(self.sources.iterdir(), key=lambda x: x.name.lower()):
            if p.name.startswith(".") or not p.is_file():
                continue
            txt = self.texts / f"{p.name}.md"
            docs.append({"filename": p.name, "size_bytes": p.stat().st_size, "modified_at": p.stat().st_mtime,
                         "chars": txt.stat().st_size if txt.exists() else 0})
        return docs

    def delete_document(self, filename: str) -> bool:
        name = safe_filename(filename)
        if not name:
            return False
        found = False
        for p in (self.sources / name, self.texts / f"{name}.md"):
            if p.is_file() and p.parent in (self.sources, self.texts):
                p.unlink()
                found = True
        return found

    # ---- search ----------------------------------------------------------------------------------
    def _signature(self) -> tuple:
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.texts.glob("*.md")))

    def _rebuild(self, sig: tuple) -> None:
        chunks: List[Dict[str, Any]] = []
        for p in sorted(self.texts.glob("*.md")):
            name = p.name[:-3]
            for section, text in _chunk(p.read_text("utf-8", errors="replace")):
                toks = _tokens(text + " " + section)
                if toks:
                    chunks.append({"filename": name, "section": section, "text": text, "tokens": toks,
                                   "tf": {t: toks.count(t) for t in set(toks)}})
        df: Dict[str, int] = {}
        for c in chunks:
            for t in c["tf"]:
                df[t] = df.get(t, 0) + 1
        self._chunks, self._df = chunks, df
        self._avg_len = (sum(len(c["tokens"]) for c in chunks) / len(chunks)) if chunks else 1.0
        self._sig = sig

    def search(self, query: str, k: int = 5) -> Dict[str, Any]:
        with self._lock:
            sig = self._signature()
            if sig != self._sig:
                self._rebuild(sig)
            chunks, df, avg = self._chunks, self._df, self._avg_len
        if not chunks:
            return {"status": "success", "query": query, "matches": [],
                    "note": "The knowledge base is empty. An administrator can upload documents."}
        q = _tokens(query)
        n, k1, b = len(chunks), 1.5, 0.75
        scored = []
        for c in chunks:
            score = 0.0
            for t in set(q):
                f = c["tf"].get(t)
                if f:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(c["tokens"]) / avg))
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda s: s[0], reverse=True)
        matches, per_doc = [], {}
        for score, c in scored:
            if per_doc.get(c["filename"], 0) >= 2:
                continue
            per_doc[c["filename"]] = per_doc.get(c["filename"], 0) + 1
            matches.append({"filename": c["filename"], "section": c["section"], "score": round(score, 2),
                            "excerpt": c["text"][:700]})
            if len(matches) >= k:
                break
        return {"status": "success", "query": query, "matches": matches}

    def stats(self) -> Dict[str, Any]:
        return {"documents": len(self.list_documents()), "docling": self.docling_available()}


SHARED_LABEL = "Shared knowledge"


class KnowledgeLibrary:
    """The one place all knowledge lives:  <root>/shared/  (added by admins)  and  <root>/users/<id>/  (private).

    Everyone's searches cover the shared documents plus their own. Shared documents are never listed to
    ordinary users and their file names are hidden in results; each user's own documents count against a quota.
    """

    def __init__(self, root: str, max_upload_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.max_upload_bytes = max_upload_bytes
        self._stores: Dict[str, KnowledgeBase] = {}
        self._lock = threading.Lock()
        self.store(None)

    def store(self, owner: Optional[str]) -> KnowledgeBase:
        """owner None = the shared library."""
        if owner is not None and not valid_identifier(owner):
            raise KnowledgeError("Invalid user")
        key = owner or ""
        with self._lock:
            kb = self._stores.get(key)
            if kb is None:
                path = self.root / "shared" if owner is None else self.root / "users" / owner
                kb = self._stores[key] = KnowledgeBase(str(path), self.max_upload_bytes)
            kb.max_upload_bytes = self.max_upload_bytes
            return kb

    def usage(self, owner: Optional[str]) -> int:
        return sum(d["size_bytes"] for d in self.store(owner).list_documents())

    async def add(self, owner: Optional[str], filename: str, data: bytes, quota_bytes: Optional[int] = None) -> Dict[str, Any]:
        kb = self.store(owner)
        if quota_bytes is not None:
            name = safe_filename(filename)
            replaced = next((d["size_bytes"] for d in kb.list_documents() if d["filename"] == name), 0)
            if self.usage(owner) - replaced + len(data) > quota_bytes:
                raise KnowledgeError(f"Your knowledge-base storage is full ({quota_bytes // (1024 * 1024)} MB). Remove a document first.")
        return await kb.add_document(filename, data)

    def search(self, owner: Optional[str], query: str, k: int = 5, reveal_shared: bool = False) -> Dict[str, Any]:
        stores = [(self.store(None), True)] + ([(self.store(owner), False)] if owner else [])
        matches, note = [], None
        for kb, shared in stores:
            r = kb.search(query, k)
            note = note or r.get("note")
            for m in r["matches"]:
                if shared and not reveal_shared:
                    m = {**m, "filename": SHARED_LABEL}
                matches.append(m)
        matches.sort(key=lambda m: m["score"], reverse=True)
        out: Dict[str, Any] = {"status": "success", "query": query, "matches": matches[:k]}
        if not matches and note and all(not kb.list_documents() for kb, _ in stores):
            out["note"] = "The knowledge base is empty. Documents can be added on the Knowledge base page."
        return out

    def delete_user(self, owner: str) -> None:
        if valid_identifier(owner):
            with self._lock:
                self._stores.pop(owner, None)
            shutil.rmtree(self.root / "users" / owner, ignore_errors=True)

    def stats(self) -> Dict[str, Any]:
        users = self.root / "users"
        n_users = sum(1 for d in users.iterdir() if d.is_dir()) if users.is_dir() else 0
        return {"documents": len(self.store(None).list_documents()), "user_libraries": n_users,
                "docling": KnowledgeBase.docling_available()}
