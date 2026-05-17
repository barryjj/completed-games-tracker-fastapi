---
name: Git push permissions and branching discipline
description: Always pull main before branching; main is protected, feature branches are not
type: feedback
---

**Always run `git pull origin main` immediately before `git checkout -b <branch>`. No exceptions. This is the single most repeated mistake in this project.**

**Why:** Branches created from stale local main get duplicate commits when the user merges a PR via rebase, causing conflicts on every subsequent PR. This has happened on 14+ PRs.

**How to apply:** Branch creation is always two commands, never one:
```
git pull origin main
git checkout -b feature/my-branch
```

---

Push feature branches and open PRs freely. Always pass `--head <branch>` to `gh pr create` — without it, gh uses the cwd's git context which may be a worktree on a different branch.

Never push to `main` or force-push `main` without explicit per-action authorization.
