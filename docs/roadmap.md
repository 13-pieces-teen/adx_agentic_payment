# Arena 402 Roadmap

> Status: current cross-module implementation status and sequencing.

Arena 402 remains a prototype. The repository has three working foundations, but
it does not yet provide the complete product loop.

## Product target

Deliver one bounded RFQ flow for a machine-verifiable digital deliverable:

```text
Mandate -> RFQ -> Offer -> Deal
    -> x402 HTTP payment -> Injective settlement
    -> delivery unlock -> Receipt verification
```

## Completed foundations

- [x] Establish repository-level agent guidance, product scope, roadmap, and
      project skill synchronization.
- [x] Implement the in-memory Python Agent registry, resource listings/intents,
      matching, negotiation state machine, Arena/ELO component, and FastAPI
      wrapper. The Arena/ELO component is not the complete Arena 402 RFQ
      application layer.
- [x] Define Python A2A/payment boundary types, fixtures, and mocks.
- [x] Implement the self-hosted Local Agent Connector beta: outbound pairing
      and WSS, bounded runtime discovery, typed commands, durable receipts and
      events, PostgreSQL persistence, authenticated onboarding, and deployment
      tooling. Runtime task execution remains opt-in and is not the Arena
      business protocol.
- [x] Verify the Injective EVM testnet environment used by the settlement module.
- [x] Implement an EIP-3009-compatible mock stablecoin deployment for testnet use.
- [x] Implement SettlementSDK mock and real adapters.
- [x] Exercise EIP-3009 buyer authorization, project-specific facilitator
      settlement, replay protection, and direct mUSDC transfer on Injective EVM
      testnet.

Module-level settlement evidence, limitations, and verified commands live in
[`../agent-arena/settlement/README.md`](../agent-arena/settlement/README.md).
Connector behavior, security boundaries, and deployment status live in
[`../connector/README.md`](../connector/README.md) and
[`self-hosted-connector-deployment.md`](self-hosted-connector-deployment.md).

## Current gaps

- [ ] The Python matching model has not been adapted to the canonical
      `Mandate` / `RFQ` / `Offer` / `Deal` contract.
- [ ] Connector devices, runtimes, and bindings are not yet linked to the
      canonical Arena 402 Agent identity and Deal lifecycle.
- [ ] Accepted negotiations do not invoke the TypeScript settlement SDK.
- [ ] The repository does not yet expose a standard seller-side x402 HTTP resource
      endpoint with payment requirements, paid retry, and payment response.
- [ ] Payment is not yet bound to a `DeliveryCommitment`.
- [ ] Artifact unlock and buyer-side `Receipt` verification are not implemented.
- [ ] There is no single command that runs the full Arena 402 product loop.

## Next

1. Freeze the first machine-verifiable digital deliverable and its automated
   acceptance check.
2. Define the minimal signed schemas for `Mandate`, `RFQ`, `Offer`,
   `CounterOffer`, `Deal`, `DeliveryCommitment`, and `Receipt`.
3. Add an adapter from the accepted Python negotiation result to an immutable
   `Deal` and SettlementSDK request.
4. Bind Connector-backed runtimes to the canonical Agent identity without
   treating device control state as Deal, payment, or delivery authority.
5. Implement the standard x402 HTTP seller/client exchange around the existing
   EIP-3009 settlement primitive.
6. Implement payment-gated artifact unlock and verify the artifact, terms or
   licence, and Receipt on the buyer side.
7. Add idempotency and failure tests for expired offers, altered deals, invalid
   payments, replayed authorization, retry, and duplicate unlock.
8. Provide one verified demo command and update the root README only after that
   command works from a clean environment.

## Later, not MVP blockers

- TEE-backed key custody and remote attestation;
- ERC-8004 identity or reputation integration;
- on-chain escrow, refunds, disputes, arbitration, or production fee collection;
- multi-round negotiation and advanced matching;
- mainnet or multichain settlement;
- persistent Arena business storage, multi-node Connector operation, production
  observability, and operations.
