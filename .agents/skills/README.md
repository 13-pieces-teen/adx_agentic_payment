# Project Skills

This directory is the canonical, version-controlled source for project-specific Agent Skills.

## Rules

- Put each skill in `.agents/skills/<skill-name>/SKILL.md`.
- Keep the directory name identical to the `name` field in `SKILL.md`.
- Describe both what the skill does and when it should trigger.
- Add `scripts/`, `references/`, or `assets/` only when the skill uses them.
- Do not add secrets, wallet material, personal paths, or transient session state.
- Edit skills here only. Do not edit generated copies under `.claude/skills/`.

## Synchronize

Run after cloning, pulling a skill change, or editing a skill:

```text
python scripts/sync_skills.py --write
python scripts/sync_skills.py --check
```

Codex discovers this directory directly. The sync command installs project-managed copies into `.claude/skills/` for Claude Code while preserving unrelated local Claude skills.
