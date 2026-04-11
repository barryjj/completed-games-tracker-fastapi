# AGENTS.md

## Purpose

This repo is for a personal game completion tracker.

Likely goals:
- track backlog, in-progress, and completed games
- support Steam and PSN data import/sync
- build incrementally with AI help
- keep the code understandable and reviewable

Do not assume the full architecture is decided unless the repo says so.

## Default behavior

- Work in small, reviewable steps.
- Prefer minimal diffs.
- Read existing docs before changing code.
- Treat undocumented architecture decisions as undecided.
- Propose before expanding structure.
- When in doubt, ask or give options.

## Do not do these things

- Do not invent large directory structures.
- Do not add frameworks or dependencies without approval.
- Do not refactor unrelated files.
- Do not fabricate API behavior or integration details.
- Do not claim code was validated unless it was actually validated.
- Do not make speculative changes “for later.”

## Protected control files

Do not edit these without explicit approval:
- `AGENTS.md`
- `.github/copilot-instructions.md`
- `.github/instructions/*.instructions.md`
- any future file whose main purpose is to control AI or agent behavior

If a change to a protected control file seems useful:
1. explain why
2. show the proposed change
3. wait for approval

## Git workflow rules

- Do not commit directly to `main`.
- Do not merge anything into `main`.
- Do not create, rename, or delete branches without approval.
- Do not commit, push, merge, rebase, or squash without approval.
- For non-trivial work, prefer a branch and pull request workflow.
- Keep branches and PRs small and focused.
- One branch should address one concern.
- If a task is too large for one focused PR, propose how to split it first.

If git actions are needed:
1. propose the branch name
2. describe the planned change
3. wait for approval before creating commits or pushing

## Ask first

Ask before:
- changing protected control files
- choosing or replacing major frameworks
- choosing the final database or ORM
- adding auth flows
- adding dependencies
- restructuring top-level directories
- adding CI/CD, Docker, or deployment config
- adding background jobs or schedulers
- deleting files
- editing secrets, env files, or credential storage

## Allowed without asking

You may:
- read files
- inspect repo structure
- propose plans
- improve ordinary documentation
- make small changes in already agreed areas
- create minimal placeholders only when explicitly asked

Ordinary documentation does not include protected control files.

## Change style

- Prefer boring, maintainable solutions.
- Prefer explicit names over clever abstractions.
- Avoid overbuilding.
- Keep modules focused.
- Keep code readable for a human learner.
- Optimize for clarity first, speed second.

## Validation

If commands exist, use the smallest relevant validation step first.

Report:
- what you changed
- what you validated
- what you did not validate
- any assumptions or follow-up needed

If nothing can be validated yet, say that plainly.

## Docs roles

- `README.md` = human overview and setup
- `AGENTS.md` = agent behavior rules
- `.github/copilot-instructions.md` = Copilot-specific guidance
- `docs/agent-guides/project-map.md` = evolving repo map
- `docs/agent-guides/build-test-verify.md` = actual commands and checks

Avoid duplicating long instructions across files.

## Task pattern

For non-trivial work:
1. restate the task briefly
2. identify assumptions
3. propose a short plan
4. make the agreed change
5. summarize the result
