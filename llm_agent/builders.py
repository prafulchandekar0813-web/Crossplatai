"""Recognise which Python framework a project uses (shown in check_build and in the app's details). One entry per framework."""
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Builder:
    id: str
    label: str
    family: str                 # "web": a backend plus pages   "gui": a screen toolkit (Kivy, Tk, Qt) that has no web address
    imports: Tuple[str, ...]    # an import of any of these (or a line in requirements.txt) marks the project


BUILDERS: Tuple[Builder, ...] = (
    # --- web family: priority order matters (first match wins)
    Builder("django", "Django", "web", ("django",)),
    Builder("nicegui", "NiceGUI", "web", ("nicegui",)),
    Builder("dash", "Dash (Plotly)", "web", ("dash",)),
    Builder("fastapi", "FastAPI", "web", ("fastapi", "starlette", "uvicorn")),
    Builder("flask", "Flask", "web", ("flask",)),
    Builder("bottle", "Bottle", "web", ("bottle",)),
    Builder("http", "Plain Python (http.server)", "web", ("http.server",)),
    # --- gui family
    Builder("kivymd", "KivyMD", "gui", ("kivymd",)),
    Builder("kivy", "Kivy", "gui", ("kivy",)),
    Builder("qt", "Qt (PyQt / PySide)", "gui", ("PyQt5", "PyQt6", "PySide2", "PySide6")),
    Builder("tkinter", "Tkinter / CustomTkinter", "gui", ("tkinter", "customtkinter")),
)
BY_ID: Dict[str, Builder] = {b.id: b for b in BUILDERS}


def _imported(module: str, code: str) -> bool:
    return bool(re.search(rf"^\s*(import|from)\s+{re.escape(module)}\b", code, re.M))


def detect(code: str, requirements: Iterable[str], has_manage_py: bool = False) -> Optional[Builder]:
    """The framework a project is written in, or None. Screen toolkits win over web frameworks (a Kivy app may also call a web API)."""
    req = {re.split(r"[=<>!~\[; ]", r, 1)[0].lower() for r in requirements}
    for fam in ("gui", "web"):
        for b in BUILDERS:
            if b.family == fam and (any(_imported(m, code) for m in b.imports) or any(m.lower() in req for m in b.imports)):
                return b
    return BY_ID["django"] if has_manage_py else None
