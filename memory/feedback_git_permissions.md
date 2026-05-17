---
name: Git push permissions
description: User has strict rules about pushing to main/develop and force pushing
type: feedback
---

Never push directly to main or develop, and never force push to any branch, without explicit user authorization for that specific action.

**Why:** User has guardrails (agents.md, copilot-instructions.md) that restrict agents from working directly on protected branches. They want to control pushes and force pushes themselves.

**How to apply:** When a fix requires pushing to main or force pushing, prepare the commands and explain what they do, then stop and let the user run them. Asking "should I go ahead?" and getting a "yes" for a rebase does NOT authorize the subsequent force push — those are separate actions requiring separate confirmation.
