# Copilot instructions

## What this repo is

This repo is for a personal game completion tracker.

Current likely goals:
- track backlog, in-progress, and completed games
- support Steam and PSN data import/sync
- build incrementally
- keep the code understandable and reviewable

Do not assume the full stack is finalized unless the repo explicitly says so.

## Read first

Before proposing or editing code, read:
- `AGENTS.md`
- `README.md`
- `docs/agent-guides/project-map.md`
- `docs/agent-guides/build-test-verify.md`

If those files are sparse, treat missing decisions as undecided.

## How to work here

- Work in small, reviewable steps.
- Prefer minimal diffs.
- Restate the task briefly before non-trivial changes.
- Identify assumptions before coding.
- If architecture is unclear, propose options instead of guessing.
- Extend agreed structure; do not invent new large structures.

## Change boundaries

- Do not add frameworks or dependencies without approval.
- Do not restructure top-level directories without approval.
- Do not refactor unrelated files.
- Do not add speculative code for future features.
- Do not fabricate external API behavior.
- Do not claim validation happened if it did not.

## Protected files

Do not edit these without explicit approval:
- `AGENTS.md`
- `.github/copilot-instructions.md`
- `.github/instructions/*.instructions.md`
- any future file whose purpose is to control AI behavior

If a protected file should change:
1. explain why
2. show the proposed change
3. wait for approval

## Git rules

- Do not commit directly to `main`.
- Do not merge to `main`.
- Do not create branches, commit, push, rebase, or squash without approval.
- For non-trivial work, prefer a branch and pull request workflow.
- Keep changes small enough for review.

## Python environment

- For Python work, use the project-local `.venv` at the repository root.
- Do not assume globally installed packages are the correct environment.
- Prefer commands that run inside the active `.venv`.
- Install Python dependencies from `backend/requirements.txt` unless the repo is later changed to use a different dependency manager.

## Current backend slice

Current confirmed backend details:
- app entry point: `backend/main.py`
- run command: `python -m uvicorn backend.main:app --reload`
- health check: `GET /health`
- local health URL: `http://127.0.0.1:8000/health`

## Coding style

- Prefer boring, maintainable solutions.
- Prefer explicit naming over clever abstractions.
- Keep modules focused.
- Avoid overbuilding.
- Optimize for readability first.
- Match existing patterns before introducing new ones.

## Validation

Use the smallest relevant validation step first.

When reporting work, state:
- what changed
- what was validated
- what was not validated
- what assumptions remain

If there are no working commands yet, say that plainly.

## Documentation

Update docs when agreed structure or behavior changes.

Use these roles:
- `README.md` for human overview and setup
- `AGENTS.md` for agent behavior rules
- `docs/agent-guides/project-map.md` for the evolving repo map
- `docs/agent-guides/build-test-verify.md` for actual commands and checks

Avoid duplicating long instructions across files.

## If the repo is still early

If the repo is mostly scaffolding:
- do not pretend the architecture is already settled
- propose the next smallest useful step
- prefer plans and file-by-file scaffolding over full app generation
