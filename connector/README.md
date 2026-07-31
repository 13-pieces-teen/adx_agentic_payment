# Arena 402 Local Connector

`adx-connector` is the user-side execution bridge between locally installed
agent runtimes and Arena 402. The executable retains the `adx-connector` name
as a compatibility identifier. It initiates one outbound WebSocket
connection, reports a multi-runtime inventory, and starts only Connector-owned
Claude Code or Codex processes. It does not attach to or take over terminals
that the user already opened.

Task transport defaults to the existing WSS path. A feature-gated
`wss + stateless MCP` mode is also implemented: WSS keeps Device presence,
heartbeats, Session control, and safe `task.available` wake hints, while MCP
Streamable HTTP performs the authoritative task claim, result submission, and
lease release. The two transports do not create separate Arena task models.

The default policy is detection-only: the Connector can discover runtimes and
answer `runtime.probe`, but it does not start an Agent task until the user opts
in locally for a specific runtime.

## Arena game integration status

The Connector is an implemented control plane, not the Arena game authority.
It now accepts versioned `arena.decide` and `arena.negotiate` payloads inside
the existing top-level `task.dispatch` action and returns a separate
`arena.agent-result.v1` terminal result. The local result is durably stored
before transport, replayed after reconnect, and removed only after
`agent_task.result.ack`. A new Connector process increments the binding's
durable session generation, creates a new Connector-owned session, and may
retry one typed Arena task whose prior receipt ended as
`connector_restarted`; the total Runtime attempt count remains capped at two.

The backend integration now includes owner-scoped Local Arena Agent
registration, frozen Connector binding epochs, authenticated game
participation, automatic Connector-owned Arena session startup, leased
AgentTask dispatch, and one coordinator for Hosted-only, Connector-only, or
Hosted/Connector mixed rounds. Each task still returns through the Arena
Result Sink and Deadline Finalizer. A real Claude Code/Codex Connector-only
one-round game was accepted in an isolated Docker stack on 2026-07-31. Both
Runtime results succeeded, passed through the Result Sink, and produced two
terminal rankings with zero chain writes. Codex chose `pass` and Claude chose
`buy grain`, so that run is not evidence of pairing, negotiation, payment, or
inventory transfer. Production reconnect and real Hosted/Connector mixed
acceptance are still pending.

The stateless MCP slice is also implementation-complete behind
`ADX_ARENA_MCP_ENABLED=false`. It includes short-lived Device/Binding/epoch
execution tokens, PostgreSQL lease fencing, the Arena Result Sink, and these
tools:

- `arena_claim_agent_task`
- `arena_get_agent_task_status`
- `arena_submit_agent_task_result`
- `arena_release_agent_task`
- `arena_sync_agent_tasks`

The current `adx-connector` consumes WSS wake hints and uses MCP for
claim/submit/release when `ADX_CONNECTOR_TASK_TRANSPORT=mcp`. The Gateway hello
acknowledgement includes only the authenticated Device's minimal
`binding_id + binding_epoch` references. The Connector remembers those frozen
routes and runs bounded cursor sync after startup/reconnect and whenever it
detects a Gateway sequence gap. Periodic WSS wake replay remains a low-latency
fallback rather than the recovery authority.

An isolated Docker protocol E2E was accepted on 2026-07-31. It covers fresh
migrations, login/pairing/approval, WSS hello and `task.available`, a
Connector-owned managed Session, Device token exchange, MCP discover/list,
sync/claim/submit/status, the PostgreSQL Result Sink, and zero chain writes.
That harness emulates the managed Runtime control frames. A separate real
Runtime harness now launches locally authenticated Claude Code and Codex child
processes on the host while Docker owns Arena, Gateway, PostgreSQL, and the
Arena worker. It verifies authoritative task/result/application rows and
rankings rather than treating process exit or MCP submit as business success.

Run the real Runtime E2E from PowerShell after confirming both local CLIs are
authenticated. It uses the isolated project `arena402-real-runtimes-e2e`,
loopback ports `18001`/`55434`, and payment-disabled one-time test users:

```powershell
docker compose -p arena402-real-runtimes-e2e -f docker-compose.local.yml -f tests/docker-compose.real-runtimes-e2e.yml --profile arena up --build -d postgres migrate provision-db-roles api arena-worker
$inviteRaw = docker compose -p arena402-real-runtimes-e2e -f docker-compose.local.yml -f tests/docker-compose.real-runtimes-e2e.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1 --count 2 --json
$env:ADX_REAL_RUNTIME_E2E_INVITES = ConvertTo-Json -Compress -InputObject @((ConvertFrom-Json $inviteRaw).invites)
python tests/real_runtimes_docker_e2e.py
Remove-Item Env:ADX_REAL_RUNTIME_E2E_INVITES
docker compose -p arena402-real-runtimes-e2e -f docker-compose.local.yml -f tests/docker-compose.real-runtimes-e2e.yml --profile arena down -v --remove-orphans
```

This accepted run does not cover production reconnect, lease expiry, durable
result replay after process loss, Hosted/Connector mixing, OpenClaw, or Hermes.

Run that isolated test from the repository root. It uses project
`arena402-mcp-e2e`, loopback ports `18000`/`55433`, and removes only its own
test volume:

```sh
docker compose -p arena402-mcp-e2e -f docker-compose.local.yml -f tests/docker-compose.mcp-e2e.yml up --build -d postgres migrate provision-db-roles api
python tests/mcp_docker_e2e.py
docker compose -p arena402-mcp-e2e -f docker-compose.local.yml -f tests/docker-compose.mcp-e2e.yml down -v --remove-orphans
```

The authority boundaries remain:

- Arena owns game, round, pairing, negotiation, inventory, and ranking state;
- the Connector owns Device, Runtime, Binding, Command, receipt, and
  Connector-owned Session state;
- Injective EVM owns payment finality;
- a successful Connector receipt proves only that a bounded local task
  completed, not that a trade or payment succeeded;
- only structured actions and public negotiation messages may be returned;
  private chain-of-thought, local credentials, and unrelated files remain out
  of scope.

## Build

Requirements:

- Go 1.23 or newer
- Network access for the pinned `github.com/coder/websocket v1.8.15` module

```sh
go build -o adx-connector ./cmd/adx-connector
go test ./...
```

## First run

The low-step foreground path is:

```sh
adx-connector connect --server https://arena.example
```

The Connector requests a one-time code, opens the default browser directly to
the approval page, saves the device credential, detects supported runtimes, and
stays online. If the browser cannot be opened, the same approval URL is printed
for manual use.

Explicit pairing remains available for installers and service setup:

```sh
./adx-connector pair --server https://arena.example
./adx-connector run --allow-root /path/to/workspace
```

`run` also starts pairing automatically when the state file has no device
credential. The terminal displays the platform verification URL and a one-time
user code. After the user approves it in the browser, the Connector exchanges
the device code and saves the returned credential in the user configuration
directory with file mode `0600`. Directories created by the Connector use mode
`0700`; a user-selected directory that already exists keeps its original mode
and ACL. Windows builds apply the equivalent current-user-only DACL to files
and Connector-created directories without replacing an existing parent
directory ACL. Device tokens and device codes are never written to logs.

Platform installers under [`deploy/install`](../deploy/install/README.md)
register a current-user Scheduled Task on Windows or `systemd --user` service
on Linux. They pair interactively once, then start `run --auto-pair=false` so a
background service never opens an unexpected authorization browser.

For local development, `http://localhost` and `ws://localhost` are allowed.
Remote enrollment and transport require HTTPS/WSS.
The current unauthenticated in-memory Gateway is also disabled by default; a
loopback-only demo server must explicitly set `ADX_CONNECTOR_UNSAFE_DEMO=true`.
When enabled, Gateway middleware rejects non-loopback direct peers for both
HTTP and WebSocket routes and does not trust forwarded-address headers. Do not
place this unsafe demo behind a reverse proxy.

Useful commands:

```sh
./adx-connector scan
./adx-connector doctor
./adx-connector run \
  --api-base http://localhost:8000 \
  --allow-root /path/to/project
```

To enable Connector-owned Codex tasks for a trusted local demo:

```sh
./adx-connector run \
  --api-base http://localhost:8000 \
  --allow-root /path/to/project \
  --enable-codex-tasks
```

Claude task execution requires the separate
`--unsafe-enable-claude-tasks` development flag. The Connector cannot yet
verify whether the local Claude CLI is using an API key, Bedrock/Vertex, or a
consumer subscription login, so this flag must remain off outside an isolated
test account with vendor approval.

The equivalent local environment gates are
`ADX_CONNECTOR_ENABLE_CODEX_TASKS=true` and
`ADX_CONNECTOR_UNSAFE_ENABLE_CLAUDE_TASKS=true`; both default to false.

On Windows, repeat `--allow-root` for each directory that Arena-managed
sessions may access. A requested working directory is resolved through
symlinks and rejected unless it is contained by one of these local roots.
An older Binding without a workspace may freeze `working_directory` on its
next create request exactly once; changing that frozen directory requires a
new Binding epoch rather than silently moving an active Agent.

## Credential injection

Pairing is the normal path. An operator may bootstrap an ephemeral environment
with all three variables:

```text
ADX_CONNECTOR_DEVICE_ID
ADX_CONNECTOR_TOKEN
ADX_CONNECTOR_GATEWAY_URL
```

The token is never placed in a URL or printed. WebSocket authentication uses:

```text
Authorization: Device <device_token>
```

The URL carries only `device_id`.

The JSON file store is an MVP implementation. Production installers should
replace token storage with Keychain, Credential Manager, or Secret Service
while retaining the same store interface.

`pair` and `run` take a fail-fast OS lock on `<state>.lock`. Only one Connector
may use a state/device credential at a time (`LockFileEx` on Windows,
`flock`/`fcntl` on Unix). The OS releases the lock if the process crashes.
As a second guard, a Connector whose WSS is closed with code `4409` because a
newer instance replaced it exits instead of entering a reconnect fight.

## Runtime discovery and drivers

Discovery checks `PATH` plus a small set of common per-user install
directories, then runs bounded `--version` and local CLI authentication-status
probes (`codex login status` or `claude auth status`). Probe output is discarded
and credentials are never read or uploaded. Every detected runtime has a stable
path-derived `runtime_id`, availability, locally enabled capabilities,
`task_enabled`, `authentication_status`, `arena_isolation`, and
`arena_compatible` / `local_execution_ready`. Compatibility is checked with a
bounded CLI `--help` probe for every required Arena safety flag.
Authentication remains classified as
`unverified_local_auth`: `configured` only means the local CLI reports a usable
login configuration, not that provider quota, model access, subscription
compliance, or a paid inference has been verified.

Generic managed tasks retain the fixed CLI command shapes below:

- Claude Code: `claude --print --output-format stream-json --verbose`
- Codex: `codex exec --json -`

Typed Arena tasks use a separate, non-resumable execution profile. The
Connector creates a short-lived empty working directory containing only a
`0600` output Schema and removes it after the process exits:

- Claude Code adds safe mode, an empty strict MCP config, `--tools ""`,
  disabled slash commands, no session persistence, `dontAsk`, and the strict
  JSON Schema. This is the Arena no-tools profile.
- Codex adds read-only sandboxing, ephemeral execution, ignored user
  configuration and exec-policy rules, `--skip-git-repo-check`, an isolated
  `--cd`, and a strict root-object output Schema compatible with OpenAI
  Structured Outputs. Nullable schema placeholders are removed only from
  Codex terminal actions before the shared strict wire validator runs. The
  current Codex CLI has no equivalent no-tools switch, so this profile still
  must not be described as tool-free.

Prompts are delivered over stdin. Runtime resume identifiers are captured from
structured output and used only through the runtimes' fixed resume flags. The
Connector never uses `sh -c`, `cmd /c`, a cloud-provided executable, or
cloud-provided process arguments.

Gateway and Connector both fail closed for typed Arena tasks unless the local
task flag, CLI authentication status, CLI safety-flag compatibility, expected
isolation profile, and the derived readiness bit all agree. Merely being
installed, version-readable,
online, or bound is not Arena execution readiness. Codex's read-only profile
can still invoke model tools within its sandbox, so task flags must remain off
unless the local account and operating-system read boundary are trusted.

Managed child processes inherit a small platform environment allowlist plus
locally resolved names explicitly listed in `environment_refs`. The platform
can refer to a variable name but cannot provide or read its value.
Each referenced name must also be authorized locally with a repeated
`--allow-env NAME` flag; no secret-bearing variable is eligible by default.
Runtime messages do not assume that every model exposes reasoning. When a
provider does include `thinking`, `reasoning`, or equivalent private blocks,
the Connector drops their content before emitting an observability event while
preserving public text, structured actions, safe errors, and numeric usage.

## Control and observation protocol

Every envelope uses `protocol_version: "1.0"`. Connector-originated message
types are:

- `hello`
- `inventory.snapshot`
- `heartbeat`
- `command.ack`
- `runtime.event`
- `agent_task.result`
- `task.available.ack`

The only accepted platform message with an operational effect is `command`,
whose `action` must be one of:

- `runtime.probe`
- `session.start`
- `task.dispatch`
- `task.cancel`
- `session.stop`
- `session.resume`

The Gateway can also acknowledge a committed terminal Arena result with
`agent_task.result.ack`; both `task_id` and `result_id` must match the pending
record before it is deleted. This acknowledgement affects only the local
durable result outbox; it is not an Arena business-action or payment
acknowledgement.

When MCP task transport is enabled, the Gateway may send `task.available`.
Its payload contains only `wake_id`, `task_id`, frozen `binding_id + epoch`,
and the Arena deadline. The Connector returns `task.available.ack`, exchanges
its Device credential for a short-lived execution token, and then claims the
task over `/mcp`. Neither the wake nor its acknowledgement leases the task or
proves execution.

All other actions, including shell execution, are rejected. Commands carry an
expiry, idempotency key, and binding epoch. Receipts survive restarts, so a
redelivered command in the retained receipt window does not run twice. Every
non-terminal receipt is retained; the newest 512 terminal receipts are kept
and older terminal receipts are compacted. A production implementation needs a
durable retention/TTL policy sized to the platform's maximum redelivery window.
Every managed-session command must match the session's binding, agent, runtime,
and binding epoch.

The cloud cannot provide `conversation_id` or `resume_token`. A stopped session
can resume only after its Connector-owned runtime child has reported a provider
session/thread token; the first locally captured token is retained and is never
returned to the cloud.

Stdout and stderr are consumed without a terminal. JSON lines become
`runtime.message` events; other lines become structured stream events.
Credential-shaped text and secret-keyed JSON fields are redacted before
upload. Runtime events are assigned durable sequence numbers and written to a
local outbox before sending. The gateway may acknowledge committed events with
`event.ack` and `through_sequence`; unacknowledged events are replayed after a
reconnect.

If command receipt lookup/claim/result persistence or event
stage/append/clear/ack persistence fails, the Connector latches a
persistence-degraded error, rejects new commands, and exits instead of
continuing with an incomplete audit stream.

`task.dispatch` first returns `accepted`. When the owned child exits, the
Supervisor emits a second command acknowledgement with `succeeded` or
`failed`, plus the task id, exit code, cancellation, and timeout state. This
keeps platform command state aligned with the observed process lifecycle.
If the Connector restarts while such a command is still `accepted`, startup
atomically converts its durable receipt to `failed` with
`connector_restarted` and replays that terminal acknowledgement. A
`task.cancel` command is terminal as soon as the cancellation signal has been
delivered to the owned task; it is never left indefinitely at `accepted`.

For a typed Arena task, process completion also produces exactly one
independent `AgentTaskResult`. A succeeded result must contain a strict
`buy|sell|pass` or `propose|accept|reject` action. Failed, timed-out, and
cancelled results contain no action. The Connector does not infer an Arena
action from a command acknowledgement, a generic event, or unstructured
stdout.

Sequence allocation and outbox insertion use a crash-recoverable staged event
in the state file. On startup (and before staging another event), the Connector
idempotently completes any interrupted outbox insertion. Reconnect replay is
limited to 64 unacknowledged events and is sent in small bursts, so gateway
acknowledgements and control commands continue to be handled while a large
backlog drains.

On Linux, managed children run in their own process group with a parent-death
`SIGKILL`, and explicit cancellation kills the process group. Other Unix
platforms retain process-group cancellation. On Windows, each managed task is
started suspended, assigned to a private Job Object configured with
`KILL_ON_JOB_CLOSE`, and only then resumed. The Job handle is retained for the
task lifetime, so explicit cancellation terminates the whole job and an
ungraceful Connector exit causes Windows to terminate its remaining task
processes and descendants. If Job creation, assignment, or process resumption
fails, the still-suspended process is terminated and the task is rejected
instead of running without containment.

Gateway close code `4403` means the Device was revoked. The Connector stops
reconnecting and `run` shuts down all of its owned tasks. Close code `4409`
means a newer Connector instance replaced this one and also terminates the old
client without a reconnect fight.

This is execution observability for Connector-owned sessions, not a claim of
full-machine auditing.
