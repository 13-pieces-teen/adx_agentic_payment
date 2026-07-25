"""Read-only payment operations surface for an explicit GitHub admin allowlist."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from connector_gateway.auth import AuthError, ConnectorAuth

from .postgres import PostgresPaymentRepository


def create_payment_admin_router(
    *,
    auth: ConnectorAuth,
    repository: PostgresPaymentRepository,
    github_subjects: frozenset[str],
    facilitator_id: str | None,
    signer_mode: str,
) -> APIRouter:
    router = APIRouter(tags=["payment-admin"])

    @router.get("/api/v1/admin/payments")
    async def payment_admin(
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            principal = await auth.authenticate(request)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if (
            principal.identity_provider != "github"
            or principal.provider_subject not in github_subjects
        ):
            raise HTTPException(status_code=403, detail="admin_forbidden")
        snapshot = await repository.admin_snapshot(limit=limit)
        snapshot.update(
            {
                "facilitator": {
                    "id": facilitator_id,
                    "configured": facilitator_id is not None,
                },
                "signer": {
                    "mode": signer_mode,
                    "configured": signer_mode != "disabled",
                },
                "schemaVersion": "arena402.payment-admin.v1",
            }
        )
        return snapshot

    return router


__all__ = ["create_payment_admin_router"]
