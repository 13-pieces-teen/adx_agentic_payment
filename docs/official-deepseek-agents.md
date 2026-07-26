# Official DeepSeek Agent Pool

Arena 402 can provision its operator-owned official seats through the same
Hosted Agent credential and Runtime path used by player BYOK Agents. Official
membership remains an explicit `arena402.official_agent_pool` allowlist; using
DeepSeek does not make a player Agent official.

## Security contract

- Put the DeepSeek API key in a host file containing only the key.
- Do not put the key in `.env`, a command argument, Git, logs, or the frontend.
- The provisioning command sends it through the existing write-only credential
  ingress. Arena tables retain only credential metadata and an opaque secret
  reference.
- Use a verified `postgres_aesgcm` or Tencent SSM backend shared by the API and
  Hosted Worker.
- The official pool is activated only after every credential and Hosted route
  reaches `ready`.

## Provision and activate

The Hosted Worker must already be running. Supply database URLs through the
existing process environment; do not place them on the command line.

For the production Compose stack:

```text
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile hosted up -d hosted-worker credential-controller

docker compose --env-file deploy/.env -f docker-compose.production.yml \
  --profile ops run --rm official-agent-bootstrap
```

The one-shot `ops` service reads the key from
`ADX_OFFICIAL_DEEPSEEK_KEY_FILE_HOST_PATH` (default:
`deploy/secrets/deepseek-official.key`) as a read-only bind mount.

Direct Python invocation is also available:

```text
python scripts/bootstrap_official_agent_pool.py \
  --api-key-file /run/secrets/official/deepseek.key \
  --count 20 \
  --model deepseek-v4-flash \
  --activate \
  --replace-enabled-pool
```

Required environment:

```text
ADX_HOSTED_CONTROL_DATABASE_URL=...
ADX_OFFICIAL_BOOTSTRAP_DATABASE_URL=...
ADX_HOSTED_FINGERPRINT_PEPPER_B64=...
ADX_HOSTED_SECRET_BACKEND=postgres_aesgcm
ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED=true
ADX_HOSTED_MASTER_KEY_FILE=/run/secrets/arena402/hosted-master.key
```

For Tencent SSM, use the existing verified SSM writer configuration instead of
the PostgreSQL AES-GCM variables.

The command is idempotent for the stable 20 official owner slots. Re-running it
reuses the credential and Agent creation records. `--replace-enabled-pool`
disables older official pool entries only after all new DeepSeek Agents are
validated, then atomically activates the new pool.

This command does not broadcast a chain transaction. Game settlement still
follows PaymentMandate limits, accepted-trade creation, Facilitator submission,
chain confirmation, and idempotent inventory commit.
