# Agent Workbench

A self-hosted AI assistant that plans a task, hands parts of it to worker agents, writes files into a private
workspace, and shows you every step while it happens. It talks to any OpenAI-compatible model server
(SGLang, vLLM, Ollama, LM Studio, OpenRouter, OpenAI).

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # set OPENAI_BASE_URL and OPENAI_MODEL
python server.py
```

Open http://localhost:8080. On first start a random admin password is printed **once** in the terminal
(username `admin`). Set `ADMIN_PASSWORD` in `.env` to choose your own. Everyone else creates an account on the
sign-in page; turn that off under Admin → Limits → Accounts once your users have signed up.

You can also set the model in the browser: Admin → Model → "Save and test connection". The test tells you whether
the server supports tool calling, which the assistant needs for files, search and workers.

No model server handy? `python -m tests.fake_llm` starts a scripted fake on http://127.0.0.1:9099/v1.
Prompts containing "build", "loop", "remember", "kb" or "slow" show the different behaviours.

## What you see in the UI

* **Chat with a live activity view.** Each task shows a timeline: thinking, plan, tool calls, and one coloured lane
  per worker agent. Click a step to see its exact arguments and result. The right panel shows agents, how close
  each is to its limits, files written and token usage.
* **Stop** cancels a running task at any point. Closing the tab does not: the task keeps running and you can
  reopen the chat to watch or read the result.
* **Files**, **Memory** and **Knowledge base** pages, and an **Admin** console (model, limits, instructions,
  web search, users, maintenance). Settings set by environment variables are shown as locked.

## Configuration

Precedence: environment variable > `config.json` (written by the Admin console) > defaults. Secrets are never
returned to the browser. Common variables: `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`,
`ADMIN_PASSWORD`, `SEARCH_PROVIDER` (`searxng` or `brave`), `SEARCH_BASE_URL`, `SEARCH_API_KEY`,
`ALLOW_REGISTRATION`, `DATA_DIR`, `HOST`, `PORT`. See `.env.example`.

## App preview

When the assistant builds a web page or app (plain HTML, CSS and JavaScript), a **Preview app** button appears under
its answer, and the sidebar has a **Preview app** entry. The window shows the app at phone, tablet or desktop size
(with rotate, reload and open-in-new-tab). Previews run in a sandbox with no access to your session or the API, via
short-lived signed read-only links. Only static files are served: **server code (Flask, Node and so on) is saved but never
executed**. Running arbitrary generated code safely needs a container sandbox, which this project does not include.

## Editing files

Besides `write_file`, the assistant has `patch_file`: exact-text replacements applied to an existing file, all or nothing,
so small changes don't mean rewriting (and risking) the whole file. `edit_lines` changes specific lines by number
(replace, insert or delete); the assistant reads the file with `line_numbers` first, and all changes in one call apply together. `insert_text` adds new lines or a paragraph at the start, the end, or next to a
line number or a piece of text, and touches nothing else.

## Downloading your work

Files has **Download everything (.zip)**, and every folder has a download icon and a delete icon (with a confirmation).
Symlinks are never followed.

## Running apps: the assistant's command line (bash)

With **Give the assistant a command line** switched on (Admin > Apps, or `SHELL_ENABLED=true`) the assistant gets a `bash` tool. Each chat has its own
**temporary environment** (a private home folder, tmp, logs and Python venv under `data/shell_envs/`), while commands start in your project folder, so
the files it writes are the files it runs. The environment is thrown away when the chat is deleted, when it has been idle for six hours, and on every restart.

It can install packages (`python3 -m venv ~/venv && pip install flask`), run and test code, and start the app in the **background** on a port from `$PORT`.
**Preview app > Run app** then shows the running app (phone / tablet / desktop frame, log view, countdown); ask for changes and run again. When you're happy,
**Looks good, build it** sends that folder to Build app; if you're not, nothing is built. Sub-agents (up to four per user) use the same tool in the same chat environment.

- **Block-list check** (`shell_guard.py`): every command is checked first. Refused: privilege escalation, system and account changes, killing processes, raw network
  tools and remote shells, download-and-run, private-network fetches, `rm -rf` of everything, paths outside your project or the chat's home, background tricks (`nohup`, `&`),
  nested `bash -c` / `eval` / `$( )` hiding any of these. **This is a safety net, not a sandbox**: a program the assistant writes and then runs can do whatever the operating-system user can.
- **Isolation** is the real protection, in two levels:
  - *bubblewrap* (`apt install bubblewrap`): the command sees only the system's read-only folders, your project and the chat's home, not the server's settings or other users. Recommended.
  - *basic* (no bubblewrap): scrubbed environment (the server's keys and passwords are not passed on), CPU / file / process limits, own process group, time limit, but **it can read anything the server's user can**.
    It is refused when the server runs as root, and otherwise only works after an admin ticks **Allow it without bubblewrap isolation** (trusted users only). Run the server as a dedicated low-privilege user.
- Limits: 120 s per command, 20 KB of output, one background app per chat, three per server, stopped after 30 minutes (all adjustable). Running apps listen on 127.0.0.1 and are only reachable through this server's signed-link proxy, inside a sandboxed frame.
- Internet access inside the environment is on by default (pip needs it); with bubblewrap you can switch it off. Because the environment shares the server's network, an app a user runs can reach other local ports: another reason to run this on a machine you can afford to lose.
- This was tested with real processes in *basic* mode. The bubblewrap command line is generated but was **not run** in the development environment (it has no bubblewrap): try it before relying on it.

## Build app and Manage app

Two kinds of app (Build app has a switch):

- **Web link**: upload a front-end zip (`index.html`); customers open `/a/<app id>/` and can add it to their home screen.
- **Android / Windows app**: you give a name, an optional logo and an optional backend project zip (any Python web app, for example Flask). The server never runs it for customers and never packs it into the app. You host the backend on a platform you choose; the Android and Windows app is a branded shell that opens its https address. You can create it from the preview ("Looks good, build it") or from Build app by uploading a zip.

Each app gets an **app ID and a password (shown once)**. **Manage app** (App ID + password) is the app's admin dashboard: products (title, price, description, picture, in stock), heading, accent colour, announcement, **app address**, usage, the **Inbox** of orders and messages sent from the app, **Update**, "Your own app" (build and download the Android and Windows files), "Download backend to host" (your project plus `workbench_client.py`, `app_config.json` and `DEPLOY.txt`), and "Change the app itself" (name, logo, backend files, higher version).

**Update button and cool-down.** Edits in Manage app are drafts. **Update** publishes them: for 60 seconds installed apps keep getting the old version and show "Updating app, wait or reopen in about a minute", then switch.

**Usage numbers.** Each install makes a random id; the server keeps first/last seen, open count, app version and platform (Android or Windows), never an IP or name. The dashboard shows devices, active today / week / month, opens and the split by version and platform. They are devices, not people.

**Inbox.** The backend (or the shell) sends orders, contact forms and sign-ups with `Client().submit("orders", {...})`; flat JSON only, 8 KB each, 30 per device per hour, 5000 per app. The owner reads them under Manage app, marks them done or deletes them.

**Preview before building.** The assistant runs the app for real in the chat's temporary environment (Preview > Run app) at phone, tablet and desktop size. Without the bash tool it writes `preview/index.html`, an HTML mock.

## Web search

Set `SEARCH_PROVIDER=searxng` and `SEARCH_BASE_URL` to **your own** SearXNG; see `searxng/README.md` for a ready-made
configuration. Public instances (searx.be and others) block scripts, so they will not work.

## Knowledge base

All knowledge lives in one place, `data/knowledge_base/`: `shared/` (documents admins add) and `users/<name>/`
(each user's private documents, limited by Admin → Limits → "Knowledge-base storage per user", 50 MB by default).
Users see and manage only their own documents. Admins' shared documents are never listed to users and their file
names are hidden in results, but the assistant can still search them for everyone.

## Safety model

* Passwords hashed with scrypt; sessions are HttpOnly, SameSite=Strict cookies (or Bearer tokens); cross-site
  requests are rejected; login attempts are throttled; CSP and other security headers on every response.
* Each user has an isolated workspace. Paths are resolved and checked against the workspace root (symlinks and
  `..` are rejected), with a per-file size limit and a per-user quota. Users can only see their own chats,
  memories, files and runs.
* Each agent has its own circuit breaker: reasoning rounds, tool calls, repeated identical calls, consecutive
  failures, delegations and a wall-clock limit. When one trips, the agent is asked for an honest summary of what
  was and was not finished.
* `read_url` lets the assistant open public web pages and PDFs (a GitHub repo address returns its README). It refuses
  anything that resolves to a private, local or cloud-metadata address, including after redirects.
* Tool output is treated as data, never as instructions. Model failures are reported as errors; nothing is
  silently faked.

## Tools the assistant has

File tools (`write_file`, `read_file`, `patch_file`, `edit_lines`, `insert_text`, `list_files`), `bash` and `stop_app` (preview only), `web_search`, `read_url`, knowledge base, memory, sub-agents and **`check_build`**: a pre-flight check of the project folder that reports a missing start file or `requirements.txt`, a missing `PORT` setting, keys written in code (never echoed back), root-relative web addresses, a missing `.env.example`, `debug=True`, and a home-made `/admin` page that Manage app already replaces. The assistant is told to run it before saying an app is ready.

Edit tools accept the usual model slips: `edits` or `changes` sent as a JSON string or as one flat edit (`path, old, new`), numbers sent as text ("12"), `file_path` for `path`. These are corrected before the arguments are checked.

## Branded native apps (Android and Windows) through GitHub Actions

Each app can get its own APK and Windows build with its own name, logo, colour and package ID. The server compiles nothing: it starts a GitHub Actions workflow in a repository you own, follows it, downloads the result and offers it in Manage app > "Your own app".

What the built app is: a small Flutter shell (`llm_agent/shell_template/shell`) that asks this server for the app's settings (heading, colour, announcement, update notice with the 1 minute cool-down, **app address**) and shows the owner's app at that address in a WebView. The owner hosts the backend wherever they choose and sets the https address in Manage app; changing it, the heading or the colour needs no rebuild. A rebuild is only needed for a new name, icon or package ID, or a new shell version.

Set-up (once):
1. Admin downloads `/api/admin/build-repo.zip` (signed in as admin), puts its contents in a new **private** GitHub repository, branch `main`.
2. Creates a fine-grained token for that repository only with "Actions: read and write" and sets it on the server as `BUILD_GITHUB_TOKEN` (environment variable; it is never stored in config.json).
3. Sets Admin > Limits > Build repository to `owner/repo`, and the server's public address to an `https://` address (the built app calls it).

Limits: `builds_per_app_per_day` (default 3), one build per app at a time, builds time out after 2 hours, finished files are kept by GitHub for 7 days and by the server until the app is deleted. The APK is signed with Flutter's debug key (installs by hand; Google Play will not accept it, so the owner must re-sign with their own key). The Windows build is a zip of an unsigned program (SmartScreen warns). The package ID is permanent for the app's identity. **None of the Flutter shell or the workflow has been built or run by the author**: expect to fix the first run (the `flutter_inappwebview` Windows support and the generated Android settings are the likeliest places). Each build runs on GitHub's machines with only the validated values listed in the workflow; the shell contains no keys.

## Your users' data, accounts and keys

This platform does not store an app's end-user accounts, passwords or data. The assistant is told to ask the owner once where they want them (their own database or sign-in provider, or a local SQLite file for a simple app), to write the code against that choice behind a thin data layer, to read every address and key from environment variables, to ship a `.env.example` with dummy values, and to use dummy sample data in the preview. It suggests established services (hosting, database, sign-in, storage, payments, email; the list is `llm_agent/providers.py`) and, when web search is on, checks current availability and pricing first. They are suggestions, not endorsements.

**Preview > Connections.** While previewing, the user can set names and values (API address, test keys) for that chat. They are passed to the running preview app as environment variables, kept on the server next to the chat's temporary environment (file mode 600, deleted with the chat), never put into a build kit, never shown to the assistant, and secret-looking values (KEY, SECRET, TOKEN, PASSWORD) are never sent back to the browser. Anyone with access to the server's disk can read them, and in basic isolation mode the preview app's own code can too, so use test keys. System names (PATH, HOME, PORT, LD_*, PYTHON*, proxies...) are refused. The real keys are set where the owner hosts the app. After publishing, the look and content are still editable from Manage app.

## Reporter agent (misuse reports for the administrator)

A separate agent, the **Reporter**, has no tools and cannot act on anyone. It records what each user typed to the assistant, commands the safety block list refused, apps they built and suspensions (never the assistant's replies or file contents), and every `reporter_interval_minutes` (default 60) writes **one markdown report per user** with new activity: risk level (none/low/medium/high), what the user seems to be trying to do, the concerns with quotes, and the activity list. Risk is decided in two layers: fixed rules (`signals.py`) set a floor, and a model review may raise it but never lower it, so text that tries to talk the reviewer round does not hide misuse. User text is shown only in code spans and wrapped as untrusted data for the model. Without a configured model the rules still run.

Admin → **Reports**: highest risk first; mark reviewed, dismiss, download `.md`, delete, or **Suspend user** (signs them out, stops their apps, blocks login until restored; also under Users). Settings under Limits: on/off, interval, retention (default 90 days; records and reports older than that are deleted). The sign-in page tells users this happens. Deleting a user removes their activity records but keeps already-filed reports until retention expires. Rules are keyword based: expect some false positives and misses, so read the quotes before acting.

## Layout

```
server.py                 entry point
llm_agent/api.py          HTTP routes, auth guard, security headers
llm_agent/settings.py     layered config, validation, secret redaction
llm_agent/auth.py         accounts and sessions (suspend)
llm_agent/audit.py        activity records the Reporter reads
llm_agent/signals.py      misuse rules (risk floor)
llm_agent/ci_build.py     branded native builds via GitHub Actions
llm_agent/shell_template/ Flutter shell + workflow for those builds
llm_agent/connections.py  preview connection settings (env vars)
llm_agent/providers.py    trusted service suggestions for the assistant
llm_agent/builders.py     recognises the Python framework of a project
llm_agent/core/           agent loop, orchestrator, run/event log, LLM client, prompts, reporter.py
llm_agent/tools/          registry, workspace files, knowledge base (BM25), web search
llm_agent/memory/         chat history, facts, summaries, expiry
llm_agent/circuit_breaker/, llm_agent/context/
llm_agent/web_ui/         vanilla ES modules, no build step
tests/                    ~90 tests (pytest, fake model)
```

Run the tests with `python -m pytest`.

## Event stream

`POST /api/sessions/{id}/chat` returns server-sent events; `GET /api/runs/{id}/events?after=N` replays and
follows a run. Types: `run_started`, `queued`, `agent_queued`, `agent_started`, `llm_start`, `llm_end`,
`assistant_note`, `plan`, `tool_start`, `tool_end`, `file_changed`, `memory_saved`, `context_compacted`,
`context_summarised`, `warning`, `breaker`, `agent_finished`, `final`, `run_finished`.

## Known limits

* Web search (SearXNG, Brave) and the optional Docling extractor are implemented but were **not tested against live
  services**; the rest is covered by tests and a browser walkthrough with the fake model.
* Runs live in the memory of one server process. Restarting loses a running task (its saved chat remains), and
  you cannot run several server processes behind one database.
* Run activity logs expire after `memory.ttl_days_transient` days; chats and memories do not.
