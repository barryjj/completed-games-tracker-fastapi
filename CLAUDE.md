# CLAUDE.md

Agent instructions for Claude Code. Read this before doing anything.

## Project

Personal game completion tracker. FastAPI backend, Jinja2/HTMX frontend, SQLite via SQLAlchemy, Alembic migrations. The stack is confirmed — do not propose replacing it.

- Entry point: `backend/main.py`
- Run: `python -m uvicorn backend.main:app --reload`
- Tests: `.venv/bin/pytest backend/ -q`
- Always use the project-local `.venv` — never global Python.

## Git rules

- Never work directly on `main` or `develop`.
- Never push to `main` or force-push `main` for any reason — provide the command and let the user run it.
- Feature branches: push freely, open PRs freely.
- Force-pushing a feature branch after a rebase is fine — explain what changed.
- Always pass `--head <branch>` explicitly to `gh pr create`.

## Branching

- **Always `git pull origin main` immediately before `git checkout -b <branch>`. No exceptions.**
- Branch from `main` unless explicitly told otherwise.
- Prefer not stacking branches on unmerged feature branches. If stacking is necessary, rebase the child branch onto `origin/main` after the parent PR lands before the child PR is merged — rebase merges rewrite commit hashes, so the child branch must be updated or it will get phantom conflicts.
- Keep no more than 5 feature branches on GitHub at a time. When creating a new branch, delete the old branch locally and remotely if it has been merged.

## PR workflow

1. Feature branch → work → commit → push → open PR → **immediately `git checkout main`**
2. Stay on `main` while the PR is open. If fixes are needed: checkout the feature branch, fix, push, return to `main`.
3. When the user confirms the PR is merged: `git pull origin main` → create next feature branch → delete old branch locally and remotely.

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
