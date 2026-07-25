# Runtime secret mount

`docker-compose.production.yml` mounts this directory read-only into the API
and Hosted Worker. The single-host beta expects a raw 32-byte file named
`hosted-master.key`; `*.key` is ignored by Git.

Production deployments should set `ADX_HOSTED_SECRET_DIR_HOST_PATH` to a
persistent directory outside the release tree. Use a root-owned, traversable
directory and make the key file owned/readable only by the container runtime
UID (`10001`), for example directory mode `0755` and key mode `0400`. The key
must never be committed.

The wallet signer uses a separate directory configured by
`ADX_WALLET_SECRET_DIR_HOST_PATH` and a separate raw 32-byte
`wallet-master.key`. Keep it at mode `0400`; do not reuse
`hosted-master.key`. Losing this key makes wallet ciphertext unrecoverable, so
store an encrypted offline backup. The wallet CSV is a one-time import source
for the manual `wallet-admin` profile and is never mounted into the running
signer.
