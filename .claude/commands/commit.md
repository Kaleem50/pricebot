---
allowed-tools: Bash(git add:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git commit:*), Bash(git push:*)
argument-hint: "[optional scope or hint]"
description: Stage all changes, generate a Conventional Commit message, commit and push
model: claude-haiku-4-5-20251001
---

# Context (auto-injected)
- Branch: !`git branch --show-current`
- Status: !`git status --porcelain=v1`
- Recent commits: !`git log --oneline -5`
- Diff: !`git diff HEAD`

# Task
1. Analyze the diff above
2. Generate a Conventional Commit message:
   - Format: `type(scope): subject` — subject ≤50 chars
   - Types: feat | fix | refactor | chore | docs | test | perf
   - If $ARGUMENTS provided, use as scope/summary hint
3. Stage all: `git add -A`
4. Commit with generated message (no co-author footer)
5. Push to current branch
6. Print the commit message and success/failure of each step

## Commit message heuristics
- feat → new capability added
- fix → bug corrected
- refactor → restructured without behavior change
- chore → deps, config, tooling
- perf → speed/cost improvement (relevant for Batch API tuning)
- docs → markdown/comments only