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

Each app gets an **app ID and a password (shown once)**. **Manage app** (App ID + password) is the app's admin dashboard: products (title, price, description, picture, in stock), heading, accent colour, announcement, **app address**, usage, the **Inbox** of orders and messages sent from the app, **Update**, "Your own app" (build and download the Android and Windows files), "Hosting" (deploy, data location, keys), and "Change the app itself" (name, logo, backend files, higher version).

**Update button and cool-down.** Edits in Manage app are drafts. **Update** publishes them: for 60 seconds installed apps keep getting the old version and show "Updating app, wait or reopen in about a minute", then switch.

**Usage numbers.** Each install makes a random id; the server keeps first/last seen, open count, app version and platform (Android or Windows), never an IP or name. The dashboard shows devices, active today / week / month, opens and the split by version and platform. They are devices, not people.

**Inbox.** The backend (or the shell) sends orders, contact forms and sign-ups with `Client().submit("orders", {...})`; flat JSON only, 8 KB each, 30 per device per hour, 5000 per app. The owner reads them under Manage app, marks them done or deletes them.

**Preview before building.** The assistant runs the app for real in the chat's temporary environment (Preview > Run app) at phone, tablet and desktop size. Without the bash tool it writes `preview/index.html`, an HTML mock.

## Web search

Set `SEARCH_PROVIDER=searxng` and `SEARCH_BASE_URL` to **your own** SearXNG; see `searxng/README.md` for a ready-made
configuration. Public instances (searx.be and others) block scripts, so they will not work.

### Page scraping (Olostep, free tier)

`scrape_page` reads a page through Olostep when `read_url` cannot (JavaScript pages, sites that block plain downloads). It only exists when the server has `OLOSTEP_API_KEY` (environment only, never stored) and Admin > Limits > "Page scraping" is on. Because the free plan allows 500 requests a month, every call is counted in the database *before* it is made; the tool switches itself off at `scrape_monthly_limit - scrape_reserve` (450 by default) until the next calendar month, and also stops at `scrape_user_daily` (5) per user and `scrape_task_limit` (2) per task. Failed calls are refunded, repeat reads of a long page are served from a 10 minute cache for free, and the assistant is told it is a last resort. The count is kept by this server: if the same key is used elsewhere, lower `scrape_monthly_limit`.

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

File tools (`write_file`, `read_file`, `patch_file`, `edit_lines`, `insert_text`, `list_files`, and the batch tools `patch_files`, `write_files` and `move_file`), `bash` and `stop_app` (preview only), `web_search`, `read_url`, `scrape_page` (metered, see below), knowledge base, memory, sub-agents and **`check_build`**: a pre-flight check of the project folder that reports a missing start file or `requirements.txt`, a missing `PORT` setting, keys written in code (never echoed back), root-relative web addresses, a missing `.env.example`, `debug=True`, and a home-made `/admin` page that Manage app already replaces. The assistant is told to run it before saying an app is ready.

**Paths and moving.** A path that is slightly wrong (`app.py` for `shop/app.py`, a leading `workspace/` or `./`) still finds the file when exactly one matches, and the answer names the real path; otherwise the error lists close matches. `move_file` moves or renames a file or a whole folder, into or out of folders, and never overwrites unless asked. A zip made from a workspace, with the app in a sub-folder next to loose files, is accepted: the project root is the shallowest folder that has `run.json` or a start file.

**Multi-edit tools.** `patch_file` takes many `{old, new}` edits at once and applies all or none; if `old` differs only in indentation or trailing spaces it still matches (and the replacement is re-indented), Windows line endings are handled, and a miss names the closest line. `patch_files` patches up to 20 files in one call, checking every file before writing any. `write_files` creates up to 20 files at once (sizes and quota checked first). `insert_text` accepts an `inserts` list for several insertions in one file; all positions refer to the file as it was. `edit_lines` takes many line ranges at once.

Edit tools accept the usual model slips: `edits` or `changes` sent as a JSON string or as one flat edit (`path, old, new`), numbers sent as text ("12"), `file_path` for `path`. These are corrected before the arguments are checked.

## Branded native apps (Android and Windows) through GitHub Actions

Each app can get its own APK and Windows build with its own name, logo, colour and package ID. The server compiles nothing: it starts a GitHub Actions workflow in a repository you own, follows it, downloads the result and offers it in Manage app > "Your own app".

What the built app is: a small Flutter shell (`llm_agent/shell_template/shell`) that asks this server for the app's settings (heading, colour, announcement, update notice with the 1 minute cool-down, **app address**) and shows the owner's app at that address in a WebView. The owner hosts the backend wherever they choose and sets the https address in Manage app; changing it, the heading or the colour needs no rebuild. A rebuild is only needed for a new name, icon or package ID, or a new shell version.

Set-up (once):
1. Admin downloads `/api/admin/build-repo.zip` (signed in as admin), puts its contents in a new **private** GitHub repository, branch `main`.
2. Creates a fine-grained token for that repository only with "Actions: read and write" and sets it on the server as `BUILD_GITHUB_TOKEN` (environment variable; it is never stored in config.json).
3. Sets Admin > Limits > Build repository to `owner/repo`, and the server's public address to an `https://` address (the built app calls it).

Limits: `builds_per_app_per_day` (default 3), one build per app at a time, builds time out after 2 hours, finished files are kept by GitHub for 7 days and by the server until the app is deleted. The APK is signed with Flutter's debug key (installs by hand; Google Play will not accept it, so the owner must re-sign with their own key). The Windows build is a single `Setup.exe` made with Inno Setup (installs for the current user without asking questions, adds Start menu and desktop shortcuts and an uninstaller); the program is unsigned, so SmartScreen warns on first run. Windows builds made before the installer existed stay downloadable as zips. The package ID is permanent for the app's identity. **None of the Flutter shell or the workflow has been built or run by the author**: expect to fix the first run (the `flutter_inappwebview` Windows support and the generated Android settings are the likeliest places). Each build runs on GitHub's machines with only the validated values listed in the workflow; the shell contains no keys.

Every screen of the built app that waits (opening, waking a sleeping server, updating, cannot connect) shows the owner's chosen logo with a short opening animation and a fixed footer, "Powered by <system name>". The name is Admin > Limits > Plans/Accounts > System name; the app refreshes it from the server each time it opens, and the build bakes in a first copy. The footer is part of the shell, so the owner's settings cannot remove it. While the owner's page loads (for example a hosted backend that is waking up) the same screen stays over it, so there is no blank white screen. Updating an existing app needs the new `main.dart`, `splash.dart` and `.github/scripts/prepare.py` in the build repository and a rebuild.

## Accounts, plans and payment

Everyone signs up on **Free**; the administrator account counts as Master. Open the account menu (the name at the bottom left) for **Profile**, **Subscription** and **Logout**.

| Plan | Chat, code, preview | Build / Manage app | Limit |
|---|---|---|---|
| Free | yes | no | `free_daily_tokens` per day (default 50,000); when they are used, chat pauses for `free_cooldown_hours` (8) and then a fresh allowance starts |
| Pro | yes | yes | `pro_apps_per_month` (5) distinct apps built or managed per calendar month; opening an app already counted this month is free; no token limit |
| Master | yes | yes | none |

Pro and Master are monthly (30 days) or yearly (365 days); prices are Admin settings (`price_pro_monthly` ... `price_master_yearly`, default placeholders in rupees, `plan_currency`). An expired plan falls back to Free. The administrator can also set any user's plan in Admin > Users (no end date). Tokens are what the model reports (prompt + completion); when it reports none, about 4 characters a token is assumed. A task already running is never cut off: it may end a little over the allowance, and the pause starts then.

**Profile**: name, phone number, e-mail. Verifying the e-mail sends a 6-digit code (valid 10 minutes, 5 wrong tries, one code a minute, five an hour) from a Gmail account you own using a Google **app password** (Google account > Security > 2-step verification on > App passwords): set `MAIL_USER` and `MAIL_APP_PASSWORD` on the server. Changing the address makes it unverified again.

**Payment** uses Razorpay Payment Links: the user pays on Razorpay's own page, then the server asks Razorpay whether that link is paid (and for the right amount) before granting the plan; nothing the browser says is trusted, and confirming twice never adds time twice. Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` (environment only). The server's public address must be https for Razorpay to send people back to the site; without it they use "I have paid: check my payment". There is no webhook, so someone who pays and closes the browser must press that button (or the administrator sets the plan by hand). Not run against the live Razorpay service by the author.

## Pictures: upload, and the system fits them

Product pictures, app logos and Designer images are uploaded, not pasted. Any PNG, JPG, WEBP or GIF up to 10 MB is accepted and `imagefit.py` makes it fit instead of refusing it: phone photos stored sideways are turned upright, hidden data such as GPS position is removed, product and design pictures are shrunk to 1200 px on the long side and saved as WEBP (a strip longer than 3:1 is trimmed to 3:1 so it cannot break a layout), and a logo becomes a 512 × 512 PNG, cropped to the middle by default or kept whole with padding when "Keep the whole picture" is ticked. Designer pictures are kept per user (up to 60) and served from `/api/designs/img/`. Needs Pillow (in `requirements.txt`).

## Designer (templates and drag-and-drop screens)

Pro and Master users open **Build > Designer** (or **Manage app > Screens**), pick a starter template (shop, menu, portfolio, booking) or a blank page, and drag blocks (heading, text, button, image, shape, row/box, space, divider) onto a canvas. Pages are tabs; buttons can go to a page or open a link; colours, font and page tabs are set in the right panel. On a phone, tap a block to add it and use the bar under the canvas to move, copy or delete. Edits autosave, with undo and redo. **Use for my app** makes an app of yours open the design: the app's catalogue points at `/ui/<app id>/` (when it has no hosted backend), and later edits show up straight away. Using a design for an app counts as managing it for the Pro monthly quota. Free accounts can look at the templates but not use them. Designs are private to their owner; a Manager role and paid templates are the next step.

More blocks: icon (43 icons), badge, banner, and **Items from Manage app**, which shows the section you manage (products, menu, services...) as a grid or list and updates when you press Update. Buttons can carry an icon, boxes can have a card look. Themes: ten ready-made colour themes plus your own colours (main, background, text, cards), font and corner style; colour fields offer the theme colours as quick swatches. **Manage app > Screens** lets the owner pick one of their designs or start from a template (a copy that is theirs to edit; Items blocks are pointed at a section the app really has), open it in the Designer, or stop using it. A chosen design replaces the app's own address until stopped. Starter content: a project's `manage.json` may carry `"items"` in each section; they are imported into Manage app (editable, live at once for a new app) and the assistant is told to use this instead of hardcoding sample products. Build checks warn about pages without a viewport meta tag.

**Side by side.** A box can be a row: its items share the width equally or keep their natural size, can drop to the next line when there is no room (so phones stay readable), and can be spread left, centre, right or apart. In the editor drag a block onto the left or right edge of another to put them in one line, or select a block and use "Put beside the one above"; "Take out of this line" reverses it. The "Side by side" list offers ready-made arrangements: 2 or 3 columns, icon beside text, picture beside text, 3 pictures, 3 numbers, 2 buttons. Tapping a block in the palette adds it after the selected block (inside it only when it is an empty box). **More widgets:** quote, questions and answers, table (hours, prices), list with icons, big number, star rating, progress bar, and a **form** (messages, bookings) whose answers arrive in Manage app's inbox. **Item details and orders:** tapping an item in an Items block opens a sheet with the picture, price and every field you defined; with "Let customers order or ask" on, an Order button asks for name and phone and sends it, with the item and price, to the inbox (collection `orders`). Visits to these screens count as devices in Manage app's usage numbers.

Files: `design_schema.py` (block definitions and cleaning), `designs.py` (storage), `design_templates/*.json`, `web_ui/js/design/` (`render.js` shared by editor and viewer, `model.js` pure edits, `editor.js`, `designer.js`, `view.js`), `web_ui/view.html`.

## Your users' data, accounts and keys

This platform does not store an app's end-user accounts, passwords or data. The assistant is told to ask the owner once where they want them (their own database or sign-in provider, or a local SQLite file for a simple app), to write the code against that choice behind a thin data layer, to read every address and key from environment variables, to ship a `.env.example` with dummy values, and to use dummy sample data in the preview. It suggests established services (hosting, database, sign-in, storage, payments, email; the list is `llm_agent/providers.py`) and, when web search is on, checks current availability and pricing first. They are suggestions, not endorsements.

**Preview > Connections.** While previewing, the user can set names and values (API address, test keys) for that chat. They are passed to the running preview app as environment variables, kept on the server next to the chat's temporary environment (file mode 600, deleted with the chat), never put into a build kit, never shown to the assistant, and secret-looking values (KEY, SECRET, TOKEN, PASSWORD) are never sent back to the browser. Anyone with access to the server's disk can read them, and in basic isolation mode the preview app's own code can too, so use test keys. System names (PATH, HOME, PORT, LD_*, PYTHON*, proxies...) are refused. The real keys are set where the owner hosts the app. After publishing, the look and content are still editable from Manage app.

## Manage app adapts to the app (content layout)

Every app describes what its owner manages in `manage.json` at the project root, written by the assistant: sections (Products, Bookings, Posts, Menu...) each with fields (`text`, `longtext`, `number`, `money`, `date`, `yesno`, `choice`, `image`, `link`). Manage app builds its tabs, lists and forms from it, so a booking app shows Bookings and Services, not Products. The owner can add, rename, reorder and remove sections and fields in Manage app (Customise sections); their layout is never overwritten by a later upload, and saved entries keep all their values, so changing a layout loses nothing. Apps without a file, and apps built before this, keep the old shop fields. Customers' apps read the same fields through `workbench_client` (`Client().sync("<section>")`); what customers send still goes to the Inbox. Code: `content_schema.py` (validation, no database), `apps.py` (storage, snapshot), `web_ui/js/content.js`. `check_build` validates the file and warns when the code reads a section that is not in it.

## Hosting users' apps (one container per app)

Each app's backend runs on this server in its own container; the owner presses **Deploy** under Manage app > Hosting and the app's address is filled in automatically (installed apps pick it up from the catalogue, no rebuild). Apps sleep after `hosting_idle_minutes` and wake on the next visit.

* **Runtimes** (Admin > Limits, or `HOSTING_RUNTIME`): `off` (default), `docker` (production), `process` (testing only: no isolation, refuses to run as root unless `HOSTING_ALLOW_ROOT_PROCESS=1`, for a throw-away test server such as a free Render instance, which cannot run Docker).
* **Container limits:** read-only filesystem, private writable `/data` volume (`DATA_DIR`), `/tmp` in memory, all capabilities dropped, no-new-privileges, non-root user, process/memory/CPU limits, restart on failure (3), port published on 127.0.0.1 only. The server needs the Docker CLI and daemon; the app's own network access is not restricted by default (`hosting_network` lets you attach a network you control).
* **Data:** the owner chooses *a database on our server* (files in the app's volume; we do not read it, but backups and privacy duties are yours as host) or *their own provider* with keys they save once.
* **Provider keys:** AES-256-GCM, random 96-bit nonce per value, bound to app id and key name (a copied ciphertext does not decrypt elsewhere). The master key lives only in the environment: generate one with `python -m llm_agent.vault` and set `SECRETS_MASTER_KEY`; to rotate, set the new key there and the old one in `SECRETS_MASTER_KEY_OLD` until values are re-encrypted. Without it, saving keys is refused. Values are write-only (the API returns names only), never logged or audited (names only), scrubbed from logs shown to the owner, passed to the container through a mode-600 env file that is deleted straight after start, and never on a command line. Anyone who can read the server's environment *and* database can decrypt them: protect the host.
* **Public addresses:** with `hosting_domain` set (wildcard DNS and certificate needed) each app is served at `https://<app id>.<domain>/` with full browser features. Without it, test addresses `/live/<app id>/` are used; those pages run in a sandboxed frame, so cookies and browser storage of the app do not work there.
* **Not run for real in development:** the Docker commands are covered by tests with a recorded runner, and the flow end to end with a real process; a real `docker build/run` has not been exercised.

## Reporter agent (misuse reports for the administrator)

A separate agent, the **Reporter**, has no tools and cannot act on anyone. It records what each user typed to the assistant, commands the safety block list refused, apps they built and suspensions (never the assistant's replies or file contents), and every `reporter_interval_minutes` (default 60) writes **one markdown report per user** with new activity: risk level (none/low/medium/high), what the user seems to be trying to do, the concerns with quotes, and the activity list. Risk is decided in two layers: fixed rules (`signals.py`) set a floor, and a model review may raise it but never lower it, so text that tries to talk the reviewer round does not hide misuse. User text is shown only in code spans and wrapped as untrusted data for the model. Without a configured model the rules still run.

Admin → **Reports**: highest risk first; mark reviewed, dismiss, download `.md`, delete, or **Suspend user** (signs them out, stops their apps, blocks login until restored; also under Users). Settings under Limits: on/off, interval, retention (default 90 days; records and reports older than that are deleted). The sign-in page tells users this happens. Deleting a user removes their activity records but keeps already-filed reports until retention expires. Rules are keyword based: expect some false positives and misses, so read the quotes before acting.

## Layout

```
server.py                 entry point
llm_agent/api.py          HTTP routes, auth guard, security headers
llm_agent/settings.py     layered config, validation, secret redaction
llm_agent/auth.py         accounts and sessions (suspend)
llm_agent/plans.py        Free / Pro / Master limits, token allowance, app quota
llm_agent/profile.py      profile and e-mail code verification (mail.py sends it)
llm_agent/payments.py     Razorpay payment links
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
tests/                    ~160 tests (pytest, fake model)
```

Run the tests with `python -m pytest`.

## Event stream

`POST /api/sessions/{id}/chat` returns server-sent events; `GET /api/runs/{id}/events?after=N` replays and
follows a run. Types: `run_started`, `queued`, `agent_queued`, `agent_started`, `llm_start`, `llm_end`,
`assistant_note`, `plan`, `tool_start`, `tool_end`, `file_changed`, `memory_saved`, `context_compacted`,
`context_summarised`, `warning`, `breaker`, `agent_finished`, `final`, `run_finished`.

## Known limits

* Hosting with Docker, the Flutter shell, the GitHub build workflow and Olostep were written against their documentation and tested with fakes; run each once for real before relying on it.
* Web search (SearXNG, Brave) and the optional Docling extractor are implemented but were **not tested against live
  services**; the rest is covered by tests and a browser walkthrough with the fake model.
* Runs live in the memory of one server process. Restarting loses a running task (its saved chat remains), and
  you cannot run several server processes behind one database.
* Run activity logs expire after `memory.ttl_days_transient` days; chats and memories do not.
