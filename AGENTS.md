# Project Agent Guide

## Project

This repository is an early-stage hackathon prototype for **Arena 402**:
a bounded RFQ workflow for machine-verifiable digital delivery.

The current repository contains two implemented but not yet integrated foundations:

- an in-memory Python matching, negotiation, and Arena prototype;
- an Injective EVM testnet EIP-3009 direct-settlement prototype.

The standard x402 HTTP payment flow and the complete
`RFQ -> Deal -> Payment -> Delivery -> Receipt` product loop are not complete yet.

## Read first and documentation authority

- Setup and repository orientation: `README.md`
- Current product scope: `docs/product.md`
- Current implementation status and next steps: `docs/roadmap.md`
- Module implementation details: the module-local `README.md`, spec index, source,
  tests, and verified run artifacts
- Project skills: `.agents/skills/`

Apply authority by question rather than using one global precedence order:

- use root `README.md` for setup and repository orientation;
- use `docs/product.md` for current product scope and non-goals;
- use `docs/roadmap.md` for cross-module implementation status and sequencing;
- use source, tests, verified run artifacts, approved active specs, and module
  `README.md` files for module behavior and implementation claims;
- use archived or background documents for historical context only.

If an implementation-status claim conflicts with code or verified evidence,
the evidence wins and the current status document must be corrected. Code does
not override the product scope defined in `docs/product.md`.

Completed specifications under `agent-arena/specs/` are frozen development
records. They preserve the decisions, acceptance criteria, terminology, and
evidence used by the original implementation and must not be retroactively
rewritten to match later documentation. When a frozen spec differs from current
behavior, explain the difference in the active module `README.md` or
`docs/roadmap.md`; create a new spec only after its scope is approved.

Additional context boundaries:

- `docs/injective/` contains fixed external documentation snapshots. A snapshot is
  reference material, not evidence that Arena 402 has adopted or implemented
  that design.
- `产品描述.docx` and `ISEK解读与ADX2025参赛策略.md` are background materials.
  Do not load or treat them as current project authority by default.

## Repository harness

- Keep `README.md`, `docs/product.md`, `docs/roadmap.md`, and active module
  entrypoints aligned with the authority boundaries above.
- Preserve existing design documents; add new documents only for a clear, maintained purpose.
- Keep `.agents/` limited to shared project skills; do not add task files yet.
- Treat `.agents/skills/` as the only editable source for project skills.
- Do not edit generated copies under `.claude/skills/`; that directory is
  created by the synchronization command and may not exist before the first run.
- After changing a skill, run:

  ```text
  python scripts/sync_skills.py --write
  python scripts/sync_skills.py --check
  ```

## Documentation lifecycle

- Update current entrypoints in place. Do not archive `README.md`,
  `docs/product.md`, `docs/roadmap.md`, or an active module entrypoint merely
  because its content is stale.
- Discuss the proposed archive target, reason, and replacement with a human and
  receive confirmation before moving any document into the archive.
- After confirmation, preserve superseded documents under
  `docs/archive/YYYY-MM-DD/<original-relative-path>`.
- Add an entry to `docs/archive/README.md` with the archive date, reason, and
  links to current replacements. Preserve the archived file's historical
  content unless the human explicitly approves an inline notice. Find and
  repair every inbound link after the move.
- Archived documents are non-current by default. Do not use them as
  implementation authority or add new design decisions to them.
- If code, commands, interfaces, or repository layout invalidate a current
  document, update that document in the same scoped change or explicitly mark the
  unresolved mismatch. This rule does not authorize edits to frozen completed
  specs.
- Before adding a new design document, state the unique maintained purpose it
  owns and which document, if any, it supersedes.

## Working rules

- Keep changes small and scoped to the requested task.
- Preserve unrelated work and avoid broad refactors.
- Add only commands that have been verified in this repository.
- Update `product.md` or `roadmap.md` only when their information changes.
- Never commit secrets, wallet keys, seed phrases, or real payment credentials.
- Use testnet by default; require human confirmation before a state-changing transaction.
