# Agent-Driven A2A Market Implementation Plan

> Status: approved and implemented through Phase D. Production Current Game is
> frozen on `agent_a2a.v1`; `fcfs.v1` remains a compatibility and rollback
> protocol for games that explicitly froze it. Phase A persistence/projection,
> Phase B Hosted/Local tasks, Phase C Deal provenance, mixed-Runtime fallback
> and recovery, and payment-enabled Phase D are complete. Formal Game
> `game-20260806-110040-099857d6f841` combined one real Codex Connector with
> nine DeepSeek Hosted Agents for eight rounds and committed three
> `arena402-g` trades only after chain confirmation. Public Facilitator
> compatibility, D5b 12/25/50 tiers and repeatability, a 20-human overlay,
> active-Game whole-host restart, and Phase E Native A2A remain separate
> acceptance items. The 2026-08-09 4 vCPU / 8 GiB runs close one
> payment-disabled and one 50-settlement payment-enabled 100-Hosted-Agent point.
>
> Approved direction: Arena 402 is an Agent-native market. Agents discover
> counterparties, choose whom to approach, select which request to engage, and
> negotiate the price. Arena is the authenticated Gateway, policy enforcement
> point, durable protocol authority, and settlement coordinator. It must not
> choose a counterparty or synthesize an Agent's market decision.

## 1. Why this plan exists

The legacy centrally matched FCFS design had two related limitations:

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
- [x] preserve real lease-expiry takeover and durable Result-outbox replay
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
- [x] run a real multi-counterparty sequential-fallback E2E and preserve
      restart/replay evidence;
- [x] run a fresh payment-enabled Injective testnet A2A E2E after the explicit
      human authorization recorded for Phase D. The isolated mUSDC run preserved
      the binding-RFQ, timeout, Result Sink, recovery, and confirmation-gated
      inventory invariants; the product `arena402-g` rerun remains open below.

### Phase D: unified autonomous Current Game

- [x] add an allowlisted deployment setting for `fcfs.v1 | agent_a2a.v1` and freeze
  it when a new Current Game is created;
- [x] never reinterpret an active or historical Game when the deployment setting
  changes; rollback changes only the next Game;
- [x] parameterize the payment-enabled Hosted canary for a strict
  `agent_a2a.v1`/eight-round preflight and include the frozen protocol in its
  evidence; this remains Hosted-only harness capability, not mixed acceptance;
- [x] complete one eight-round isolated Game with one real Codex Connector player
  and nine persistent PydanticAI Hosted Agents selected from the official pool;
- [ ] use `arena402-g`, `single_eip3009`, and the self-hosted Facilitator so at
  least one immutable Deal reaches chain confirmation and idempotent inventory
  commit;
- [x] prove with three isolated mUSDC settlements that the committed portfolio
  affects the next round and final ranking;
- [x] create learning evaluations only from authoritative completed-game evidence,
  then freeze an activated revision only when that persistent Agent joins a
  later Game;
- [x] preserve restart, Result Sink, settlement recovery, market terminalization,
  secret isolation, and no-CoT-persistence invariants in the unified run;
- [ ] cut over the production Official pool and Current Game only in a no-active-
  Game maintenance window with a tested `fcfs.v1` rollback.

Intermediate evidence is `phase-d-mixed-musdc-v4-c30a038913`: 92/92 Task
results applied across eight rounds, three immutable Deals, three confirmed
mUSDC transfers and inventory commits, and ten rankings. A later
payment-disabled Game froze and executed the two activated learned revisions.
One additional settled Agent exhausted the two-attempt structured-output limit
and correctly retained its prior revision. This evidence closes the isolated
D2/D3 mechanics, not the `arena402-g` or production cutover gates.

Phase D is the first phase that may be described as a payment-enabled,
cross-game-learning autonomous Agent competition. Separate FCFS Hosted payment
canaries and payment-disabled Codex A2A games cannot be combined into that
claim.

### Phase E: Native A2A

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
- Hosted and Local use the same business schema and per-Game deadline in
  Phase D; Native A2A must join that contract in Phase E;
- timeout calibration collects at least 100 real end-to-end samples per
  supported Runtime/task combination at target load, applies the frozen
  `max(P99) * 1.25` formula, and demonstrates no more than 1% valid-Task
  deadline timeout;
- Native A2A is not claimed before a standards-compliant Phase E Adapter E2E;
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

`fcfs.v1` remains immutable for historical Games and as an explicit next-Game
rollback option. New Games freeze the deployment-selected protocol without
reinterpreting active or historical Games; the accepted Phase D production
Game uses `agent_a2a.v1`:

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
  is outstanding. That run alone did not prove lease-expiry takeover or replay
  of a terminal Result already persisted in the Connector outbox; the
  following runs cover those boundaries.
- The isolated run `mixed-fallback-8af2ba9c8c` injected a five-second orphan
  lease immediately before the real MCP claim for the fallback seller's
  `arena.market.select` Task. The production notifier and claim path waited
  until expiry, then the binding-scoped MCP worker took over about 42 ms after
  the recorded expiration. The Task recorded both worker identities and three
  lease events, but only one Result row and one applied action; negotiation
  still reached one Deal with zero payment, asset, or chain mutations.
- The isolated run `mixed-fallback-4f99467b24` rejected the first terminal
  Result submission for the same real Codex selection Task after the Connector
  had persisted it locally. Before process restart the local outbox contained
  one Result while Arena contained zero. Restarting from the same state replayed
  the outbox, cleared the local entry, and produced exactly one authoritative
  Arena Result and one applied action. The Connector transport Result ID and
  Arena's normalized authoritative Result ID are preserved separately; they
  are not incorrectly treated as one identifier. The Game then completed its
  Deal with zero SettlementIntents, asset mutations, or chain writes.
- The all-real-seller run `mixed-fallback-87fc3f3217` used one deterministic
  Hosted buyer only to bound the scenario; both seller seats were independent
  real Codex CLI 0.146.0 Connectors with separate users, Devices, Bindings,
  Sessions, and durable state. Both Codex Agents independently published iron
  sell Intents at `5.000000` with a private `4.500000` floor. The buyer sent
  the Primary seller a `1.000000` opening; that real seller engaged and
  countered at `5.500000`, after which the buyer rejected. A second RFQ from
  the original frozen directory targeted the Secondary real seller, which
  engaged and accepted its `5.000000` opening. All ten AgentTasks completed
  as succeeded/applied candidates. Arena preserved two RFQs, two Engagements,
  four Engagement-scoped compatibility entries, and one Deal with distinct
  proposal/acceptance Result IDs. After restarting API and Arena worker the
  counts remained `10 tasks / 10 Results / 10 applies / 2 RFQs /
  2 Engagements / 1 Deal / 4 entries`, with the RFQ session still
  `completed / 2 of 3`. Payment remained disabled, so SettlementIntent,
  asset-mutation, and chain-write counts were all zero.
- The preceding calibration run `mixed-fallback-d56b70ab63` is retained as an
  economic non-failure: its Primary real Codex Agent chose to buy gems instead
  of publishing an iron sell Intent, so only one compatible seller existed
  and the Game correctly completed after one RFQ. The accepted run used equal
  seller portfolios to exercise the intended two-seller path; no Arena
  matcher or Result was rewritten to manufacture fallback.

The real probes also show why “few trades” cannot be solved by a matcher
alone: Agents may choose different goods, private price intervals may not
overlap, and a bounded negotiation may end in a rational rejection. Runtime
output compatibility defects and external CLI/API failures are tracked
separately from those economic outcomes. Follow-up work may calibrate Agent
strategy and common deadlines, but Arena must never choose or relax an
Agent's economic action.

### Action-timeout calibration pilot (2026-08-05)

- `scripts/calibrate_action_timeout.py` reads only explicitly selected Games
  and resolves Connector identity through the frozen Arena binding and the
  authoritative Connector runtime record. It uses persisted Task creation,
  first lease, Result receipt, and Arena application timestamps; Connector ACK
  and HTTP request latency are not treated as AgentTask completion.
- The report groups queue age, successful end-to-end latency, apply latency,
  deadline timeout, failure, and retry by exact Runtime/task. It emits a
  recommendation only when every required combination has at least 100
  terminal samples, no more than 1% valid-task deadline timeout, and successful
  end-to-end evidence. The recommendation is the maximum required P99 times
  `1.25`, rounded upward to five seconds. Otherwise it exits non-zero with a
  null recommendation and concrete blockers.
- Fresh game `real-runtimes-f65b334bd1` used two independent Codex Connectors,
  `agent_a2a.v1`, three rounds, and `authorizationMode=none`. All six
  `arena.market.intent` Tasks succeeded and were applied; the Agents produced
  no compatible RFQ opportunity, so the correct autonomous outcome was zero
  Request, Engagement, Deal, SettlementIntent, asset mutation, and chain write.
- Combining that game with seven accepted, non-fault mixed runs gives Codex
  terminal counts `arena.decide=0`, `arena.market.intent=15`,
  `arena.market.rfq=0`, `arena.market.select=8`, and `arena.negotiate=8`.
  The three observed groups have zero deadline timeout and zero retry. Their
  observed end-to-end P50/P95/P99 values are respectively
  `15.223/33.087/33.087 s`, `10.968/31.055/31.055 s`, and
  `9.400/18.161/18.161 s`. These small nearest-rank tails are pilot
  observations, not production estimates; all five combinations remain below
  the required 100 samples and no timeout value is frozen.
- The separate read-only API control baseline used 1000 `/api/ready` requests.
  Sequential concurrency 25/50/64 produced P95
  `63.72/107.47/161.51 ms` and error rates `0/0/0.3%`. Concurrency 100 exceeds
  the isolated Compose limit `ADX_API_MAX_CONCURRENCY=64` and receives
  admission 503s. This control-plane result is intentionally excluded from the
  action-timeout formula.

### Ten-Agent real Codex canaries (2026-08-05)

- The real-Runtime harness now accepts 2–100 independent invite, User, Device,
  Binding, Session, and Connector seats. `--runtime-kind codex` restricts
  discovery before executable/version/auth probes; all three canaries observed
  zero newly started Claude processes. Buyer and seller portfolios remain
  exactly 20 gold at initial prices. Multi-good seller portfolios increase
  market coverage without changing or manufacturing any Agent action.
- Baseline A2A game `real-runtimes-a2a048b555` completed ten succeeded/applied
  Intents with 72.52 ms launch skew, 7.01 s Result receipt skew, 29.56 s Intent
  stage wall time, and 29.77 s round wall time. Buyers independently chose
  non-grain goods while every seller held grain, so zero RFQ and Deal was the
  correct economic outcome.
- Diversified A2A game `real-runtimes-d95129aafc` completed 10 Intents, 5 RFQs,
  2 seller Select/Engage actions, 2 accepts, and 2 immutable Deals. The Intent
  launch skew was 56.74 ms and the whole round took 82.11 s. All 19 Tasks were
  succeeded/applied with zero timeout and retry. Payment was disabled, so both
  accepted negotiations ended `settlement_failed` with zero SettlementIntent,
  asset mutation, or chain write.
- FCFS compatibility game `real-runtimes-61ba000c4b` completed ten real
  `arena.decide` Tasks with 55.41 ms launch skew, 4.16 s Result receipt skew,
  20.38 s stage wall time, and 20.58 s round wall time. The autonomous buy,
  sell, and pass actions had no compatible same-good pair, so zero pairing was
  not a matcher failure.
- Across accepted non-fault Codex evidence, including two subsequent
  ten-Agent, eight-round complete games, terminal sample counts are now
  `decide=10 / intent=195 / rfq=79 / select=33 / negotiate=36`; every observed
  group has zero deadline timeout and retry. The earlier one-round percentile
  snapshot is superseded and must be regenerated with the 12/25/50/100 Agent
  load tiers. Only Intent has crossed the 100-sample gate, so the common
  timeout remains unfrozen.
- One mid-run, non-peak resource snapshot observed API 63 MiB, Arena worker
  37 MiB, PostgreSQL 107 MiB, ten Connector processes 238 MiB total, and three
  active Codex child processes 388 MiB total. It is a canary observation, not
  a capacity ceiling or production host sizing result.
