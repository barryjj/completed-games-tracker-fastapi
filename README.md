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

## Next steps

Likely next documentation/setup steps:
- refine `AGENTS.md`
- refine `.github/copilot-instructions.md`
- define `project-map.md`
- define initial build/test/dev commands
- choose the first actual implementation slice

## Notes

This project is also partly a learning vehicle:
- for building a usable app around game tracking
- for working with Python and related tooling
- for learning how to get better results from agentic AI in a real repository
