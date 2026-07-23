# Injective MCP Server

> Connect AI agents and AI assistants to Injective queries and transactions over MCP for natural language operations on the Injective network.

- Official source: [https://docs.injective.network/developers-ai/mcp](https://docs.injective.network/developers-ai/mcp)
- Last source change: `2026-03-31`
- Snapshot captured: `2026-07-23`
- Upstream revision: [`1a31f4937cce`](https://github.com/InjectiveLabs/injective-docs/commit/1a31f4937cce679b1bf5542743dc1e223289d248)

---

The Injective MCP server provides full trading capabilities on Injective.
This includes perpetual futures, spot transfers, cross-chain bridging, and raw EVM transactions.

This is powerful because it gives your AI tools the ability to trade on Injective.
This server is designed to be used extensively by
the [Injective Trading Skills](./injective-trading-skills.md).
Teach your AI tools how to use this MCP server via
the [Injective MCP Servers Skill](./injective-mcp-servers-skill.md).

## What is MCP?

[MCP](https://modelcontextprotocol.io/introduction) is an **open standard** that enables
AI assistants to connect to external data sources and tools.
Instead of relying solely on training data, AI assistants can use MCP to access real-time,
authoritative information directly from documentation.

## Why use the Injective MCP server?

When you connect an AI tool to the Injective MCP server, it can:

- Enable you to perform more informed trades by performing common queries or research tasks
- Trade autonomously on your behalf (e.g. BYO trading logic/ instructions)

This is particularly useful when trading on Injective,
as the AI can help you express your trading intent in spoken language.

## MCP server details

| Property | Value |
|----------|-------|
| Endpoint | localhost |
| Transport | stdio |
| Available tools | Multiple, see [using the MCP server](#using-the-mcp-server) |

## Connecting to the MCP server

### All MCP clients

    For MCP-compatible clients, set up and run the server locally:

    ```shell
    git clone https://github.com/InjectiveLabs/mcp-server injective-mcp-server
    cd injective-mcp-server
    npm install && npm run build
    ```

### Claude Desktop

    Add the following to your Claude Desktop configuration file:

#### macOS

        Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

        ```json
        {
            "mcpServers": {
                "injective": {
                    "command": "node",
                    "args": ["/path/to/injective-mcp-server/dist/mcp/server.js"],
                    "env": {
                        "INJECTIVE_NETWORK": "mainnet"
                    }
                }
            }
        }
        ```
#### Windows

        Edit `%APPDATA%\Claude\claude_desktop_config.json`:

        ```json
        {
            "mcpServers": {
                "injective": {
                    "command": "node",
                    "args": ["/path/to/injective-mcp-server/dist/mcp/server.js"],
                    "env": {
                        "INJECTIVE_NETWORK": "mainnet"
                    }
                }
            }
        }
        ```

    After saving the configuration, restart Claude Desktop. The Injective Docs tool will appear in Claude's available tools.

> **Warning**

  You **must** have NodeJs available on your `PATH`.
  You can verify this using the following command (Linux/ Mac):

  ```shell
  which node
  ```

  This should output the absolute path at which the NodeJs binary can be found,
  for example: `/Users/bguiz/.nvm/versions/node/v22.16.0/bin/node`.

  Additionally you **must** have a recent version of NodeJs installed.
  You can verify this using the following command:

  ```shell
  node -v
  ```

  The version number that is output should be newer than `v22`,
  for example: `v22.16.0`.
  If not, use [`nvm`](https://github.com/nvm-sh/nvm) (Linux/ Mac)
  or [`nvm-windows`](https://github.com/coreybutler/nvm-windows) (Windows) to manage your installation.

## Using the MCP server

Once connected, you can ask your AI tool to perform queries and transactions on Injective.

A list of all the available tools is available at
[`github.com/InjectiveLabs/mcp-server`](https://github.com/InjectiveLabs/mcp-server#tools).
Alternatively, your MCP client can list all available tools and their descriptions.
