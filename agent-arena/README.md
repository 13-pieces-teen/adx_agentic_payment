# Arena 402 Settlement Prototype Workspace

> Status: Injective EVM EIP-3009 settlement prototype; not a complete x402 or
> Arena 402 product implementation.

The `agent-arena/` directory name is retained for workspace compatibility. Its
current implemented focus is the settlement foundation for **Arena 402**. The
current product is a bounded RFQ and machine-verifiable digital-delivery flow,
described in [`../docs/product.md`](../docs/product.md).

The original
[`AGENT_ARENA_FULL_SPEC.md`](../docs/archive/2026-07-23/AGENT_ARENA_FULL_SPEC.md)
is archived background design material. It is not the default authority for
current implementation status.

## Documentation authority

For work in this directory:

1. source code, tests, deployment records, and verified run evidence;
2. [`settlement/README.md`](settlement/README.md) for implementation status,
   limitations, verified evidence, and module setup;
3. component READMEs under `settlement/` for their local commands and behavior;
4. current root product and roadmap entrypoints for product scope and sequencing;
5. completed files under [`specs/`](specs/README.md) as frozen development and
   acceptance records;
6. archived specifications for historical context only.

The completed `specs/` files are intentionally preserved as written. If their
terminology or planned behavior differs from the current implementation, use the
active Settlement README to describe that difference instead of rewriting the
original spec.

## Current status

| Area | State |
|------|-------|
| Injective EVM testnet | Environment and chainId 1439 path exercised |
| Test asset | EIP-3009-compatible mock USDC deployed for prototype use |
| Buyer authorization | EIP-712/EIP-3009 signing implemented |
| Facilitator | Project-specific verify/settle service implemented for the prototype |
| Settlement SDK | Mock and real TypeScript adapters implemented |
| Direct settlement | Buyer-authorized mUSDC transfer to seller exercised on testnet |
| Standard x402 HTTP flow | Not complete |
| Python matching integration | Not complete; the implemented matching/Arena code currently lives under `../matching/` and `../web/` |
| Delivery commitment, unlock, and Receipt | Not complete |
| TEE, escrow, disputes, real refunds, and production fees | Not implemented product capabilities |

The current direct-settlement path does not lock funds in an escrow contract.
Its refund path must not be presented as a completed on-chain refund mechanism.

## Directory map

| Path | Purpose |
|------|---------|
| `settlement/sdk/` | SettlementSDK interfaces, mock/real adapters, and EIP-3009 signing |
| `settlement/facilitator/` | Project-specific Injective EVM verify/settle service |
| `settlement/contracts/` | Prototype mock stablecoin contract and deployment scripts |
| `settlement/deployments.json` | Non-secret testnet deployment metadata |
| `specs/` | Frozen development decisions, acceptance criteria, and recorded evidence |

There is no `tee/` or Python Arena implementation inside this directory. Those
must not be inferred from the historical monorepo plan.

## Integration boundary

The next integration milestone is:

1. convert an accepted Python negotiation into the canonical Arena 402 `Deal`;
2. invoke SettlementSDK through a stable adapter;
3. wrap settlement in a standard seller-side x402 HTTP challenge and paid retry;
4. bind payment to a `DeliveryCommitment`;
5. release and verify the artifact and `Receipt`.

Until those steps pass together, describe this directory only as an Injective
EVM EIP-3009 settlement prototype. It is not a complete x402 HTTP flow,
TEE/escrow implementation, or Arena 402 product.

## Security

- Use testnet only.
- Never commit `.env`, private keys, seed phrases, or real credentials.
- Require human confirmation before any state-changing transaction.
- Treat `deployments.json` as public metadata; keep signing material outside the
  repository.
