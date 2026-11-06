"""How to start a project: run.json ({"start": "python app.py"}) or an app.py / main.py / server.py at its top. Used to run it and to pack it."""
import json
import re
from typing import Iterable, Optional

DEFAULT_START = ("app.py", "main.py", "server.py", "manage.py")


def parse_start(run_json: Optional[str], names: Iterable[str]) -> str:
    """Return the start command. Raises ValueError with a sentence the user can act on."""
    names = set(names)
    start = None
    if run_json:
        try:
            cfg = json.loads(run_json[:4096])
        except ValueError:
            raise ValueError("run.json is not valid JSON. It should look like {\"start\": \"python app.py\"}.")
        if not isinstance(cfg, dict):
            raise ValueError("run.json must be an object like {\"start\": \"python app.py\"}.")
        start = cfg.get("start")
    if not start:
        found = next((n for n in DEFAULT_START if n in names), None)
        if not found:
            raise ValueError("Nothing to start: add app.py (or main.py / server.py), or a run.json saying how to start the app.")
        start = "python manage.py runserver" if found == "manage.py" else f"python {found}"
    if not isinstance(start, str) or len(start) > 300 or "\n" in start:
        raise ValueError("run.json: \"start\" must be one short command, for example \"python app.py\".")
    return start
