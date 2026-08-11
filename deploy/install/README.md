# Arena 402 Local Connector installers

> Current status — 2026-08-09: the installer contract remains the supported
> Local Connector onboarding path. A real Codex Connector has completed the
> formal mixed-Runtime payment-enabled Game. Signed releases, SBOM-based client
> verification and automatic secure updates are the next distribution work.

The binary, service, and environment identifiers retain the historical `ADX`
name for compatibility.

These installers provide the low-step local onboarding path:

1. download a platform-hosted Connector binary over HTTPS;
2. verify its SHA-256 checksum;
3. open the Arena device-approval page and wait for one authorization;
4. install a current-user startup service and bring the Connector online.

The platform must publish these files (or pass an explicit binary URL):

```text
/downloads/adx-connector-windows-amd64.exe
/downloads/adx-connector-windows-amd64.exe.sha256
/downloads/adx-connector-windows-arm64.exe
/downloads/adx-connector-windows-arm64.exe.sha256
/downloads/adx-connector-linux-amd64
/downloads/adx-connector-linux-amd64.sha256
/downloads/adx-connector-linux-arm64
/downloads/adx-connector-linux-arm64.sha256
/downloads/adx-connector-darwin-amd64
/downloads/adx-connector-darwin-amd64.sha256
/downloads/adx-connector-darwin-arm64
/downloads/adx-connector-darwin-arm64.sha256
```

The checksum file starts with the binary's 64-character SHA-256 digest.
Production installers accept only HTTPS URLs. Loopback HTTP is retained solely
for local development.

## Windows

From a downloaded, reviewed script:

```powershell
.\install-connector.ps1 -Server https://arena.example
```

It installs into the current user's local application directory and registers
the `ADX Local Connector` scheduled task at logon. Both the installation and
credential directories receive a protected current-user-only ACL.

To remove startup and the binary while retaining local credentials:

```powershell
.\uninstall-connector.ps1
```

Add `-PurgeCredentials` only after revoking the Device in Arena.

## Linux

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://arena.example/downloads/install.sh \
  | sh -s -- --server https://arena.example
```

It installs under `~/.local`, creates a hardened `systemd --user` unit, enables
it, and starts it. The installer attempts to enable user lingering for startup
before login. If policy requires administrator approval, it prints the exact
`loginctl` command instead.

To remove startup and the binary while retaining local credentials:

```sh
sh ./uninstall-connector.sh
```

Add `--purge-credentials` only after revoking the Device in Arena.

## macOS

The same `install.sh` / `install-connector.sh` script detects Darwin and
registers a current-user LaunchAgent instead of systemd:

```sh
curl --proto '=https' --tlsv1.2 -fsSL https://arena.example/downloads/install.sh \
  | sh -s -- --server https://arena.example
```

It installs under `~/.local`, writes
`~/Library/LaunchAgents/com.adx.local-connector.plist`, bootstraps it for the
current GUI session, and starts the Connector immediately. Apple Silicon uses
`adx-connector-darwin-arm64`; Intel Macs use `adx-connector-darwin-amd64`.

To remove startup and the binary while retaining local credentials:

```sh
sh ./uninstall-connector.sh
```

Add `--purge-credentials` only after revoking the Device in Arena.

## Local capability opt-in

Installers start detection-only by default. `--enable-codex-tasks` on Linux/macOS
or `-EnableCodexTasks` on Windows opts into Connector-owned Codex tasks for the
selected `allow-root`. Claude task execution remains an explicit unsafe
development-only CLI flag and is intentionally absent from the installers.
