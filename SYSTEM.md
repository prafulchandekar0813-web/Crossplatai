# SYSTEM.md: how this system works, how it fails, how to change it

Read this first when you work on the code (you, or an assistant helping you). It is the map. The README says how to run
and use the product; this file says how the pieces fit, what breaks and why, and where to make a change safely.
**Keep it true:** when you change behaviour, change the matching line here in the same edit (see the last section).

## 1. What it is

A FastAPI server (`server.py` -> `llm_agent/api.py`) with a plain-JavaScript web UI (`llm_agent/web_ui`). A user chats; an
assistant (any OpenAI-compatible model) plans, calls tools, writes files into that user's private workspace, builds web
apps, previews them, and can publish them and package them as Android/Windows apps. Around that: accounts and plans,
Razorpay payments, memory and notes, skills, GitHub-based UI starts, and a review area for the administrator and Managers.

Design rules that explain most decisions:

- **Thin layers, one job per file.** `api.py` only authenticates, validates and calls the `Orchestrator`. Logic lives in
  small modules (`notes.py`, `skills.py`, `payments.py`, ...). Pure helpers (`buildcheck.py`, `uilint.py`, `html_text.py`) take
  text in and give findings out, no files or network, so they are easy to test.
- **Async everywhere.** One SQLite connection (WAL) reached through worker threads (`db.py`). No blocking calls in handlers.
- **Security first.** Provider keys are encrypted (`vault.py`), never sent to the browser or the model. The assistant is
  told tool output is untrusted data. Every outbound fetch is checked against private addresses (`web_fetch.py`).
- **The system explains its own failures.** Tools return `{"status":"error","error":"plain sentence"}`; the assistant
  and the UI show that sentence, never a stack trace.
- **Nothing shared goes live without a person.** The curator drafts, the administrator/Manager approves.

## 2. Map of the code

| Area | Files | One line |
|---|---|---|
| Entry | `server.py`, `api.py` | Routes (about 140), auth dependencies `principal`, `reviewer`, admin checks. |
| Task engine | `core/orchestrator.py`, `core/agent.py`, `core/runs.py` | Starts a task, builds the prompt and history, runs the agent loop, stores the answer. |
| Model access | `core/llm_client.py`, `core/patience.py` | OpenAI-style client; waits out 429/5xx instead of failing; detects "context full". |
| Prompt | `core/prompts.py` | The admin's base prompt plus everything that must stay true (date, tools, memory, skills, notes). |
| Limits | `circuit_breaker/*` | Turn/tool/time/token limits, checkpoints, loop detection, `CallCache` (same call = same answer, free). |
| Tools | `tools/registry.py` | Every tool: definition, handler, how it shows in the UI. Add tools here. |
| Files | `tools/file_manager.py` | Sandboxed workspace, patch/edit tools with fuzzy matching and numbered hints. |
| Web | `tools/web_search.py`, `web_research.py`, `web_fetch.py`, `html_text.py`, `web_scrape.py` | Search (SearXNG / Brave / DuckDuckGo, tidy, recency), one-call research, page reader, JS-page data, metered Olostep scraper. |
| GitHub | `tools/github_repos.py`, `skill_files/github-ui.md` | `github_search` and `github_clone` (permissive licences only, zip download, safe unpack, SOURCE.md), scan then copy into its own folder. |
| Repo safety | `repo_scan.py`, `repo_review.py`, `repo_approval.py` | Fixed-rule scan, model review (raise only), the user's "run anyway". |
| Project audit and lock | `project_audit.py`, `project_gate.py`, `analyze_project` tool | Framework/db/admin report, PROJECT.md marker, file tools refuse edits to an unanalysed copy. |
| One place for repo files | `overlay.py`, `file_manager.list(hide_base)`, `usage_bytes`, `github_repos.clone` | `.base.json` lists unchanged library files; they are hidden from the user's Files and quota. |
| Repo library | `repo_library.py`, `github_repos._obtain/_save`, `api` `/api/admin/repos*`, `admin.js` Repo library tab | Shared checked copies by category in `data_dir/repo_library`; clone reads from it first. |
| Ready gate | `readiness.py`, `run_proof.py`, `apps._ready`, `hosting.deploy` | Required checks before build and hosting for copied projects. |
| App data | `datastore.py`, `backups.py`, `hosting.configure_data` | Local (one .db file) or own online database, daily backups. |
| App admin wall | `admin_session.py`, `api.live_proxy`, `admin_live` | Admin paths (`/admin`, `/admin-api`, admin.json paths) only for the owner's Manage session. |
| Admin dashboard | `admin_kit.py`, `web_ui/admin_app.html`, `api` `/admin-app/*` and `/media/*` | Dashboard drawn from `admin.json`; proxies CRUD to the app's `/admin-api`; stores uploads. |
| Context | `context/context_manager.py` | Token estimates, history window, summaries, tool-output compaction. |
| Memory | `memory/store.py`, `notes.py`, `memory/decay.py` | Chat history, short facts, rolling summaries, longer notes; expiry of transient data. |
| Knowledge | `skills.py`, `skill_files/*.md`, `flags.py`, `curator.py`, `scrub.py` | Guides the assistant loads; flags/counters; the drafting loop; privacy scrubbing. |
| Apps | `apps.py`, `appkit.py`, `content_schema.py`, `preview.py`, `hosting.py`, `builders.py`, `buildcheck.py`, `uilint.py`, `runspec.py`, `ci_build.py`, `shell_template/` | Build, preview, publish, host, package. |
| Accounts | `auth.py`, `security.py`, `plans.py`, `payments.py`, `profile.py`, `mail.py`, `mail_templates.py`, `vault.py` | Sessions, roles (admin, Manager, user), plans, Razorpay, e-mail codes, key encryption. |
| Oversight | `audit.py`, `signals.py`, `core/reporter.py` | What users asked and what safety checks refused; per-user reports for the administrator. |
| UI | `web_ui/js/*.js`, `web_ui/styles.css` | No framework: `h()` builds DOM, one class per view. |
| Tests | `tests/`, `tests/fake_llm.py` | `pytest -q`. The fake model is scripted by words in the prompt. |

## 3. What happens when a user sends a message

1. `POST /api/sessions/{id}/chat` checks login, plan limits, size (413) and that no task is already running.
2. The user message is stored (`chat_history`). A `Run` is created; its event log is what the UI streams (SSE).
3. `Orchestrator._run_agents` assembles: system prompt (`prompts.py`: base prompt, date, tool rules, skills catalogue,
   workspace listing, saved facts, **note titles**, session summary) + chat history (`ContextManager.prepare_history`) + the new message.
4. `Agent.run` loops: ask the model (patiently) -> run the tool calls -> add results -> repeat until it answers.
   Per turn it applies: duplicate-call cache, loop detector, per-task limits, checkpoints (renew allowance, compact, carry on),
   token budget, and the once-per-task "prepare first" nudge (research before big code, `app-design` before big HTML/CSS).
5. Tools run through `registry.py` against the user's workspace/shell/web. Large outputs are truncated; old ones compacted.
6. The final text is stored, the trace saved (expires), counters bumped (`metrics`, only if the user allows), the UI gets `final`.

Bigger jobs: `delegate_task` runs worker agents (own lane in the UI) in parallel, up to `max_subagents_per_user`.

## 4. Where data lives (all in one SQLite database unless noted)

- **Per user, private:** `chat_history`, `chat_summaries`, `permanent_facts` (short facts), `notes`, `user_skills`, workspace files
  (disk, `workspace_storage/<user>`), `app_*` tables for their apps, `app_secrets` (encrypted).
- **Shared, reviewed:** `skill_overrides` (approved skill edits), `skill_proposals`, `knowledge_flags` + `metrics` (scrubbed, 30 days).
- **Shared, on disk:** `data_dir/repo_library/<category>/<owner--name>/{files,meta.json}`, the checked GitHub copies every user can start from (admin-managed).
- **Admin uploads, on disk:** `data_dir/app_media/<app id>/<16hex>.<ext>`, public at `/media/...`, removed with the app.
- **Hosted apps:** `hosted_apps` also holds `data_mode`, `data_engine`, `data_provider`, `data_configured`, `admin_paths`, `admin_schema` (validated admin.json); `run_proofs` (per user, fingerprint of the files last started in the sandbox). Database addresses live in `app_secrets`; local databases and backups live in Docker volumes and `data_dir/backups`.
- **Money and access:** `users` (role, `manager`, `share_flags`), `subscriptions`, `payments` (one row per Razorpay link; the
  server asks Razorpay if it is paid, never trusts the browser), `auth_sessions`.
- **Expires on its own:** run traces (`transient_logs`), login sessions, flags (30 days). Chat history and facts are never auto-deleted.
- Deleting an account runs `MemoryStore.delete_user_data` (history, summaries, facts, notes, personal skills, traces) and removes its files.

## 5. Memory, notes and skills (what the assistant "knows")

- **Within a chat:** the last messages are sent verbatim. Older ones are folded into `chat_summaries` once the history exceeds its
  budget. History always keeps at least 35% of `summary_trigger_tokens` (`HISTORY_FLOOR`), however big the prompt and tool list are.
- **Across chats:** *facts* (one sentence, always in the prompt, `remember` tool or the Memory page) and *notes* (longer write-ups; only
  titles in the prompt; `save_note` / `read_notes` tools; "Write it up as notes" on the Memory page builds notes from a whole chat).
- **Skills:** the prompt lists names + one-line descriptions; `use_skill(name)` loads the text. Order of precedence for a user:
  their own skill > approved edit (DB) > built-in file. Personal skills are private; reviewers and the curator never read them.
- **Learning loop:** the assistant may `flag_knowledge` (generic, scrubbed) -> daily curator run drafts a skill edit -> a person approves in Review -> live.

## 6. How it fails, and why (symptom -> cause -> where to look -> fix)

| Symptom | Usual cause | Look at | Fix |
|---|---|---|---|
| Assistant forgets after 3-4 messages | History budget squeezed by a big prompt/tool list (fixed: `HISTORY_FLOOR`) or the model's window is smaller than `max_context_tokens` | `ContextManager.prepare_history`, Admin > Limits | Set the limits to the real window of the model; check the "chat summarised" event. |
| "Search provider returned HTTP 429" | Provider rate limit | `web_search.py` | Already spaced, retried, cached; DuckDuckGo is the backup. Add a second SearXNG address or a Brave key. |
| "No readable text (JavaScript)" | Page builds itself in the browser | `web_fetch.py` | Reader uses JSON-LD/embedded data and an archived copy; else `scrape_page` (metered) or find the site's API/feed. |
| Deploy: "The app did not start" with a traceback | Generated code uses an API removed from the newest library (for example Flask `before_first_request`) | `buildcheck.REMOVED`, `skill_files/python-web-apps.md` | Add the pattern to `REMOVED`, and the rule to the skill. Pin versions in requirements. |
| Task stops with "limit reached" | Turn/tool/time/token budget | `circuit_breaker`, Admin > Limits | Checkpoints renew up to `max_checkpoints`; raise limits for big jobs. |
| "Replaced text not found" loops | Patch text differs from file | `file_manager._fuzzy`, `_hint` | Fuzzy match (93%) auto-applies; otherwise numbered lines are returned; the cache stops identical retries. |
| Model 429/503, tasks wait | Provider busy | `core/patience.py` | Waits 10s up to 90s, shared per task, up to `max_wait_seconds`. |
| Tasks vanish after a Render restart | Free hosting restarts lose running tasks | Render logs | Not fixable in code; user sends the message again. Own server later. |
| Ugly or bare app UI | Assistant skipped the design guide | `uilint.py`, `app-design.md`, `github-ui.md` | Held back once until `use_skill("app-design")`; it starts from a cloned open-source UI; lint feedback in `check_build`. |
| Subscribe says payment is not set up | No `RAZORPAY_KEY_ID`/`SECRET` | `payments.py` | Add both env vars. |
| Verification e-mail not sent | Host blocks SMTP ports (Render free), wrong app password, or no mail settings | `mail.py` (the error text names the cause) | Use Brevo or Resend over HTTPS (README, Profile), or fix the Gmail app password. |
| `github_search` says the limit was reached | About 10 unauthenticated searches a minute | `github_repos.py` | Wait, or set `GITHUB_TOKEN`. |
| `github_clone` refuses a repo | Licence not permissive, repo too big, or private | `github_repos.PERMISSIVE`, `MAX_REPO_KB` | Pick another project, or use `subdir`. |
| `github_clone` says blocked / needs_approval | Scan found something (rule names in `repo_scan.RULES`) | `repo_scan.py`, `repo_review.py` | Blocked: next project. Warn: the user replies "run anyway" (typed by them, not the assistant). A false alarm: adjust the rule. |
| Edits to a copied project are refused | `PROJECT.md` has no analysis marker | `project_gate.py` | Call `analyze_project` on the folder. |
| Build or Deploy says "not ready" | A required check in `readiness.py` failed; the message lists each | `readiness.check`, `check_build` | Fix the listed items; run the project in the sandbox again after the last edit (`run_proof.py`). |
| "The name is already used by another app" | Names are unique across users (`apps.name_taken`) | `apps._need_free_name`, tool `check_app_name` | Choose another name. |
| Open admin says the app has no admin API yet | `/admin-api/<first collection>` answered 404 | the app's `/admin-api` routes, `admin.json` | Ask the assistant (admin-access, B), update, open again. |
| Open admin says the app has no admin page yet (own-admin projects) | The app answered 404/410 at every admin path (`api.admin_open` probes) | the app's `/admin` routes, `admin.json` | Ask the assistant to add the admin (admin-access, B), update, open again. |
| Admin gives 403 on the app's own address | By design: admin paths are Manage-only | `admin_session.py`, `admin.json` | Open it from Manage app > Open admin. List extra admin paths in `admin.json`. |
| App won't start: "no address is saved" / "needs Docker" | Provider mode without an address | `hosting.data_problem` | Save the address in Manage app, or choose SQLite. |
| Admin/Review controls cut off on a phone | Layout overflow (fixed in `styles.css`) | `.page`, `.list-row` | Keep `.page{width:100%}` and wrapping rows. |
| No browser screenshots of generated UIs | No Chromium on Render | - | Not available there; `uilint` checks the code instead. |

General rule: a failure is **expected** if a tool returned a plain error sentence; a failure is a **bug** if it shows a stack
trace, silent empty output, or a loop. Bugs get a test first (see below), then a fix.

## 7. How to change things (the safe way)

| I want to... | Do this |
|---|---|
| Add a tool | `registry.py`: write `async def _name(ctx, a)` returning `{"status": ...}`; add a `ToolSpec` (definition, handler, label, result summary, `kinds`). Mention it in `prompts.py` only if it needs a rule. Add a test (the fake LLM can call it: see `keepnote` in `fake_llm.py`). |
| Change how the assistant behaves | Prefer a **skill** (`skill_files/*.md`, front matter name + description). Use `prompts.py` only for rules that must hold every time (the prompt is size-sensitive). |
| Add a check for generated apps | `buildcheck.py` (problems stop "ready", warnings advise) or `uilint.py`. Add a case to `tests/test_buildcheck.py`. |
| Add a setting | `config.py` dataclass field + range in `validate`; admin field in `web_ui/js/admin.js`; read via `settings.effective`. |
| Add an API route | `api.py` (thin): auth dependency, call a module, map its error to HTTP. Logic goes in a module. |
| Add a table | `db.py` `CREATE TABLE IF NOT EXISTS` (migrations for new columns go beside the others). Delete per-user rows in `MemoryStore.delete_user_data`. |
| Add a UI screen | `web_ui/js/<name>.js` class with `el` and `load()`; register in `app.js`. Check at 390px wide. |
| Add a search provider | `web_search.py`: a `_name` method, allow the name in `config.py` validation, add it to the admin select. |

Always: `python -m pytest -q` (all green), `python -m pyflakes llm_agent` (no new warnings), and for UI changes look at the page at phone width.

## 8. Keeping this file true

- Changed a module's job? Update section 2. Changed the request flow? Section 3. New table? Section 4. New failure you fixed? Add a row to section 6.
- If a row here and the code disagree, the code wins; fix the row.
- Zip for delivery: `rm -rf data workspace_storage config.json`, then zip without `__pycache__`, `.pytest_cache`, `data`.
