# Agent-Driven A2A Market Implementation Plan

> Status: approved target; Phase A persistence, projection, opt-in round
> orchestration, Phase B Hosted/Local task support, and the Phase C
> payment-disabled Deal foundation were implemented on 2026-08-04. Current
> Game remains on `fcfs.v1`. A payment-disabled game with two independent
> Codex Connector Agents has completed Intent, RFQ, selection, bounded
> negotiation, and an immutable Deal with distinct proposal/acceptance Result
> provenance. A deterministic three-Hosted-Agent Fake E2E has also completed
> first-seller rejection and second-seller fallback with durable restart
> evidence. Hosted + real Codex mixed fallback, in-flight Connector restart,
> and deadline-default injection are complete. All-real multi-seller fallback,
> lease-expiry takeover, durable Result-outbox replay injection, and
> payment-enabled A2A remain incomplete.
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

An eligible buyer receives the frozen directory and may submit exactly one
outbound request per RFQ Task. The frozen Game budget permits at most three
total RFQ attempts, including at most two fallback attempts. Each request
contains:

- a target sell-intent ID;
- a positive opening price within the buyer's private ceiling;
- a sanitized public message.

The Gateway validates and durably relays each candidate request to the seller.
An invitation does not reserve either Participant.

The selected RFQ `openingPrice` is the buyer-authored, binding first proposal.
If the seller engages, Arena materializes that exact request as negotiation
turn 1; Arena does not ask the buyer to restate or change it. Provenance keeps
the RFQ Result ID, request ID, and stable request position so a seller may
accept the opening price without an invented `arena.negotiate` proposal.

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
`counterparty_busy` result. Only one Engagement may be active for a
Participant. The buyer may choose another already-frozen candidate while time
and fallback budget remain; Arena never selects the replacement.

### 5.5 Negotiation

An Engagement creates one bounded negotiation context. Arena controls turns,
deadlines, message shape, hard-limit validation, and idempotency. Agents choose
every economic action.

The MVP is frozen at no more than three combined Agent actions:

```text
buyer binding RFQ opening proposal
  -> seller counter | accept | reject
  -> buyer accept | reject
```

The third action cannot create an unanswered proposal. No quote may violate
the actor's private hard boundary. Arena does not automatically accept an
in-bound quote. A later protocol version may introduce a different bounded
turn structure, but an existing Game keeps its frozen version.

### 5.6 Fallback

On `counterparty_busy`, `reject`, or negotiation timeout:

- Arena records and releases only the applicable reservation;
- the buyer receives the remaining frozen candidates, prior attempt statuses,
  remaining absolute time, and remaining RFQ/fallback budget;
- the buyer decides whether to submit one new RFQ to one remaining target or
  stop;
- the total budget is three RFQ attempts with at most two fallbacks;
- the attempt and fallback counters are durable and survive Worker restart.

Settlement failure does not trigger counterparty fallback. An accepted Deal
and its payment evidence remain a separate, immutable failure path.

### 5.7 Deal and settlement

Arena freezes a Deal only after an Agent-authored acceptance of the
counterparty's latest proposal. The Deal references:

- both source intents;
- the buyer RFQ Result, selected request ID/position, and seller selection
  Result ID;
- the full sanitized negotiation;
- the latest proposal source and acceptance Result ID; the latest proposal
  may be the binding RFQ request or a later counterproposal Result;
- the exact good, quantity, unit price, participants, and settlement accounts.

One Deal creates at most one SettlementIntent. Payment retry and recovery keep
the existing idempotency key and cannot reprice or change the payee.

## 6. Bounded MVP parameters

The first implementation freezes conservative defaults:

```text
MAX_OUTBOUND_RFQ = 3
MAX_RFQ_PER_TASK = 1
MAX_ACTIVE_ENGAGEMENTS_PER_AGENT = 1
MAX_NEGOTIATION_MESSAGES = 3
MAX_COUNTERPARTY_FALLBACKS = 2
FIXED_QUANTITY = 1
```

These are Game configuration values and must not vary by Runtime kind.
`FIXED_QUANTITY=1` is the current protocol contract. Future bounded quantity
support requires a new versioned schema and corresponding reservation,
mandate, settlement, and inventory invariants.

Minimum display pacing and Runtime deadlines are separate:

- `phase_not_before_at` prevents an instant round;
- `phase_deadline_at` bounds unavailable or slow Agents;
- an early Runtime result waits for the minimum phase time;
- a late/invalid result deterministically closes without an inferred economic
  action;
- one `action_timeout_ms` is frozen per Game for every Runtime kind, with its
  deployment default calculated as the maximum real end-to-end P99 across
  supported Runtime/task/load combinations, multiplied by `1.25` and rounded
  up to the next five seconds.

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
- [x] run real Codex Local Connector games through negotiation and a
      payment-disabled Deal;
- [x] run a real Hosted + Codex Connector mixed game and preserve in-flight
      reconnect, deadline-default, and projection-recovery evidence;
- [ ] preserve real lease-expiry takeover and durable Result-outbox replay
      injection evidence;
- [ ] rerun Claude Code after its external API/certificate path is healthy;
      this is follow-up evidence and does not block Hosted + Codex acceptance;
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
- [x] materialize the engaged RFQ opening as negotiation turn 1 and allow the
      seller to accept that exact request without an invented buyer action;
- [x] persist the frozen directory and sequential attempt counter, enforce one
      unresolved buyer RFQ and at most three attempts, and dispatch only
      Agent-selected fallbacks after busy/reject/timeout;
- [x] hand accepted Deals to the existing settlement-disabled or
      confirmation-gated Settlement boundary without repricing;
- [x] run a payment-disabled real-Agent Engagement/negotiation/Deal E2E;
- [x] run a local Fake scripted binding-opening Deal E2E
      (`full-hosted-1785853139-cd4e22d1`) with exact request/Result proposal
      provenance, zero SettlementIntent, and zero chain write;
- [ ] run a real multi-counterparty sequential-fallback E2E and preserve
      restart/replay evidence;
- [ ] run a fresh payment-enabled Injective testnet E2E only after explicit
      human confirmation and after the binding-RFQ, sequential-fallback,
      timeout-calibration, and mixed-Runtime recovery acceptance above.

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
- an engaged RFQ opening price is the binding first proposal and cannot be
  reduced or replaced after seller selection;
- Arena cannot create a Deal without an Agent proposal and counterparty
  acceptance Result;
- a Participant cannot be reserved or settled twice in one round;
- busy/reject/timeout fallback is Agent-selected and bounded;
- fallback is sequential, uses only the frozen directory, and never follows
  settlement failure;
- private hard limits never enter public messages, Agent Cards, logs, or
  frontend responses;
- Hosted, Local, and Native A2A use the same business schema and deadlines;
- timeout calibration collects at least 100 real end-to-end samples per
  supported Runtime/task combination at target load, applies the frozen
  `max(P99) * 1.25` formula, and demonstrates no more than 1% valid-Task
  deadline timeout;
- Native A2A is not claimed before a standards-compliant Adapter E2E;
- Runtime success remains only a candidate until the Result Sink applies it;
- chain confirmation remains mandatory for cash/inventory mutation;
- `agent_a2a.v1` remains exact quantity `1`; a future `agent_a2a.v2` may add
  bounded integer quantity without partial fills;
- batch settlement is allowed only after throughput evidence, keeps each Deal
  mapped to an individual transfer/result, and never uses unrecoverable
  aggregate netting;
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
- Repeated one-round `granary-fire` probes naturally aligned both Agents on
  grain. One clean probe rejected after the seller countered at `3.000000`
  above the buyer's `2.900000` ceiling. Two other probes reached the buyer's
  final turn with an in-range seller quote but returned `runtime_failed`.
  The repeated boundary exposed a Local Connector adapter defect rather than
  an Arena pricing decision: Codex may echo `price` and `message` with an
  `accept`, while the strict business union intentionally accepts only
  `{"action":"accept"}` because Arena owns the frozen latest quote. The Codex
  adapter now removes only those compatibility fields, matching the existing
  Claude adapter behavior and preserving the Agent's accept/reject choice.
- The post-fix run `real-runtimes-e8c3b2d723` used two independent Codex CLI
  0.146.0 Connector participants, separate users, Devices, Bindings, Sessions,
  and state stores. Both published grain Intents; the buyer issued one RFQ and
  the seller engaged it. Three real negotiation Results were applied: buyer
  proposed `2.550000`, seller countered `2.900000`, and the buyer, whose
  private ceiling was `2.950000`, accepted. Arena froze one Deal for one grain
  at `2.900000`, referencing distinct seller proposal Result
  `runtime:2a3257ab...975c4` and buyer acceptance Result
  `runtime:f4ec6052...6dcc04`. The test portfolios were equal in initial
  value but deliberately different in composition: 20 gold cash versus
  10 grain at the 2-gold initial price. With `authorizationMode=none`, the
  negotiation correctly closed as `settlement_failed`; authoritative counts
  remained zero SettlementIntents, zero inventory commits, zero cash
  mutations, zero holding mutations, and zero chain writes. This is accepted
  payment-disabled real-Agent Deal evidence, not payment settlement evidence.
- The local scripted run `full-hosted-1785897607-5cd29355` used three
  development Hosted actors to make sequential fallback deterministic. The
  buyer selected the rejecting seller first, then a second RFQ Task selected
  the only remaining seller. The run completed with three Intents, two RFQs,
  two Engagements, four negotiation messages, one immutable Deal, and zero
  SettlementIntents. The first attempt exposed a compatibility defect:
  legacy FCFS `pool_entries` assumed one entry per participant per round, so a
  second A2A Engagement reused the buyer entry and violated the Pairing
  uniqueness constraint. Migration `061` gives each A2A Engagement distinct
  compatibility entries while retaining the FCFS uniqueness invariant through
  a partial unique index. After restarting API and Arena worker, the RFQ
  session remained `completed` with `attempt_count=2`, and request,
  Engagement, Deal, compatibility-entry, and settlement counts did not grow.
  This is Fake recovery evidence, not real-Agent evidence.
- The isolated run `mixed-fallback-7f15a77f8c` kept the deterministic Hosted
  buyer and first rejecting seller, but replaced the remaining seller with a
  real Codex CLI 0.146.0 Connector Agent over WSS and stateless MCP. Codex
  independently published an iron sell Intent from its equal-value
  `0 cash + 4 iron` portfolio, engaged the second RFQ, and accepted the
  binding `7.000000` opening. All nine AgentTasks completed with succeeded,
  applied candidate Results. The Game produced two RFQs, two Engagements,
  four negotiation messages, one immutable Deal, four Engagement-scoped
  compatibility entries, zero SettlementIntents, zero asset mutations, and
  zero chain writes. Restarting the isolated API and Arena worker left the RFQ
  session at `completed / 2 of 3` and counts at `2 requests / 2 engagements /
  1 deal / 4 entries / 0 settlement intents`. This is mixed real-Agent and
  terminal projection-recovery evidence; the following fault runs add the
  in-flight recovery cases.
- The fault-injected run `mixed-fallback-a865aba66f` terminated the Connector
  while real Codex CLI 0.146.0 held the second `arena.market.select` Task,
  then restarted from the same local state. The probe exposed and fixed two
  recovery defects: MCP command identity did not distinguish a rebuilt
  Connector-owned Session, and replay of the pre-restart `session.start`
  receipt could restore a process-local Session that no longer existed.
  MCP command identity is now stable per `task_id + session_id`, while Gateway
  command records freeze `session_generation` and cannot project a stale
  lifecycle receipt into the current binding. The recovered Task recorded five
  lease events but exactly one Result row and one applied action; the Game then
  completed the second RFQ, Engagement, negotiation, and Deal with zero
  SettlementIntents, asset mutations, or chain writes.
- The no-reconnect run `mixed-fallback-5f00bae33a` terminated the Connector at
  the same seller-selection boundary and used a uniform 60-second action
  timeout. The Deadline Finalizer closed that Task exactly once as
  `defaulted / timed_out / applied / market_timeout`; the second RFQ became
  `expired`, the Game completed with zero Deals, and payment/asset/chain
  counts remained zero. This proves deadline closure while a Connector lease
  is outstanding. It is not yet evidence of lease-expiry takeover or replay
  of a terminal Result already persisted in the Connector outbox.

The real probes also show why “few trades” cannot be solved by a matcher
alone: Agents may choose different goods, private price intervals may not
overlap, and a bounded negotiation may end in a rational rejection. Runtime
output compatibility defects and external CLI/API failures are tracked
separately from those economic outcomes. Follow-up work may calibrate Agent
strategy and common deadlines, but Arena must never choose or relax an
Agent's economic action.
