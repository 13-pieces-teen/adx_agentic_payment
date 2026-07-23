# Project Agent Guide

## Project

This repository is an early-stage hackathon prototype for Agent-to-Agent payments using x402 and Injective.

## Read first

- Product outline: `docs/product.md`
- Current direction: `docs/roadmap.md`
- Project skills: `.agents/skills/`
- Setup commands: `README.md`

## Repository harness

- Use `docs/product.md` and `docs/roadmap.md` as the lightweight harness entrypoints.
- Preserve existing design documents; add new documents only for a clear, maintained purpose.
- Keep `.agents/` limited to shared project skills; do not add task files yet.
- Treat `.agents/skills/` as the only editable source for project skills.
- Do not edit generated copies under `.claude/skills/`.
- After changing a skill, run:

  ```text
  python scripts/sync_skills.py --write
  python scripts/sync_skills.py --check
  ```

## Working rules

- Keep changes small and scoped to the requested task.
- Preserve unrelated work and avoid broad refactors.
- Add only commands that have been verified in this repository.
- Update `product.md` or `roadmap.md` only when their information changes.
- Never commit secrets, wallet keys, seed phrases, or real payment credentials.
- Use testnet by default; require human confirmation before a state-changing transaction.
