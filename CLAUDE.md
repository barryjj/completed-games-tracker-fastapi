# CLAUDE.md

Agent instructions for Claude Code. Read this before doing anything.

## Project

Personal game completion tracker. FastAPI backend, Jinja2/HTMX frontend, SQLite via SQLAlchemy, Alembic migrations. The stack is confirmed — do not propose replacing it.

- Entry point: `backend/main.py`
- Run: `python -m uvicorn backend.main:app --reload`
- Tests: `.venv/bin/pytest backend/ -q`
- Always use the project-local `.venv` — never global Python.

## Git rules

- **Never commit, push, rebase, or force-push without explicit per-action approval.**
- Never work directly on `main` or `develop`.
- Never push to `main` — not even after the user approves a rebase. Push is a separate action requiring separate approval.
- All work goes on a feature branch via pull request.
- When a destructive git operation is needed (force push, rebase, history rewrite), explain what it does and provide the command. Stop there — let the user run it.

## Branching

- Branch from `main` unless explicitly told otherwise.
- Prefer not stacking branches on unmerged feature branches. If stacking is necessary, rebase the child branch onto `origin/main` after the parent PR lands before the child PR is merged — rebase merges rewrite commit hashes, so the child branch must be updated or it will get phantom conflicts.

## Code

- Small, reviewable diffs. No speculative code for future features.
- No new dependencies without approval.
- No top-level directory restructuring without approval.
- Match existing patterns before introducing new ones.
- Tests live in `backend/test_*.py` and use pytest with in-memory SQLite. Run them before committing.

## UI

- Theme: Catppuccin Mocha (dark default) / Latte (light). OS-default via `prefers-color-scheme`; user can override via localStorage toggle.
- No emoji in UI chrome.
- HTMX for dynamic interactions — no JavaScript frameworks.

## What not to do

- Do not push to protected branches.
- Do not open multiple PRs for the same work.
- Do not reopen a PR after closing it — open a new one with a clear name.
- Do not fabricate API behavior or claim validation happened if it did not.
