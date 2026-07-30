# Official DeepSeek Agents through LiteLLM

Arena 402 uses LiteLLM only for platform-owned Official Agents:

```text
Official Agent -> official-deepseek -> private LiteLLM -> DeepSeek key pool
Player BYOK    -> deepseek          -> DeepSeek directly
```

The public Hosted capability API never advertises `official-deepseek`, so a
player cannot select the internal route. Game and AgentTask snapshots still
freeze Provider, model, Runtime, and credential exactly as before.

## What LiteLLM owns

Every configured DeepSeek key is one LiteLLM deployment under the same model
alias. LiteLLM uses `simple-shuffle` to select a deployment per request and
cooldown unhealthy deployments. Arena does not assign keys to Agents.

Proxy retries and model fallbacks are disabled. One Arena Attempt therefore
causes at most one LiteLLM upstream attempt; Arena remains the only owner of
the approved AgentTask retry.

The Proxy is pinned to LiteLLM `v1.89.4`, has no published host port, and is
reachable only on the Compose networks. Official Agents store only the
internal LiteLLM token. Player DeepSeek credentials never enter LiteLLM.

## Secret boundary

- Put upstream DeepSeek keys in separate `*.key` files only for the one-shot
  provisioning command.
- Put the LiteLLM internal token in its own file. It must start with `sk-`.
- Never put either value in `.env`, Git, logs, command arguments, the
  manifest, or frontend responses.
- The provisioner sends all values directly through the existing write-only
  Secret Store port, under versioned platform-only refs. They are not unbound
  player Credential rows and therefore do not inherit the player credential
  expiry lifecycle. At rest they use the configured PostgreSQL AES-256-GCM
  vault or Tencent SSM.
- `manifest.json` contains only model aliases and opaque Secret Store refs.
- At Proxy startup a short wrapper resolves those refs, removes all Arena
  database/vault settings, and then `exec`s the stock LiteLLM process. Raw
  values exist only in the trusted gateway process memory.

Remove the transient source key files after successful provisioning.

## Provision the gateway

Prepare the key and token paths readable by container UID/GID `10001`. The
manifest directory must be writable by that UID/GID during provisioning and
readable by it when the Proxy starts:

```text
/etc/arena402/official-deepseek-keys/
  01-primary.key
  02-secondary.key
/etc/arena402/official-litellm-token.key
/var/lib/arena402/official-litellm/
```

Set only their paths in `deploy/.env`:

```text
ADX_OFFICIAL_DEEPSEEK_KEY_SOURCE_HOST_PATH=/etc/arena402/official-deepseek-keys
ADX_OFFICIAL_LITELLM_TOKEN_FILE_HOST_PATH=/etc/arena402/official-litellm-token.key
ADX_OFFICIAL_LITELLM_CONFIG_HOST_PATH=/var/lib/arena402/official-litellm
ADX_OFFICIAL_LITELLM_CONFIG_VERSION=v1
ADX_ENABLE_OFFICIAL_LITELLM=false
```

When intentionally replacing the upstream key set or LiteLLM token, increment
the immutable config version (`v2`, `v3`, ...). Provisioning then creates new
Secret Store records, and the final bootstrap creates a replacement Official
Agent pool using that token. This is versioned cutover, not an Arena-side
rotation or sharding algorithm.

First ingest the keys and create the non-secret manifest:

```text
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile ops run --rm official-litellm-provision
```

Then start the private gateway and Hosted Worker:

```text
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile official-agents up -d --force-recreate official-litellm

docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile hosted up -d hosted-worker credential-controller
```

Finally provision Official Agents with the LiteLLM token:

```text
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile ops run --rm official-agent-bootstrap
```

The Official bootstrap never mounts or reads the upstream DeepSeek key
directory. Before creating or activating the pool it calls LiteLLM's
authenticated `/health` endpoint, which makes one real check against every
configured deployment; any unhealthy key blocks the cutover.

After the first successful cutover, set
`ADX_ENABLE_OFFICIAL_LITELLM=true`. Standard deploy/release then rebuilds,
force-recreates, and health-checks the private gateway. Keep it `false` before
the first manifest has been provisioned.

## Current cutover boundary

These changes provide the gateway and the new Official Agent route. They do
not silently rewrite an already-running Official pool. Existing Agents are
frozen to their joined Provider/config, and production cutover must happen
between Games after the new LiteLLM-backed pool has validated `ready`.

This setup does not broadcast a chain transaction. Settlement remains behind
PaymentMandate validation, confirmation, and idempotent inventory commit.
