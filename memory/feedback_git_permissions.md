---
name: Git push permissions
description: Push/PR on feature branches is fine; main is the only protected surface
type: feedback
---

Push feature branches and open PRs freely — that is normal expected workflow.

**The hard rules:**
- Never push to `main` or force-push `main` without explicit per-action authorization
- Always pass `--head <branch>` to `gh pr create` — without it, gh picks up the cwd's git context which may be a worktree on a different branch

**Why:** User got burned by agents pushing to main and by a bad PR that pointed at the wrong branch because `gh pr create` was run from a worktree directory.

**How to apply:** After finishing work on a feature branch, push it and open the PR with `--head`. That's correct. Only stop and ask when the target is `main` itself.
