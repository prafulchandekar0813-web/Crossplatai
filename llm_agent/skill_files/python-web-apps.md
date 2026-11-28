---
name: python-web-apps
description: Writing Flask/FastAPI apps that start on a fresh install: current APIs only, pinned versions, start-up code, ports, and testing before you say done.
---
The platform installs the NEWEST version of every package in requirements.txt, so code written from old examples crashes at start. Pin majors (flask>=3,<4) and use current APIs.
Flask 3: there is NO @app.before_first_request (removed). Run set-up once at start: call init() before app.run, or use `with app.app_context(): init()`. No flask.Markup / flask.escape (use markupsafe). No app.json_encoder (use app.json.default). Use @app.get / @app.post, jsonify, send_from_directory.
FastAPI: use `lifespan` for start-up, not @app.on_event. Pydantic v2: model_dump(), not dict().
Read the port from the PORT env var and listen on 0.0.0.0. Keep data under DATA_DIR if set. Never write absolute paths.
Before you call the app ready: pip install -r requirements.txt in a venv, start it with bash for a few seconds, curl its main page and one API route, read any traceback, fix it, and run check_build. An import error or traceback means it is NOT ready.
If you see an AttributeError or ImportError about a removed name, search the library's changelog with web_search instead of guessing.
