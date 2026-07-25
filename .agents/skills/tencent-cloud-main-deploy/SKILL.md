---
name: tencent-cloud-main-deploy
description: Deploy the latest verified main branch of this Arena 402 repository to its Tencent Cloud Docker Compose server, including the Japan-host variant, over SSH. Use whenever the user asks to package, publish, update, release, roll back, or verify the production deployment, especially when they mention latest main, the production server, deploy.sh, Docker Compose, or api.arena402.com. Follow the preflight, backup, package-transfer, migration, health-check, replica-scaling, DNS, and rollback evidence gates; never copy secrets into the package or expose them in output.
compatibility: Requires Git, PowerShell, OpenSSH (ssh/scp), tar, and a remote Linux host with Docker Compose. The exact SSH key, host, user, and remote release directory must be supplied by the operator or discovered from approved local deployment configuration without printing secrets.
---

# Tencent Cloud main deployment SOP

Use this skill for a controlled release of the repository's latest `main` to
the Tencent Cloud production host. The goal is to make the remote release,
database state, running containers, and public health endpoint agree on one
known commit. Treat a deployment as incomplete until the post-deploy evidence
has been collected.

## Safety boundary

- Deploy only to the operator-approved Tencent Cloud host and release path.
- Never print, commit, package, upload, or request private keys, `.env` files,
  `deploy/secrets/`, wallet files, Hosted credential material, or facilitator
  CSV contents. Preserve those files on the server and exclude them from the
  source archive.
- Do not run destructive commands against a broad path. Before moving or
  deleting anything, resolve and display the exact release or temporary-file
  target; retain a rollback directory and database backup.
- Use testnet payment configuration unless the operator explicitly confirms a
  separately approved production chain change. A successful container deploy
  is not proof of a successful on-chain payment flow.
- Do not silently stage unrelated worktree changes. Package the requested
  commit or a clean checkout of `origin/main`, not every local file.

## Phase 0: establish release identity

1. Read the repository `AGENTS.md`, `README.md`, `docs/roadmap.md`, and the
   deployment scripts that will run on the server.
2. Fetch the remote and record the exact release commit:

   ```powershell
   git fetch origin main --prune
   git rev-parse origin/main
   git log -1 --oneline origin/main
   ```

3. Confirm whether the worktree is dirty. Treat existing unrelated changes as
   user-owned; do not include or reset them. If the requested release is not
   already on `origin/main`, stop and report the missing commit instead of
   deploying a different branch.
4. Run the smallest relevant local checks before packaging. For a release
   containing migration changes, run the migration tests and the targeted
   service tests. Record the pass count and commit hash.

### Japan-host password/SSH variant

The Japan host may be password-authenticated and the supplied `adx.pem` may
belong to a different server. Do not try an unrelated key or place the
password in a command argument, shell history, archive, or report. If the
operator explicitly supplies a password, use a short-lived local askpass
helper that reads a process environment variable, set
`SSH_ASKPASS_REQUIRE=force`, and remove the helper as soon as all remote work
is complete. Each escalated network command should still have a bounded
timeout. Confirm the host with a read-only `echo`/hostname command before any
state change.

If the repository worktree is dirty, package only `origin/main` with
`git archive`; never use the working tree as the release source and never
stage unrelated files. Re-read `git rev-parse origin/main` immediately before
packaging and again before writing the deployed marker. If it changed during
the run, stop, rebuild the archive, and restart the identity comparison.

## Phase 1: inspect the remote before changing it

Use the operator-provided SSH key with `ssh` and `StrictHostKeyChecking` policy
appropriate for the environment. Do not put the key contents in a command or
log. Read-only remote checks should establish:

- hostname, remote user, release directory, and active Compose project;
- current container status and recent health state;
- current deployed revision marker, image IDs, or release directory name;
- presence of the production `.env`, secret mounts, and connector artifacts;
- current migration registry and the latest database backup.

If the server is a source-package deployment without `.git`, use an explicit
archive checksum and a release directory marker as the deployment identity.
Do not infer the deployed revision from a successful SSH command alone.

## Phase 2: create a safe release package

Prefer a clean archive generated from the exact `origin/main` tree. Exclude
runtime state, secrets, caches, local virtual environments, test artifacts,
`.git`, `deploy/secrets`, and any operator-only files. For example:

```powershell
$release = (git rev-parse --short=12 origin/main)
$archive = ".tmp\adx-$release.tar"
git archive --format=tar --output $archive origin/main
Get-FileHash $archive -Algorithm SHA256
```

If the deployment requires connector binaries, build or verify them using the
repository's `deploy/scripts/build-connector-artifacts.sh` contract and record
their checksums separately. Do not rebuild a connector artifact merely to
change unrelated application code when a verified compatible artifact already
exists.

Before transfer, inspect the archive listing and confirm it contains source,
Compose files, migrations, and deploy scripts but no secrets or private key
material.

## Phase 3: transfer and preserve rollback state

1. Upload the archive to a uniquely named remote temporary path such as
   `/tmp/adx-<commit>.tar` with `scp`.
2. Verify the remote SHA-256 equals the local SHA-256 before extraction.
3. On the server, move the current release to a timestamped rollback path such
   as `/home/ubuntu/adx_agentic_payment.pre-<commit>-<utc>`.
4. Extract the new release into the exact active release directory.
5. Copy or preserve only the approved runtime files from the old release,
   especially `deploy/.env`, secret mounts, wallet/Hosted credential files,
   and verified connector artifacts. Never copy them into the source archive.
6. Take a database backup before applying migrations. Record its absolute path,
   timestamp, and compression status.

For a server that has no `.git`, the archive SHA-256 plus a remote marker such
as `/opt/arena402/DEPLOYED_GIT_SHA` is the release identity. Keep the previous
release directory and the database backup; do not delete either as part of
normal cleanup. Delete only the exact temporary archive paths after the final
verification succeeds.

If any checksum, extraction, ownership, or required-runtime-file check fails,
restore the previous directory and stop before running Compose.

## Phase 4: run the repository deployment

Run the repository's deployment entrypoint from the active remote release:

```sh
cd /home/ubuntu/adx_agentic_payment
sh deploy/scripts/deploy.sh
```

The script is the authority for Compose build profiles, TLS mode, migration
execution, and enabled workers. Do not replace it with an ad-hoc `docker
compose up` sequence unless diagnosing a failure after preserving the release
and backup.

Watch migration output carefully. A migration is successful only when it is
registered with the expected filename and checksum. If a migration fails:

- do not mark the deployment successful;
- capture the exact error and current registry rows;
- inspect ownership and grants for the migration registry before changing
  permissions;
- take or retain a backup before a repair;
- fix the migration in source, commit it to `main`, and redeploy the new commit
  when the failure is a source defect;
- never manually mark an unapplied migration as applied.

Already-applied migration files are immutable database state. If the source
archive changes comments, line endings, a filename, or DDL in a migration that
is already present in `adx_schema_migrations`, do not overwrite its registry
row or use a checksum bypass. Compare the registry checksum with the exact
previously applied release file, preserve that applied file in the server-only
release if necessary, and keep the source fix in a later migration/commit. A
temporary registry-table grant is allowed only when the migration's reviewed
SQL requires it: capture the grant, run the migration, immediately revoke it,
and verify that the grant is gone. Never leave the temporary privilege in the
production database.

On this project, `deploy/scripts/deploy.sh` uses the base Compose file. If the
remote installation is intentionally running a benchmark or capacity
override (for example a Hosted Worker replica override), record that before
deployment and converge the base file plus the same override after the base
deployment. Otherwise the base script can silently reduce the deployment to a
single worker. Use the configured replica count, not an assumed count, and
verify every resulting container.

## Phase 5: post-deploy verification

Collect evidence in this order:

1. `docker compose ps` shows the expected API, database, proxy, and enabled
   worker/signer/facilitator containers running and healthy where healthchecks
   exist.
2. The migration registry contains every expected migration, including the
   exact deployed filename and checksum. Query the schema for columns,
   constraints, indexes, or tables introduced by the release.
3. Connector release artifacts exist remotely and their checksums match the
   verified artifacts.
4. Query the public API health endpoint and record HTTP status plus the
   sanitized response. For this project the production endpoint is normally
   `https://api.arena402.com/api/health`, but use the configured public host.
5. Smoke-test only the routes appropriate to the release. A `404` such as
   `current_game_not_found` can be an expected empty-state response; report it
   separately from a transport or service failure.
6. Compare the deployed release checksum/marker or image digest to the target
   `origin/main` commit. Do not claim "latest main deployed" without this
   comparison.

For an IP-TLS Japan host, test the configured server IP directly with the
appropriate certificate option when the public DNS name points elsewhere. A
successful IP health check proves the Japan host is serving; it does not prove
that `api.arena402.com` has been cut over. Resolve the public hostname and
report any DNS mismatch separately. An empty-state route response such as
`404 current_game_not_found` is an application-level smoke result, not a
transport failure.

Do not claim that real settlement, a complete game loop, or public frontend
acceptance passed unless those flows were actually exercised and independently
verified. Health checks prove availability, not business correctness.

## Rollback

Rollback is allowed only to the explicitly recorded previous release and
backup. Stop before rollback if the migration is irreversible or if the
operator has not approved the database restore procedure. For a code-only
failure, restore the previous release directory, keep the new directory for
forensics, rerun the same Compose convergence command, and re-check health and
revision identity. For a schema failure, preserve logs and backup evidence and
obtain human direction before restoring data.

## Release report

Return a concise report containing:

- target host and public URL (without secrets);
- deployed commit and archive SHA-256;
- local test results;
- backup path and rollback directory;
- migration names/checksums and schema verification;
- container and public health results;
- known non-blocking empty-state responses;
- any remaining dirty local files that were intentionally not packaged.

Also report whether the public hostname resolves to the target Japan host,
whether a benchmark override was restored, whether temporary remote files and
the local askpass helper were removed, and whether any migration compatibility
work was server-only. Do not include passwords, private keys, `.env` values,
wallet material, or full secret-bearing command lines in the report.

A deployment is complete only when this report is evidence-backed and the
operator can identify both the rollback target and the exact deployed commit.
