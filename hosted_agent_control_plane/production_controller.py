"""Production Credential Controller process entrypoint."""

from __future__ import annotations

import asyncio
import os
import signal

from hosted_agent_runtime.production_secrets import (
    build_production_secret_controller,
    close_secret_port,
    initialize_secret_port,
)

from .credential_controller import (
    DurableCredentialController,
    PostgresCredentialLifecycleRepository,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    database_url = _required("ADX_CREDENTIAL_CONTROLLER_DATABASE_URL")
    repository = PostgresCredentialLifecycleRepository(database_url)
    secret_controller = build_production_secret_controller(database_url)
    controller = DurableCredentialController(
        repository=repository,
        secret_controller=secret_controller,
        controller_id=os.getenv(
            "ADX_CREDENTIAL_CONTROLLER_ID"
        ) or None,
        lease_seconds=int(
            os.getenv(
                "ADX_CREDENTIAL_CONTROLLER_LEASE_SECONDS",
                "60",
            )
        ),
        retry_seconds=int(
            os.getenv(
                "ADX_CREDENTIAL_CONTROLLER_RETRY_SECONDS",
                "30",
            )
        ),
    )
    try:
        await repository.initialize()
        await initialize_secret_port(secret_controller)
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_name, controller.stop)
            except NotImplementedError:
                pass
        await controller.run_forever(
            poll_seconds=float(
                os.getenv(
                    "ADX_CREDENTIAL_CONTROLLER_POLL_SECONDS",
                    "1",
                )
            )
        )
    finally:
        await close_secret_port(secret_controller)
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
