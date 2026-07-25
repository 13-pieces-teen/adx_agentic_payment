# Arena 402 Settlement Prototype Workspace

> Status: Injective EVM EIP-3009 direct-relay prototype; not a complete x402
> HTTP implementation or complete Arena 402 game.

The `agent-arena/` directory name is retained for compatibility. Its current
implemented focus is the payment foundation for Arena 402: an accepted game
negotiation should eventually become a point-to-point testnet USDC settlement,
and game inventory should move only after chain confirmation.

Current product rules are defined in
[`../docs/game-design.md`](../docs/game-design.md). The integration contract is
[`../docs/arena-settlement-integration.md`](../docs/arena-settlement-integration.md).

## Documentation authority

For work in this directory:

1. source, tests, deployment metadata, and verified run evidence;
2. [`settlement/README.md`](settlement/README.md) for current module behavior;
3. component READMEs under `settlement/`;
4. current root game, product, and roadmap documents;
5. completed files under [`specs/`](specs/README.md) as frozen development
   records;
6. archived documents for historical context only.

The frozen specs preserve the terminology and acceptance criteria used when the
prototype was built. Do not rewrite them when product framing changes; explain
current differences in active READMEs.

## Current status

| Area | State |
|------|-------|
| Injective EVM testnet | Chain ID 1439 path exercised |
| Test asset | EIP-3009-compatible mUSDC deployed; test-only public-faucet token |
| Buyer authorization | EIP-712/EIP-3009 signing implemented |
| Facilitator | Project-specific verify/settle relay implemented |
| Settlement SDK | Mock and real TypeScript adapters implemented |
| Direct settlement | Buyer-authorized mUSDC transfer to seller exercised |
| Standard HTTP x402 | Not complete |
| Game integration | Single-payment mode persists immutable SettlementIntent snapshots after accepted negotiations |
| Inventory commit | Read-only confirmation recovery gates idempotent Arena cash/holding commit |
| TEE, escrow, refund, dispute, production fee | Not implemented product capabilities |

The current direct-settlement path does not lock funds in an escrow contract.
Its local `refund()` status must not be represented as an on-chain reversal.

## Directory map

| Path | Purpose |
|------|---------|
| `settlement/sdk/` | SettlementSDK interfaces, mock/real adapters, and EIP-3009 signing |
| `settlement/facilitator/` | Project-specific Injective EVM verify/settle service |
| `settlement/contracts/` | Prototype mock stablecoin and deployment scripts |
| `settlement/deployments.json` | Non-secret testnet deployment metadata |
| `specs/` | Frozen implementation decisions and evidence |

There is no current game engine or TEE implementation in this directory.

## Remaining integration boundary

The current game bridge freezes one unique SettlementIntent, binds its payment
snapshot, records operator approval/submission, resolves submitted/unknown
states through read-only chain evidence, and commits Arena cash/goods exactly
once after confirmation. The remaining product boundary is a bounded,
revocable PaymentMandate with `reserve / consume / release`, unattended signer
isolation, and a freshly approved end-to-end testnet acceptance run.

Continue to describe this workspace as an EIP-3009 settlement prototype, not a
standard x402 HTTP flow or a complete unattended payment authority.

## Security

- Use testnet only.
- Never commit `.env`, private keys, seed phrases, API keys, or real credentials.
- Require human confirmation before any state-changing transaction.
- Treat `deployments.json` as public metadata and keep signing material outside
  the repository.
