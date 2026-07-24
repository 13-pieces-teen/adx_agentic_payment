"""Hosted Worker process entrypoint."""

from __future__ import annotations

import asyncio
import os
import signal

from .postgres_worker import (
    DurableHostedWorker,
    PostgresHostedWorkerRepository,
)
from .production_providers import build_production_provider_bundle
from .secret_store import TencentSecretReader, TencentSsmSettings


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


async def main() -> None:
    if not _true("ADX_TENCENT_SSM_IAM_VERIFIED"):
        raise RuntimeError(
            "Role-specific Tencent SSM IAM validation is required"
        )
    repository = PostgresHostedWorkerRepository(
        _required("ADX_HOSTED_WORKER_DATABASE_URL")
    )
    providers = build_production_provider_bundle()
    reader = TencentSecretReader(
        TencentSsmSettings(
            region=os.getenv(
                "ADX_TENCENT_SSM_REGION",
                "ap-guangzhou",
            ),
            deployment_iam_verified=True,
        )
    )
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
    await repository.initialize()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, worker.stop)
        except NotImplementedError:  # Windows development only
            pass
    try:
        await worker.run_forever(
            poll_seconds=float(
                os.getenv("ADX_HOSTED_WORKER_POLL_SECONDS", "1")
            )
        )
    finally:
        await providers.close()
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
