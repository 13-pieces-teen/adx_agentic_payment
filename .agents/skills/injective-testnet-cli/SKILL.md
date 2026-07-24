---
name: injective-testnet-cli
description: Safely inspect and use the Injective CLI for this project's testnet payment flow. Use when checking injectived, querying Injective testnet, preparing or verifying a testnet transaction, or troubleshooting x402 receipts. Also use when a request mentions mainnet, raw-key import or export, --yes, unattended signing, reset, or deletion so the skill can refuse those unsafe actions.
compatibility: Read-only queries may use a reported installed version. State-changing work requires a team-approved pinned injectived binary and explicit human control.
---

# Injective Testnet CLI

Use this skill to support the project's Injective testnet payment path without exposing wallet secrets or allowing an agent to perform an unattended transaction.

## Safety boundary

- Work on Injective testnet only. Stop if the resolved chain ID, endpoint, or account configuration points to mainnet.
- Default to read-only queries.
- Do not install or upgrade `injectived` automatically. Report a missing or mismatched binary so a human can provision the team-approved pinned version.
- Do not read, request, print, copy, import, export, or store private keys, seed phrases, keyring passwords, or wallet backup files.
- Do not place secrets in command arguments, standard input, environment examples, logs, or generated files.
- Do not use `--yes`, unsafe key commands, reset commands, or unattended signing.
- Require explicit confirmation from a human in the current interaction before signing or broadcasting any transaction.

## Preflight

1. Read the repository `AGENTS.md`, `docs/product.md`, and `docs/roadmap.md`.
2. Locate the installed binary without changing the machine:

   ```powershell
   Get-Command injectived -ErrorAction Stop
   injectived version
   ```

3. Record the actual CLI version and compare it with the version approved by the team. An unpinned installed version may be used for a narrow read-only query if its version is reported, but stop before every state-changing operation until the team pins a version.
4. Resolve the effective endpoint, chain ID, account, and keyring backend without exposing secret values.
5. Verify that the chain ID and endpoint are the team's Injective testnet values. Reject mainnet configuration.

## Read-only query workflow

1. Inspect the exact command help from the installed binary:

   ```powershell
   injectived query --help
   injectived <query-path> --help
   ```

2. Run the narrowest query needed.
3. Capture only non-sensitive evidence required for the task, such as network, height, transaction hash, status, denomination, amount, and public addresses.
4. Report the command, exit status, and relevant result without dumping unrelated configuration.

## Transaction preparation workflow

Before signing or broadcasting:

1. Build the intended transaction without secrets in the command.
2. Simulate or generate an unsigned transaction when supported by the installed CLI.
3. Present this summary to the human:
   - endpoint and chain ID;
   - sender and recipient public addresses;
   - denomination and amount;
   - fees and gas settings;
   - project order or payment identifier;
   - expected state change;
   - whether the action is retry-safe.
   Show both base-unit and human-readable amounts when a conversion is required. Obtain denomination metadata from the active testnet and never guess decimal conversion.
4. Stop and wait for explicit confirmation.
5. Keep wallet unlock and signing under direct human control.
6. The agent may broadcast a human-signed transaction only when the human explicitly approves that signed artifact and authorizes broadcast in the current interaction. Never perform wallet unlock or signing.
7. Verify the returned transaction independently with a read-only query.

## x402 settlement verification

Bind a payment receipt to the project order before treating it as paid:

- expected Injective testnet network and chain ID;
- expected recipient;
- expected denomination and amount;
- unique order or payment identifier;
- successful transaction status;
- transaction hash and block height;
- absence of a previously accepted receipt for the same idempotency key.

Do not trigger delivery when any field is missing, mismatched, failed, or ambiguous.

## Stop conditions

Stop and ask for human direction when:

- configuration resolves to mainnet;
- the CLI binary is missing;
- a state-changing operation is requested while the CLI version is unpinned or different across teammates;
- a command would expose or manipulate key material;
- simulation fails or differs from the proposed transaction;
- recipient, asset, amount, network, or order identity is unclear;
- retry behavior could create a second payment or delivery;
- the requested command deletes, resets, exports, imports, or irreversibly changes wallet or chain state.
