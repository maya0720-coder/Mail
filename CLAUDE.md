# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` holds the repository rules (style, commits, security). This file covers the points that take several files to work out. The project's docs, UI, prompts, and commit messages are written in Korean.

## Mandatory workflow

- Before changing behavior, read the relevant entries in `docs/변경이력.md`. After every code or prompt change, add a dated entry to it (`## YYYY-MM-DD` section) listing the affected files, the user-visible behavior change, and the validation you ran (for example "전체 unittest N개 통과"). Never include mail content or personal config.
- `config.json` and `data/` hold real company mail (`data/golfzon/`). Never read them or run against them for testing. Use `config.demo.json` (→ `data/demo`) or temp dirs instead.

## Commands

```powershell
# Full test suite (stdlib unittest)
.\.venv311\Scripts\python.exe -m unittest discover -s tests -v
# Single module / class / test
.\.venv311\Scripts\python.exe -m unittest tests.test_pipeline -v
.\.venv311\Scripts\python.exe -m unittest tests.test_web.WebTests.<test_name> -v

# Run with demo data (http://127.0.0.1:18766)
.\.venv311\Scripts\python.exe app.py --config config.demo.json serve
# Other CLI subcommands: demo | import <path> | collect outlook|groupware [--preview --start --end] | outlook-status | search <q> | report [--start --end] | wiki
```

`setup.bat` creates `.venv311`. `run.bat` / `MailBrain_OneClick.bat` check the setup and then launch. There is no build step, linter, or formatter. Run `node --check static/<file>.js` to syntax-check edited JS. The change log uses this check.

## Architecture

Single-user local app with no web framework. It uses the stdlib `http.server` plus SQLite.

- **`mailbrain/core.py`** is the center:
  - `load_config` resolves `user_email` from the Windows `USERNAME` + `email_domain` when `identity_mode=windows_ad`. It also makes `data_dir` absolute.
  - `Store` owns the SQLite schema and runs migrations in `__init__`. Tables: `messages`, `proposals`, `tasks`, `events`, `runs`, `reports`. Every task mutation goes through `Store.change`, which writes an `events` before/after snapshot in the same transaction. Reports are rebuilt by replaying events up to the period's end, so never update tasks without an event.
  - The data dir is bound to the first owner email (`bind_owner`).
  - `Pipeline.ingest` normalizes messages, rejects duplicates by ID, raises a conflict error when the same ID arrives with a different body, and applies sender/noise policy (`policy.py`). It then runs analyze → verify → reconcile and stores **proposals**. Tasks change only when the user accepts a proposal (`Store.resolve`). Later mail never overwrites tasks automatically.
- **Engine / prompts**: `Engine.role(name, payload)` concatenates `main_prompt + prompts/common.md + prompts/<role>.md` and records its SHA-256 in `runs` (see also the `@audited` decorator).
  - With `engine=rules` (the default), it returns `None` and the code's deterministic rule functions (`rule_analysis`, etc.) produce the result.
  - With `engine=ollama`, it calls only `http://127.0.0.1:11434/api/chat`, bypassing the system proxy, with a 120KB input cap.
  - Editing a prompt file therefore changes the hashes, but it only changes output under Ollama. Role I/O contracts are in `prompts/README.md`.
- **Read models / features**: `topics.py` groups by ConversationID plus normalized subject, newest-received first. `rag.py` is an SQLite FTS5 BM25 index over mail chunks and task events, with no embeddings. `reporting.py` produces the weekly report (구분·금주·차주·비고) as Markdown/PDF. `solutions.py` builds cached suggestions from the latest mail summary of a conversation. Bump its cache version when you change inputs.
- **Collection**: `collectors.py` handles EML/JSON, Classic Outlook through COM (`pywin32`), and a selector-configured browser groupware adapter (optional `requirements-collectors.txt`). `sync.py` `SyncManager` runs Outlook COM on a dedicated STA worker thread: initial 30-day sync at startup, then every `outlook_sync_minutes`, with a UTC DASL `Restrict` filter. It skips body reads for known IDs.
- **Web**: `web.py` `serve()` binds 127.0.0.1 only and enforces Host-header checks, a CSRF token injected into `index.html` (`__CSRF_TOKEN__`), Origin checks, and CSP. Static files are served by explicit routes, so a new `static/*.js` file needs a route in `do_GET`. `launcher.py` reuses an already-running identical instance through `/healthz`. The identity is a hash of the config plus a hash of all source, prompt, and static files, so code edits force a fresh instance. If the port is taken it tries the next ones (up to +19) and never kills other programs.
- **Frontend**: plain JS split across `static/app.js` (tabs, shell), `tasks.js`, `topics.js`, and `solutions.js`. There is no bundler. Render mail and AI text as escaped text, never as HTML.

## Testing notes

Tests use `tempfile.TemporaryDirectory()` for `Store` and minimal inline configs (`{"user_email": "me@example.com", "aliases": [], "engine": "rules", ...}`). `test_web.py` starts the real server on a free port by patching `ThreadingHTTPServer`. Outlook and COM are always mocked.
