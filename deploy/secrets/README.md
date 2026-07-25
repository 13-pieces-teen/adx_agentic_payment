# Runtime secret mount

`docker-compose.production.yml` mounts this directory read-only into the API
and Hosted Worker. The single-host beta expects a raw 32-byte file named
`hosted-master.key`; `*.key` is ignored by Git.

Production deployments should set `ADX_HOSTED_SECRET_DIR_HOST_PATH` to a
persistent directory outside the release tree. Use a root-owned, traversable
directory and make the key file owned/readable only by the container runtime
UID (`10001`), for example directory mode `0755` and key mode `0400`. The key
must never be committed.
