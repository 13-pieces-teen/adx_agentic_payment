"""Hosted Worker process entrypoint."""

from __future__ import annotations

import asyncio
import os
import signal

from db.schema_identity import verify_schema_identity

from .postgres_worker import (
    DurableHostedWorker,
    PostgresHostedWorkerRepository,
)
from .production_providers import build_production_provider_bundle
from .production_secrets import (
    build_production_secret_reader,
    close_secret_port,
    initialize_secret_port,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    database_url = _required("ADX_HOSTED_WORKER_DATABASE_URL")
    repository = PostgresHostedWorkerRepository(database_url)
    providers = build_production_provider_bundle()
    reader = build_production_secret_reader(database_url)
    worker = DurableHostedWorker(
        repository=repository,
        providers=providers,
        secret_reader=reader,
        worker_id=os.getenv("ADX_HOSTED_WORKER_ID") or None,
        lease_seconds=int(
            os.getenv("ADX_HOSTED_WORKER_LEASE_SECONDS", "600")
        ),
        task_concurrency=int(
            os.getenv("ADX_HOSTED_WORKER_TASK_CONCURRENCY", "5")
        ),
    )
    try:
        await repository.initialize()
        await verify_schema_identity(repository._pool)
        await initialize_secret_port(reader)
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_name, worker.stop)
            except NotImplementedError:  # Windows development only
                pass
        await worker.run_forever(
            poll_seconds=float(
                os.getenv("ADX_HOSTED_WORKER_POLL_SECONDS", "1")
            )
        )
    finally:
        await providers.close()
        await close_secret_port(reader)
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
