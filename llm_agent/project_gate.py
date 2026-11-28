"""A copied repository may not be edited until it has been read and described in PROJECT.md (analyze_project writes it with a marker).

The check lives in the file tools. It stops a model that jumps straight to "writing the app" and invents a new structure beside the real one.
"""
from typing import Any, Iterable, List

from llm_agent.project_audit import MARK
from llm_agent.tools.file_manager import WorkspaceError


def locked_folder(files: Any, path: str) -> str:
    """The copied-repository folder that `path` lies in and that has not been analysed yet ('' when the path is free to edit)."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
    for i in range(len(parts) - 1, -1, -1):
        folder = "/".join(parts[:i])
        try:
            source = files.resolve((folder + "/" if folder else "") + "SOURCE.md", allow_root=True)
            if not source.is_file():
                continue
            doc = files.resolve((folder + "/" if folder else "") + "PROJECT.md", allow_root=True)
            if doc.is_file() and MARK in doc.read_text("utf-8", "replace")[:300]:
                return ""
        except (WorkspaceError, OSError):
            return ""
        return folder or "."
    return ""


def check(files: Any, paths: Iterable[Any]) -> None:
    """Raise WorkspaceError (the tool's error message) when any path is inside an unanalysed copied repository."""
    for p in paths:
        if isinstance(p, str) and (folder := locked_folder(files, p)):
            raise WorkspaceError(f"'{folder}' is a copied project that has not been analysed yet. Call analyze_project with path='{folder}' first: it reads the "
                                 "whole project and writes PROJECT.md. Then change the project in place, keeping its framework and folder structure.")


def paths_of(args: dict, *keys: str) -> List[Any]:
    """The paths a write tool will touch: single keys ('path', 'to'...) and the {path} entries of a 'files' list."""
    out = [args.get(k) for k in keys]
    out += [f.get("path") for f in args.get("files", []) if isinstance(f, dict)] if isinstance(args.get("files"), list) else []
    return out
