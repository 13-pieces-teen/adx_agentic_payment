# Hosted Arena production runbook

> Status: deployable, fail-closed production boundary. This document is not
> evidence that Tencent SSM, a real Provider credential, or a fresh Injective
> transaction has been validated on a live server.

This runbook enables the three non-public production processes needed by the
Hosted Arena path:

- `hosted-worker`: reads one model credential, invokes an allowlisted Provider,
  and emits a candidate `AgentTaskResult`;
- `credential-controller`: can disable/delete a Secret but cannot create or
  read it;
- `arena-worker`: coordinates Hosted tasks, independently finalizes expired
  tasks, automatically advances complete games, reads chain evidence, and
  commits inventory after confirmation. It has no wallet, signer, private key,
  or transaction-submission interface.

The public API remains the write-only Secret ingress and authenticated control
plane. PostgreSQL, not process memory, is the durable queue and state authority.

## 1. Generate the base environment

On the server:

```sh
sh deploy/scripts/generate-env.sh arena.example.com
chmod 600 deploy/.env
```

The generator creates distinct random passwords for the API, Hosted Worker,
Arena Core Worker, and Credential Controller database logins. It leaves all
optional production workers disabled.

Never commit `deploy/.env`. Do not place a model API key, wallet private key,
seed phrase, or EIP-3009 signature in this file.

## 2. Configure Tencent SSM identities

Before setting `ADX_TENCENT_SSM_IAM_VERIFIED=true`, verify three distinct CAM
identities against only the Arena Hosted-model Secret prefix:

| Process | Required SSM capability | Explicitly forbidden |
|---|---|---|
| API writer | create the one requested Secret | get, list, disable, delete |
| Hosted Worker reader | get the one referenced Secret value | create, list, disable, delete |
| Credential Controller | disable and delete the referenced Secret | create, get, list |

On a single CVM, a shared instance role cannot prove this three-way separation.
Use three scoped CAM identities injected into their corresponding containers,
or move the processes to a workload-identity environment. If static CAM keys
are temporarily used, keep each identity distinct and store the values only in
the root-readable deployment environment.

Set:

```text
ADX_HOSTED_AGENTS_ENABLED=true
ADX_ENABLE_HOSTED_RUNTIME=true
ADX_TENCENT_SSM_IAM_VERIFIED=true
```

The API fails to enable Hosted creation unless the fingerprint pepper, Tencent
SSM settings, PostgreSQL repository, and role-specific IAM verification flag
are all present. There is no production fallback to the in-memory Secret
Store or deterministic demo Provider.

## 3. Enable the Arena Worker

Review the Injective testnet read endpoints, then set:

```text
ADX_ENABLE_ARENA_WORKER=true
ADX_ARENA_SETTLEMENT_RPC_URL=https://k8s.testnet.json-rpc.injective.network/
ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL=https://testnet.blockscout.injective.network/api/v2
```

Both configured URLs must use HTTPS. The worker can call read-only JSON-RPC and
Blockscout APIs. It cannot sign or broadcast a transaction.

The production Pawnhouse operator API is intentionally not exposed yet.
`arena-worker` processes durable runs and settlement submissions already
present in PostgreSQL; creating/starting production Games still needs a
separate authenticated operator surface.

## 4. Deploy

```sh
sh deploy/scripts/deploy.sh
```

The script always deploys PostgreSQL, migration, role provisioning, API, Web,
and Caddy. It starts Hosted and Arena profiles only when their explicit enable
flags are true. Hosted startup additionally refuses to proceed until SSM IAM
has been marked verified.

For a 2-vCPU / 4-GB host, keep one replica of each worker. The steady-state
Compose memory limits total about 3.5 GB; migration and role-provisioning jobs
are short-lived. Do not add API replicas because the current Connector WSS
connection registry and rate limiter remain single-process.

## 5. Verify without moving funds

```sh
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile hosted --profile arena ps
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile hosted logs --tail=100 hosted-worker credential-controller
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile arena logs --tail=100 arena-worker
curl -fsS "$(sed -n 's/^ADX_PUBLIC_APP_URL=//p' deploy/.env)/api/health"
```

Required observations:

- API and PostgreSQL are healthy;
- every enabled worker remains running and has no public port;
- creating a Hosted credential returns only metadata and a fingerprint hint;
- PostgreSQL contains only the Secret reference, never the raw Provider key;
- validation reaches `ready` with a real allowlisted Provider;
- stopping the Hosted Worker still allows Arena Deadline Finalizer defaults;
- a running Game advances across rounds from PostgreSQL state and freezes final
  prices/rankings without a browser session;
- a pending accepted settlement keeps its Round in `settle`;
- an unknown chain receipt is retried read-only and never causes a second
  authorization or payment.

Do not run the state-changing settlement bridge as part of infrastructure
verification. A fresh testnet transfer requires a separate human review of the
frozen `SettlementIntent`, approval bound to its exact `intentHash`, and
explicit confirmation. The bridge persists that approval before broadcast and
uses a deterministic per-Intent nonce. If submission recording is interrupted,
resume only with the already public transaction hash; never replace it with a
new authorization.

## 6. Rollback

Disable new Hosted creation first:

```text
ADX_HOSTED_AGENTS_ENABLED=false
ADX_ENABLE_HOSTED_RUNTIME=false
```

Then redeploy. Do not delete PostgreSQL rows or Secrets to perform a rollback.
Existing lifecycle jobs and submitted settlement records are recovery
evidence. A submitted/unknown payment must be reconciled by transaction hash,
not replaced with a new authorization.
