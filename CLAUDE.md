# SONYA Project Instructions

You are working on SONYA production repository.

Before making changes:
- Read docs/SONYA_AUDIT.md
- Check existing architecture before editing
- Prefer minimal patches over rewrites

## Workflow

- Group changes into logical blocks.
- Do not ask permission for:
  - reading files
  - grep/search
  - git status
  - running tests
- Before large changes:
  - provide short plan
- After changes:
  - run relevant checks
  - summarize what changed

## Git

- Never push without explicit user command.
- Before commit:
  - show short diff summary.
- Keep commits focused.

## Architecture

Frontend:
- Vanilla JS
- No build system

Backend:
- FastAPI
- PostgreSQL

Storage:
- S3 compatible storage

GPU:
- vast.ai workers
- dispatcher + orchestrator + worker pipeline

Do not break production flow without checking dependencies.

## Priority

Work order:
1. P0 production blockers
2. P1 reliability/security
3. P2 cleanup/refactoring

Already fixed issues:
- vast.ai worker claim bug
- frontend session-status authentication bug
- pytest infrastructure
- CI test workflow
