# completed-games-tracker-fastapi

A personal game completion tracker project.

The goal is to build a local-first app for tracking backlog, in-progress, and completed games, with likely support for importing or syncing data from services such as Steam and PlayStation Network (PSN).

## Status

Early scaffolding / planning stage.

This repository currently contains project structure and AI-assistance control files.
The application itself is not built yet.

## Goals

- Track games by status, such as backlog, in-progress, and completed
- Explore Steam and PSN integration for importing or syncing data
- Build the project incrementally instead of generating a full app all at once
- Keep the codebase understandable and maintainable
- Use AI tooling in a controlled, reviewable way

## Planned direction

The exact architecture is not final yet.

Current likely direction:
- Python-based backend, likely FastAPI
- polished frontend later
- local development on macOS in VS Code
- project-local Python `.venv` at the repo root
- branch / pull request workflow for AI-assisted changes

These are working assumptions, not final commitments.

## Repository docs

- `AGENTS.md` — repo-wide agent behavior rules
- `.github/copilot-instructions.md` — Copilot-specific instructions
- `docs/agent-guides/project-map.md` — evolving repo map
- `docs/agent-guides/build-test-verify.md` — build/test/run commands once they exist

## Working approach

This repo is being set up to support AI-assisted development without letting the tooling run wild.

General approach:
- keep changes small
- prefer branch + pull request workflow
- review non-trivial changes before they land
- protect AI control files from unapproved edits
- avoid inventing architecture that has not been agreed on

## Current state

Right now, this repo is mainly:
- scaffolding
- documentation
- workflow setup
- guardrails for future AI-assisted implementation
- minimal backend health check

## Current backend slice

Current confirmed backend details:
- app entry point: `backend/main.py`
- run command: `python -m uvicorn backend.main:app --reload`
- health check: `GET /health`
- local health URL: `http://127.0.0.1:8000/health`

Expected response:

```json
{"status":"ok"}
