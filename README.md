# ADX — Agent Deal Exchange

ADX is an Agent-native exchange for **bounded RFQs and machine-verifiable digital
delivery**. A buyer delegates a mandate, seller agents return structured offers,
the parties close one limited negotiation, and payment unlocks a deliverable that
the buyer can verify.

ADX is not a generic Agent marketplace, a paid-API directory, or an on-chain
order book. Injective is the concrete testnet settlement layer for the MVP; the
product boundary remains the RFQ and verifiable-delivery protocol.

## Target product loop

```text
Buyer Mandate
    -> RFQ
    -> signed Offer (optionally one CounterOffer)
    -> immutable Deal
    -> DeliveryCommitment
    -> x402 HTTP payment challenge
    -> Injective settlement
    -> artifact unlock + verifiable Receipt
```

The repository does **not** run this full loop end to end yet.

## Current implementation status

| Area | Current state |
|------|---------------|
| Python matching and Arena | Implemented as an in-memory prototype under `matching/`, exposed by `web/api.py` |
| A2A/payment boundary | Python interfaces, fixtures, and mocks exist under `shared/`, `a2a_team/`, and `x402_team/` |
| Injective settlement | Injective EVM testnet prototype under `agent-arena/settlement/`: EIP-3009 authorization, project-specific facilitator, and direct mUSDC settlement |
| Standard x402 HTTP flow | Not complete; the seller-side 402 challenge, paid retry, and resource response are not wired into the product |
| Product integration | Not complete; accepted negotiations do not yet produce the final Deal, payment-gated delivery, and Receipt flow |
| TEE, escrow, disputes, refunds, and production fees | Not implemented as product capabilities |

The existing EIP-3009 settlement prototype validates one payment primitive. It
must not be described as a complete x402 or ADX product implementation.

## Repository map

| Path | Purpose |
|------|---------|
| `matching/` | Agent registry, listings/intents, matching, negotiation, calibration, and ELO/Arena logic |
| `web/api.py` | FastAPI wrapper around the in-memory Python prototype |
| `shared/` | Python payment boundary and shared fixtures |
| `a2a_team/`, `x402_team/` | Integration mocks |
| `agent-arena/settlement/` | Injective EVM EIP-3009 settlement prototype |
| `docs/product.md` | Current product scope |
| `docs/roadmap.md` | Current cross-module status and next steps |
| `docs/injective/` | Fixed snapshots of external Injective documentation; reference only |

## Matching prototype quick start

The repository-provided setup script is intended to configure the local Git hook
path and run an in-memory matching/Arena smoke check. Run it from a Bash
environment:

```bash
./setup.sh
```

To start the existing FastAPI wrapper:

```bash
pip install fastapi uvicorn
python3 -c 'from web.api import create_app; import uvicorn; uvicorn.run(create_app(), port=8000)'
```

This starts the Python prototype only. It does not start the settlement
facilitator or a complete ADX demo.

These are repository-existing commands. This documentation update did not
reinstall dependencies or re-run them from a clean environment; clean-environment
demo verification remains roadmap work.

For settlement status and module-specific setup, read
[`agent-arena/README.md`](agent-arena/README.md) and
[`agent-arena/settlement/README.md`](agent-arena/settlement/README.md).

## Repository harness

- Agent rules: [`AGENTS.md`](AGENTS.md)
- Product scope: [`docs/product.md`](docs/product.md)
- Implementation roadmap: [`docs/roadmap.md`](docs/roadmap.md)
- Shared project skills: [`.agents/skills/`](.agents/skills/)

Codex reads `.agents/skills/` directly. Claude Code users can synchronize the
same project skills locally:

```bash
python scripts/sync_skills.py --write
python scripts/sync_skills.py --check
```

Treat `.agents/skills/` as the editable source. Do not edit generated
project-managed copies under `.claude/skills/`. The generated directory appears
after the first successful `--write` synchronization.

## Git safety

Running `./setup.sh` configures `core.hooksPath` to use
`.githooks/pre-commit`. The repository also contains
`.claude/guard_file_owner.py`. These are safeguards; preserve unrelated work
and follow [`AGENTS.md`](AGENTS.md) when editing.

## External links

- A2A Protocol: https://github.com/a2aproject/A2A
- x402: https://github.com/coinbase/x402
- Injective: https://injective.com
