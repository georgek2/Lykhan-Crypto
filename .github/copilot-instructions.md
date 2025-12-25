# Copilot / AI Agent Instructions for `lykhan`

Quick, actionable guidance to help an AI coding agent be productive in this repository.

1) Repo overview
- Small Django project scaffold with a single app: `agent`.
- Django settings live in `project/settings.py` (generated for Django 6.0).
- Root URL routing: `project/urls.py` includes `agent.urls` at `''`.
- There is also a tiny standalone script `main.py` (prints a greeting) — not part of the Django app.

2) Key files and what they mean
- `manage.py`: standard Django entrypoint — use for running server, migrations, and tests.
- `project/settings.py`: database (sqlite3), installed apps, templates, middleware.
- `project/urls.py`: top-level URL conf — routes root to `agent.urls` and exposes `/admin/`.
- `agent/urls.py`: maps `''` to `views.home`.
- `agent/views.py`: `home(request)` returns `render(request, 'agent/home.html')` (note: template name mismatch, see below).
- Templates: `agent/templates/agent/agent.html` exists (contains "Lykhan Crypto" text).

3) Important notes / gotchas discovered in the codebase
- Template mismatch: `views.home` renders `agent/home.html` but the repo only contains `agent/agent.html`. When changing or adding templates, follow the `agent/templates/agent/` directory pattern.
- No `requirements.txt` or pinned dependencies in `pyproject.toml`. The Django settings indicate Django 6.0; prefer installing `django==6.*` for local work.
- `pyproject.toml` claims Python >=3.12; verify the runtime before running tests.
- Database: `db.sqlite3` is used by default (no special setup needed other than migrations).

4) Typical developer workflows & exact commands
Install and run (recommended minimal steps):
```bash
python -m venv .venv
source .venv/bin/activate
pip install "django>=6.0,<7.0"
python manage.py migrate
python manage.py runserver
```
Run tests:
```bash
source .venv/bin/activate
python manage.py test
```
Create superuser:
```bash
python manage.py createsuperuser
```

5) Patterns and conventions to follow (project-specific)
- App-level templates live under `agent/templates/agent/` and are referenced as `agent/<name>.html` in `render()` calls.
- URLs: `agent/urls.py` exposes views at the project root — prefer small, focused URL patterns.
- Keep view logic thin: current `views.home` only renders a template. If you add logic, prefer moving business logic to helper modules or `agent/services.py` to keep views testable.

6) Integration points & external dependencies
- No external APIs or third-party integrations are present in the code. If you add integrations (crypto exchanges, websockets, etc.), register configuration in `project/settings.py` and add secrets to environment variables (do NOT commit them).

7) Concrete examples to edit/fix
- Fix the template mismatch: either
  - change `agent/views.py` to `return render(request, 'agent/agent.html')`, or
  - add `agent/templates/agent/home.html` based on `agent/agent.html`.
- If adding dependencies, update `pyproject.toml` or add `requirements.txt` and document install steps in `README.md`.

8) What the AI should not assume
- Don't assume production settings — `DEBUG=True` and an insecure SECRET_KEY are present; treat this repo as a development sandbox.
- Don't assume tests exist for new features — add focused tests when implementing behavior changes.

9) Quick PR checklist for the AI to follow
- Small focused changes with a short description.
- Run `python manage.py test` locally before proposing a PR.
- If you add a template or path, verify `render()` uses the correct relative path under `agent/templates/agent/`.

If anything here is unclear or you'd like the file to include automatic dev container setup, CI commands, or example tests, tell me which area to expand and I will iterate.
