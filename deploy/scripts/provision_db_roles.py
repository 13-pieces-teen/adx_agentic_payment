"""Create least-privilege PostgreSQL login roles for Arena 402 processes."""

from __future__ import annotations

import asyncio
import os

import asyncpg


ROLE_BINDINGS = {
    "adx_api_login": (
        "ADX_API_DATABASE_PASSWORD",
        ("adx_connector_gateway", "adx_arena_api"),
    ),
    "adx_hosted_worker_login": (
        "ADX_HOSTED_WORKER_DATABASE_PASSWORD",
        ("adx_hosted_worker",),
    ),
    "adx_arena_core_login": (
        "ADX_ARENA_CORE_DATABASE_PASSWORD",
        ("adx_arena_core",),
    ),
    "adx_settlement_login": (
        "ADX_SETTLEMENT_DATABASE_PASSWORD",
        ("adx_settlement",),
    ),
    "adx_credential_controller_login": (
        "ADX_CREDENTIAL_CONTROLLER_DATABASE_PASSWORD",
        ("adx_credential_controller",),
    ),
}


async def main() -> None:
    admin_url = os.environ["ADX_DATABASE_ADMIN_URL"]
    connection = await asyncpg.connect(admin_url, command_timeout=30)
    try:
        for login, (password_env, memberships) in ROLE_BINDINGS.items():
            password = os.environ[password_env]
            exists = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)",
                login,
            )
            if not exists:
                await connection.execute(
                    f"CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION"
                )
            alter_sql = await connection.fetchval(
                """
                SELECT format(
                    'ALTER ROLE %I WITH PASSWORD %L',
                    $1::text,
                    $2::text
                )
                """,
                login,
                password,
            )
            await connection.execute(alter_sql)
            for membership in memberships:
                await connection.execute(
                    f"GRANT {membership} TO {login}"
                )
        print("Arena 402 database login roles provisioned", flush=True)
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
