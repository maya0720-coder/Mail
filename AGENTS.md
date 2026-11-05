# Repository Guidelines

## Project Structure & Module Organization

This Python 3.11+ Windows application collects and analyzes mail locally. `app.py` is the CLI entry point; `mailbrain/` contains collection, pipeline, storage, policy, reporting, search, and web modules. The browser interface lives in `static/`, prompt templates in `prompts/`, and architecture and operating notes in `docs/`. Tests are in `tests/` and use Python's built-in `unittest`. `data/`, `config.json`, and generated output are local runtime files; do not commit them.

## Build, Test, and Development Commands

Run `setup.bat` on Windows to create the Python 3.11 `.venv311` environment and install Outlook and PDF dependencies. `run.bat` checks setup before launch. For manual setup:

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Run the local web app with `.\.venv311\Scripts\python.exe app.py serve`, or use `run.bat` on Windows. For demo data, run `.\.venv311\Scripts\python.exe app.py --config config.demo.json serve`. Run the full suite with:

```powershell
.\.venv311\Scripts\python.exe -m unittest discover -s tests -v
```

Install `requirements-collectors.txt` only when working on the optional browser-based groupware collector.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, `snake_case` for modules, functions, and variables, and `PascalCase` for classes. Keep collector-specific integrations in `mailbrain/collectors.py` or focused modules, and keep browser assets under `static/`. Avoid adding dependencies when the standard library or current packages suffice. No formatter or linter is configured in the repository.

## Testing Guidelines

Add or update `unittest` cases in the relevant `tests/test_*.py` module. Use temporary directories and mocks for storage and external integrations; tests should not depend on personal mail, Outlook state, or live services. Run the full discovery command above after changes that affect shared behavior.

## Commit & Pull Request Guidelines

The existing history uses short, descriptive messages, including Korean text, without a fixed prefix convention. Follow that pattern: describe the change clearly and keep each commit focused. Pull requests should explain behavior changes, list validation performed, reference related issues when applicable, and include screenshots for visible UI changes. Never include real mail, credentials, `config.json`, or generated runtime data.

## Security & Configuration

Use `config.example.json` as the template and keep personal settings local in ignored `config.json`. Treat collected mail and the `data/` directory as sensitive. Keep demo and test workflows pointed at demo or temporary data directories.

