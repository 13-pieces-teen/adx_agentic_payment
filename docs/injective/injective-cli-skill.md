# Injective CLI Agent Skill

> An AI agent skill for executing Injective CLI commands programmatically via natural language.

- Official source: [https://docs.injective.network/developers-ai/injective-cli-skill](https://docs.injective.network/developers-ai/injective-cli-skill)
- Last source change: `2026-03-31`
- Snapshot captured: `2026-07-23`
- Upstream revision: [`1a31f4937cce`](https://github.com/InjectiveLabs/injective-docs/commit/1a31f4937cce679b1bf5542743dc1e223289d248)

---

Use the `injectived` binary to query and transact against an Injective chain
with consistent wallet handling, endpoint selection, and gas configuration.

## Installation

Installing skill:

```bash
uvx upd-skill InjectiveLabs/injective-cli
```

Installing skill globally:

```bash
uvx upd-skill InjectiveLabs/injective-cli --global
```

Install via NPX:

```bash
npx skills add https://github.com/InjectiveLabs/agent-skills --skill injective-cli
```

## Usage

After installing the skill, enter your prompt into the harness to use this skill.
