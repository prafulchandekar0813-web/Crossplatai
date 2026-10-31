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

## Layout

```
server.py                 entry point
llm_agent/api.py          HTTP routes, auth guard, security headers
llm_agent/settings.py     layered config, validation, secret redaction
llm_agent/auth.py         accounts and sessions
llm_agent/core/           agent loop, orchestrator, run/event log, LLM client, prompts
llm_agent/tools/          registry, workspace files, knowledge base (BM25), web search
llm_agent/memory/         chat history, facts, summaries, expiry
llm_agent/circuit_breaker/, llm_agent/context/
llm_agent/web_ui/         vanilla ES modules, no build step
tests/                    62 tests (pytest, fake model)
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
