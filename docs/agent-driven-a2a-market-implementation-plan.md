# Agent-Driven A2A Market Implementation Plan

> Status: approved target; Phase A persistence, projection, opt-in round
> orchestration, Phase B Hosted/Local task support, and the Phase C
> payment-disabled Deal foundation were implemented on 2026-08-04. Current
> Game remains on `fcfs.v1`. A real Claude Code + Codex Connector-only game
> completed intent/discovery with no compatible price interval; full
> real-Agent Engagement/negotiation/Deal evidence is not complete.
>
> Approved direction: Arena 402 is an Agent-native market. Agents discover
> counterparties, choose whom to approach, select which request to engage, and
> negotiate the price. Arena is the authenticated Gateway, policy enforcement
> point, durable protocol authority, and settlement coordinator. It must not
> choose a counterparty or synthesize an Agent's market decision.

## 1. Why this plan exists

The current game has two related limitations:

1. `buy | sell | pass` results are put into centrally controlled pools and
   Arena creates FCFS Pairings before either Agent chooses its counterparty.
2. The current "Arena A2A" path is a controlled task/result transport. The
   future Native A2A Endpoint is only a Runtime Adapter. Neither path currently
   gives the participating Agent an Agent-driven discovery and selection step.

Improving FCFS or replacing it with centralized maximum matching can improve
fill rate, but it does not solve the product problem. A more efficient central
matcher is still a central matcher.

The target business flow is therefore:

```text
event broadcast
  -> Agent publishes a buy/sell intent or passes
  -> Agent receives a frozen public market directory
  -> Agent chooses counterparties and sends bounded RFQs
  -> counterparty Agent chooses which RFQ to engage
  -> both Agents negotiate through Arena A2A Gateway
  -> Agent accept freezes one immutable Deal
  -> Settlement validates the PaymentMandate and submits payment
  -> chain confirmation gates Arena's idempotent inventory commit
```

## 2. Non-negotiable authority boundaries

### 2.1 Agent authority

The active Game Agent owns every economic choice:

- whether to buy, sell, or pass;
- which good to advertise;
- the public listing/reference price;
- the private buyer ceiling or seller floor;
- which visible counterparties to contact;
- the opening proposal and public message;
- which inbound request to engage;
- every `propose | accept | reject` negotiation action;
- whether to use a remaining fallback after a busy, rejected, or timed-out
  counterparty.

Every authoritative market transition must reference the exact sanitized
`AgentTaskResult` that requested it. An Arena timer, worker, acknowledgement,
Provider success, Connector receipt, or A2A task success cannot stand in for
that result.

### 2.2 Arena authority

Arena owns:

- Game membership and frozen Runtime/config snapshots;
- the public market directory and visibility policy;
- Gateway relay, ordering, sanitization, persistence, and audit;
- phase windows, absolute deadlines, and bounded request/turn counts;
- schema, ownership, asset, inventory, cash, and private-limit validation;
- atomic Participant round-slot and asset reservation;
- canonical negotiation state and immutable Deal creation;
- the Result Sink/Consumer and deterministic Deadline Finalizer;
- the handoff to Settlement and confirmation-gated inventory commit.

Arena may reject an illegal candidate action. It may never choose a target,
select an RFQ, invent a proposal, or accept a quote for an Agent.

### 2.3 Settlement authority

Settlement continues to own:

- PaymentMandate `reserve / consume / release`;
- payment payload construction and submission;
- confirmation recovery;
- the handoff to Arena's idempotent inventory commit.

`engaged`, `negotiating`, and `accepted_pending_settlement` are not completed
trades. Inventory moves only after the exact chain transfer is confirmed.

## 3. State-machine validation versus real Agents

An in-memory deterministic state machine is allowed only for:

- protocol and invariant tests;
- Fake E2E;
- replaying a fixed set of Agent candidate actions;
- concurrency, idempotency, timeout, and recovery verification;
- serving as an executable specification for PostgreSQL transitions.

It is not an Agent, must not contain a buyer/seller selection strategy, and
must not be deployed as the production decision maker.

The production path must obtain all economic choices from one of:

- a real Hosted Agent through the Hosted Runtime;
- a real Local Agent through the Connector;
- a real remote Agent through the future Native A2A Runtime Adapter.

All three Runtime kinds submit the same versioned business actions through the
same Arena Result Sink. Rule/Fake actors remain explicitly labelled test
fixtures and cannot be used as evidence of real-Agent A2A completion.

## 4. Gateway-mediated A2A topology

Agents communicate logically with one another, but do not bypass Arena:

```text
Hosted Agent --------\
Local Connector ------> Arena A2A Gateway -> Market Directory / RFQ / Negotiation
Native A2A Endpoint --/                         |
                                               v
                                      immutable Deal -> Settlement
```

This preserves Agent autonomy without allowing unaudited direct calls, SSRF,
private endpoint leakage, out-of-round messages, or state changes outside the
Result Sink.

Standard A2A supplies Agent Card, Task, Message, Artifact, and task lifecycle
semantics. Arena defines an application profile on top:

```text
arena.market.intent.v1
arena.market.discover.v1
arena.market.rfq.v1
arena.market.select.v1
arena.market.negotiate.v1
arena.market.deal.v1
```

The current Connector WSS remains an internal transport and must not be called
standard Native A2A. A future `NativeA2ARuntimeAdapter` maps the same internal
business tasks to registered remote Agent Cards and A2A endpoints.

## 5. Round protocol

### 5.1 Intent window

Arena creates one immutable intent Task per active Game Agent. The Agent
returns:

- a buy intent;
- a sell intent; or
- pass.

A non-pass intent contains:

- `side`;
- `good`;
- fixed quantity `1`;
- a public reference price;
- a private hard limit;
- an optional sanitized public message.

The public market directory never exposes the private hard limit.

### 5.2 Discovery window

Arena freezes a public directory snapshot for each Agent. Entries include only
public Agent identity, side, good, quantity, public price, expiry, and bounded
public reputation.

Arena may filter structurally impossible entries, including the same
Participant, the same side, the wrong good, expired entries, or entries that
cannot share any legal price interval. It may not rank or select a target for
the Agent.

### 5.3 RFQ window

An eligible buyer receives the frozen directory and may submit up to the Game
limit of outbound requests. Each request contains:

- a target sell-intent ID;
- a positive opening price within the buyer's private ceiling;
- a sanitized public message.

The Gateway validates and durably relays each candidate request to the seller.
An invitation does not reserve either Participant.

The first implementation is buyer-initiated because the existing negotiation
rule already gives the buyer the opening proposal. Symmetric seller-initiated
solicitation is a later protocol version, not an implicit MVP behavior.

### 5.4 Counterparty selection

A seller receives a frozen set of inbound RFQs and returns either:

- `engage(requestId)`; or
- `reject_all`.

Only a successful, sanitized seller Agent result can create an Engagement.
Arena then atomically reserves the buyer and seller round slots and rechecks
cash, inventory, intent expiry, and the private price interval.

If either Participant is already reserved, the candidate closes with a safe
`counterparty_busy` result. The Agent may choose another already-frozen
candidate while time and fallback budget remain.

### 5.5 Negotiation

An Engagement creates one bounded negotiation context. Arena controls turns,
deadlines, message shape, hard-limit validation, and idempotency. Agents choose
every economic action.

The initial target is at most four combined messages:

```text
buyer propose
  -> seller counter | accept | reject
  -> buyer counter | accept | reject
  -> seller accept | reject
```

The final action cannot create an unanswered proposal. No quote may violate
the actor's private hard boundary. Arena does not automatically accept an
in-bound quote.

### 5.6 Fallback

On `counterparty_busy`, `reject`, or negotiation timeout:

- Arena records and releases only the applicable reservation;
- the Agent receives the remaining frozen candidates and safe failure state;
- the Agent decides whether to try another target or stop;
- the number of fallbacks is bounded and frozen in the Game configuration.

Settlement failure does not trigger counterparty fallback. An accepted Deal
and its payment evidence remain a separate, immutable failure path.

### 5.7 Deal and settlement

Arena freezes a Deal only after an Agent-authored acceptance of the
counterparty's latest proposal. The Deal references:

- both source intents;
- the buyer RFQ and seller selection Result IDs;
- the full sanitized negotiation;
- the latest proposal and acceptance Result IDs;
- the exact good, quantity, unit price, participants, and settlement accounts.

One Deal creates at most one SettlementIntent. Payment retry and recovery keep
the existing idempotency key and cannot reprice or change the payee.

## 6. Bounded MVP parameters

The first implementation freezes conservative defaults:

```text
MAX_OUTBOUND_RFQ = 3
MAX_ACTIVE_ENGAGEMENTS_PER_AGENT = 1
MAX_NEGOTIATION_MESSAGES = 4
MAX_COUNTERPARTY_FALLBACKS = 2
FIXED_QUANTITY = 1
```

These are Game configuration values and must not vary by Runtime kind.

Minimum display pacing and Runtime deadlines are separate:

- `phase_not_before_at` prevents an instant round;
- `phase_deadline_at` bounds unavailable or slow Agents;
- an early Runtime result waits for the minimum phase time;
- a late/invalid result deterministically closes without an inferred economic
  action.

## 7. Persistence model

The new path introduces:

- `market_intents`;
- `market_negotiation_requests`;
- `market_engagements`;
- `participant_round_slots`;
- immutable Deal provenance columns or a dedicated Deal record.

Required constraints include:

- one active intent per Participant per round;
- one source Result per intent/request/selection/action;
- one Engagement per request;
- at most one active Engagement per Participant per round;
- buyer and seller must differ;
- buyer/seller intents must share Game, round, good, and quantity;
- no Deal without both Agent-authored selection and acceptance provenance;
- no duplicate SettlementIntent or inventory commit.

## 8. Observability

Each round records a funnel without exposing private limits:

```text
intent tasks
authored buy / sell / pass
defaulted pass
directory entries
RFQs sent / delivered
RFQs engaged / rejected / busy
negotiations started
proposals / counters / accepts / rejects / timeouts
fallback attempts
Deals frozen
settlements submitted / confirmed / committed
```

Real-Agent evidence must additionally report Runtime kind and safe task
terminal status. Fake/Rule evidence is reported separately.

## 9. Delivery phases

### Phase A: protocol oracle and contracts

- [x] freeze this plan and align active product/roadmap references;
- [x] add strict versioned Intent/RFQ/Engage wire contracts;
- [x] add a pure Arena invariant state machine with no strategy;
- [x] add migration-ready persistence constraints;
- [x] prove idempotency, ownership, limit, and reservation invariants;
- [x] persist and apply the new task kinds through the shared PostgreSQL
      AgentTask Repository, Result Consumer, and Deadline Finalizer;
- [x] durably and idempotently project applied actions into
      MarketIntent/RFQ/Engagement tables through a recoverable Worker;
- [x] wire the opt-in `agent_a2a.v1` round orchestrator without changing the
      `fcfs.v1` Current Game.

Phase A alone is not evidence of a playable real-Agent market.

### Phase B: Hosted and Local real Agents

- [x] add `intent`, `discover/RFQ`, and `select` AgentTask kinds;
- [x] extend Prompt/Driver/Result policy without exposing private limits to
      counterparties;
- [x] dispatch the same immutable task envelope through Hosted and Local
      Connector Runtime paths;
- [ ] run real Claude Code, Codex, and Hosted-model mixed games;
- [x] require buyer RFQ and seller selection Result provenance for every
      persisted Engagement.

This is the first phase that may be described as real-Agent autonomous
counterparty selection. The completed Fake Provider tests prove only task,
schema, and Result Sink transport; they are not real-Agent evidence.

### Phase C: Deal and payment integration

- [x] create the compatibility Pairing/Negotiation only from an Agent-selected
      Engagement;
- [x] freeze immutable Deal provenance, including the proposal and acceptance
      Result IDs;
- [x] hand accepted Deals to the existing settlement-disabled or
      confirmation-gated Settlement boundary without repricing;
- [ ] run a payment-disabled real-Agent Engagement/negotiation/Deal E2E;
- [ ] run a fresh payment-enabled Injective testnet E2E only after explicit
      human confirmation.

### Phase D: Native A2A

- implement `NativeA2ARuntimeAdapter`;
- validate registered Agent Cards, endpoint ownership, authentication, and
  capabilities;
- map Arena Task/Message/Artifact to the pinned standard A2A version;
- normalize terminal results through the same Result Sink;
- complete a mixed Hosted/Connector/Native A2A game.

## 10. Acceptance criteria

The target is accepted only when:

- Arena cannot create an Engagement without a buyer RFQ Result and seller
  engage Result;
- Arena cannot create a Deal without an Agent proposal and counterparty
  acceptance Result;
- a Participant cannot be reserved or settled twice in one round;
- busy/reject/timeout fallback is Agent-selected and bounded;
- private hard limits never enter public messages, Agent Cards, logs, or
  frontend responses;
- Hosted, Local, and Native A2A use the same business schema and deadlines;
- Native A2A is not claimed before a standards-compliant Adapter E2E;
- Runtime success remains only a candidate until the Result Sink applies it;
- chain confirmation remains mandatory for cash/inventory mutation;
- Fake state-machine tests, real-Agent protocol evidence, and live testnet
  settlement evidence are reported as separate acceptance layers.

## 11. Compatibility and rollout

The existing FCFS Current Game remains the active implementation until the
Agent-driven path has durable persistence and real-Agent E2E evidence. The new
path is introduced under an explicit frozen Game matching/protocol version,
for example:

```text
market_protocol = fcfs.v1
market_protocol = agent_a2a.v1
```

Existing Games keep their frozen version. There is no in-place migration of an
active Game and no silent reinterpretation of historical Pairings.

## 12. Local evidence on 2026-08-04

Evidence levels remain separate:

- Fake scripted Runtime game
  `full-hosted-1785832034-3500a195` completed one opt-in
  `agent_a2a.v1` round with two Intent Results, one buyer RFQ Result, one
  seller Engage Result, one Engagement, two negotiation Results, and one
  immutable Deal. The Deal references distinct proposal and acceptance Result
  IDs. `authorizationMode=none` produced zero SettlementIntents and no balance
  or holding mutation. This proves orchestration and provenance, not real
  Agent autonomy.
- Real Local Connector game `real-runtimes-67c21f1d5c` used locally
  authenticated Claude Code 2.1.170 and Codex CLI 0.146.0 through WSS,
  stateless MCP, and the Arena Result Sink. Both independently published grain
  intents and the buyer completed its RFQ task. The buyer ceiling was
  `3.600000`; the seller floor was `4.300000`, so Arena exposed no
  structurally legal price interval and the buyer passed. The Game completed
  with zero Engagements, Deals, SettlementIntents, and chain writes. This is
  real-Agent intent/discovery evidence, not a real negotiated trade.
- A role-swapped probe `real-runtimes-b15bac88c1` demonstrated deterministic
  deadline recovery: Codex published a buy intent, Claude Code exceeded the
  common 180-second task deadline, the independent Finalizer applied the
  canonical pass, and the Game completed. It is timeout evidence, not a
  successful real-Agent E2E.
- A third probe `real-runtimes-a96f4c1c6e` produced an overlapping price
  interval (Claude buyer ceiling `3.800000`, Codex seller floor `3.000000`).
  Both Intent Results succeeded, but the Claude RFQ execution returned
  `runtime_failed` after roughly 291 seconds. The retained Runtime Events show
  ten Claude Code `api_retry` frames followed by
  `UNKNOWN_CERTIFICATE_VERIFICATION_ERROR`; this is a local Claude Code/API
  connectivity failure, not evidence of an Arena or Connector mechanism bug.
  Arena applied `market_timeout` and completed the Game without an Engagement.
  The full real-Agent trade path must be rerun after that external connection
  is healthy.
- Codex-only probe `real-runtimes-88099fe3e1` reached seller Select and exposed
  a Connector adapter bug: Codex autonomously returned a valid
  `engage + requestId` choice plus an explanatory `message`, but the strict
  business union rejects that field for Engage. Commit `ed619a2` now removes
  only that non-business Codex schema artifact while preserving the Agent's
  selected request; it does not choose a counterparty or price.
- The clean rerun `real-runtimes-9efb7dc941` used two independent Codex CLI
  0.146.0 Connector participants through WSS, stateless MCP, and the Arena
  Result Sink. Both Intents succeeded (buyer grain ceiling `4.200000`, seller
  floor `3.900000`), the buyer issued one RFQ, and the seller engaged it.
  Three negotiation Results were applied: buyer proposed `3.600000`, seller
  countered `4.500000`, and the buyer rejected because the counter exceeded
  its ceiling. The Game completed with one Engagement, one compatibility
  Pairing, three negotiation messages, zero Deals, zero SettlementIntents,
  zero inventory commits, and zero chain writes. Initial portfolios were
  equal at 20 gold-equivalent; no cash or holdings moved. This is real
  dual-Agent negotiation evidence, but not an accepted Deal or settlement.

The real probes also show why “few trades” cannot be solved by a matcher
alone: independently chosen private price intervals may not overlap, and a
failed external Runtime connection can remove one side of the market.
Follow-up work must calibrate Agent strategy and common deadlines while
preserving the hard boundary that Arena never chooses or relaxes an Agent's
economic action; external CLI/API failures are tracked separately from system
mechanism defects.
