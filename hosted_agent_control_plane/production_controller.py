"""Production Credential Controller process entrypoint."""

from __future__ import annotations

import asyncio
import os
import signal

from hosted_agent_runtime.secret_store import (
    TencentSecretController,
    TencentSsmSettings,
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


def _true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


async def main() -> None:
    if not _true("ADX_TENCENT_SSM_IAM_VERIFIED"):
        raise RuntimeError(
            "Role-specific Tencent SSM IAM validation is required"
        )
    repository = PostgresCredentialLifecycleRepository(
        _required("ADX_CREDENTIAL_CONTROLLER_DATABASE_URL")
    )
    controller = DurableCredentialController(
        repository=repository,
        secret_controller=TencentSecretController(
            TencentSsmSettings(
                region=os.getenv(
                    "ADX_TENCENT_SSM_REGION",
                    "ap-guangzhou",
                ),
                recovery_window_days=int(
                    os.getenv(
                        "ADX_TENCENT_SSM_RECOVERY_WINDOW_DAYS",
                        "0",
                    )
                ),
                deployment_iam_verified=True,
            )
        ),
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
    await repository.initialize()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, controller.stop)
        except NotImplementedError:
            pass
    try:
        await controller.run_forever(
            poll_seconds=float(
                os.getenv(
                    "ADX_CREDENTIAL_CONTROLLER_POLL_SECONDS",
                    "1",
                )
            )
        )
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
