"""Development-first API for the clean-slate King's Pawnhouse game."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from connector_gateway.auth import AuthError, ConnectorAuth
from arena_game import (
    EventDeckError,
    PawnhouseRepositoryError,
    Portfolio,
    PortfolioError,
    PostgresPawnhouseRepository,
    RuleStrategy,
    ChainReadError,
    EvmJsonRpcConfirmationReader,
    SettlementAccount,
    SettlementConfig,
    SettlementError,
    STANDARD_EVENT_DECK_ID,
    build_event_schedule,
    gold,
)


_Id = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
_OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$",
    ),
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateGameBody(_Body):
    game_id: _Id = Field(alias="gameId")
    event_seed: Annotated[str, StringConstraints(min_length=8, max_length=256)] = (
        Field(alias="eventSeed")
    )
    action_timeout_ms: int = Field(
        default=90_000,
        ge=100,
        le=900_000,
        alias="actionTimeoutMs",
    )
    round_count: int = Field(default=5, ge=1, le=10, alias="roundCount")
    event_deck_id: Literal["pawnhouse-standard-v1"] = Field(
        default=STANDARD_EVENT_DECK_ID,
        alias="eventDeckId",
    )
    event_mode: Literal["fixed_demo", "seeded_shuffle"] = Field(
        default="fixed_demo",
        alias="eventMode",
    )
    max_participants: int = Field(
        default=16,
        ge=2,
        le=64,
        alias="maxParticipants",
    )
    settlement: "SettlementConfigBody | None" = None


class PortfolioBody(_Body):
    cash: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    holdings: dict[str, int]


class RuleStrategyBody(_Body):
    intent: Literal["buy", "sell", "pass"]
    good: Literal["grain", "iron", "warhorse", "gems"]
    target_price: Annotated[str, StringConstraints(min_length=1, max_length=64)] = (
        Field(alias="targetPrice")
    )
    public_message: Annotated[str, StringConstraints(min_length=1, max_length=100)] = (
        Field(alias="publicMessage")
    )


class AddRuleParticipantBody(_Body):
    user_id: _Id = Field(alias="userId")
    agent_id: _Id = Field(alias="agentId")
    portfolio: PortfolioBody
    strategy: RuleStrategyBody


class AddHostedParticipantBody(_Body):
    agent_id: _Id = Field(alias="agentId")
    portfolio: PortfolioBody
    settlement_account: "SettlementAccountBody | None" = Field(
        default=None,
        alias="settlementAccount",
    )


class AddConnectorParticipantBody(_Body):
    agent_id: _Id = Field(alias="agentId")
    portfolio: PortfolioBody
    settlement_account: "SettlementAccountBody | None" = Field(
        default=None,
        alias="settlementAccount",
    )


class SettlementConfigBody(_Body):
    authorization_mode: Literal["none", "single_eip3009"] = Field(
        default="none",
        alias="authorizationMode",
    )
    chain_id: int | None = Field(default=None, ge=1, alias="chainId")
    token_address: str | None = Field(default=None, alias="tokenAddress")
    token_symbol: str | None = Field(default=None, alias="tokenSymbol")
    token_decimals: int | None = Field(
        default=None,
        ge=0,
        le=18,
        alias="tokenDecimals",
    )
    required_confirmations: int = Field(
        default=1,
        ge=1,
        le=100,
        alias="requiredConfirmations",
    )


class SettlementAccountBody(_Body):
    chain_id: int = Field(ge=1, alias="chainId")
    address: str
    custody_mode: Literal["wallet", "sandbox_guest"] = Field(
        alias="custodyMode",
    )


class RecordSettlementSubmissionBody(_Body):
    tx_hash: str = Field(alias="txHash")
    authorization_nonce: str = Field(alias="authorizationNonce")
    approved_intent_hash: Annotated[
        str,
        StringConstraints(
            min_length=71,
            max_length=71,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ] = Field(alias="approvedIntentHash")
    submission_source: Literal["wallet", "sandbox_guest"] = Field(
        alias="submissionSource",
    )
    human_confirmed: bool = Field(alias="humanConfirmed")


class RecordSettlementApprovalBody(_Body):
    approved_intent_hash: Annotated[
        str,
        StringConstraints(
            min_length=71,
            max_length=71,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ] = Field(alias="approvedIntentHash")
    authorization_nonce: Annotated[
        str,
        StringConstraints(
            min_length=66,
            max_length=66,
            pattern=r"^0x[0-9a-fA-F]{64}$",
        ),
    ] = Field(alias="authorizationNonce")
    approval_source: Literal["operator_cli"] = Field(alias="approvalSource")
    human_confirmed: bool = Field(alias="humanConfirmed")


CreateGameBody.model_rebuild()
AddHostedParticipantBody.model_rebuild()
AddConnectorParticipantBody.model_rebuild()


def _repository_error(exc: PawnhouseRepositoryError) -> HTTPException:
    code = str(exc)
    status = (
        404
        if code in {"game_not_found", "inventory_commit_not_found"}
        else 409
    )
    return HTTPException(status_code=status, detail={"code": code})


def create_pawnhouse_read_router(
    *,
    repository: PostgresPawnhouseRepository,
) -> APIRouter:
    """Expose public, read-only game state without mounting dev mutations."""

    router = APIRouter(tags=["pawnhouse"])

    @router.get("/api/v1/pawnhouse/games/{game_id}")
    async def game_state(game_id: _Id) -> dict[str, object]:
        try:
            return await repository.game_state(game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.get("/api/v1/pawnhouse/games/{game_id}/automation")
    async def game_automation(game_id: _Id) -> dict[str, object]:
        try:
            value = await repository.automation_state(game_id=game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None
        return {
            **value,
            "schemaVersion": "arena.pawnhouse-automation.v1",
        }

    @router.get("/api/v1/pawnhouse/games/{game_id}/timeline")
    async def game_timeline(
        game_id: _Id,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        events = await repository.timeline(game_id, after_sequence=after)
        return {
            "gameId": game_id,
            "events": events,
            "nextAfter": events[-1]["sequence"] if events else after,
            "schemaVersion": "arena.pawnhouse-timeline.v1",
        }

    @router.get("/api/v1/pawnhouse/games/{game_id}/runtime-run")
    async def hosted_runtime_run(game_id: _Id) -> dict[str, object]:
        value = await repository.hosted_run_status(game_id=game_id)
        if value is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "runtime_run_not_found"},
            )
        return {
            "gameId": game_id,
            "runtimeRunId": value["runtime_run_id"],
            "roundId": value["round_id"],
            "status": value["status"],
            "stage": value["stage"],
            "errorCode": value["safe_error_code"],
            "createdAt": value["created_at"],
            "startedAt": value["started_at"],
            "completedAt": value["completed_at"],
            "schemaVersion": "arena.pawnhouse-runtime-run.v1",
        }

    @router.get(
        "/api/v1/pawnhouse/games/{game_id}/settlement-intents"
    )
    async def settlement_intents(game_id: _Id) -> dict[str, object]:
        values = await repository.settlement_intents_for_game(
            game_id=game_id
        )
        return {
            "gameId": game_id,
            "settlementIntents": values,
            "total": len(values),
            "schemaVersion": "arena402.settlement-intent-list.v1",
        }

    @router.get(
        "/api/v1/pawnhouse/settlement-intents/"
        "{settlement_intent_id}/inventory-commit"
    )
    async def inventory_commit(
        settlement_intent_id: _OpaqueId,
    ) -> dict[str, object]:
        try:
            return await repository.inventory_commit_for_intent(
                settlement_intent_id=settlement_intent_id
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    return router


def create_pawnhouse_participation_router(
    *,
    repository: PostgresPawnhouseRepository,
    auth: ConnectorAuth,
) -> APIRouter:
    """Expose authenticated participant mutations without dev controls."""

    router = APIRouter(tags=["pawnhouse-participation"])

    @router.post(
        "/api/v1/pawnhouse/games/{game_id}/hosted-participants",
        status_code=201,
    )
    async def add_hosted_participant(
        game_id: _Id,
        body: AddHostedParticipantBody,
        request: Request,
    ) -> dict[str, object]:
        try:
            principal = await auth.authenticate(request)
            await auth.require_csrf(request, principal)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from None
        try:
            settlement_account = (
                None
                if body.settlement_account is None
                else SettlementAccount(
                    chain_id=body.settlement_account.chain_id,
                    address=body.settlement_account.address,
                    custody_mode=body.settlement_account.custody_mode,
                )
            )
            portfolio = Portfolio.initial(
                cash_atomic=gold(body.portfolio.cash),
                holdings=body.portfolio.holdings,
            )
            participant_id = await repository.add_hosted_participant(
                game_id=game_id,
                user_id=principal.user_id,
                agent_id=body.agent_id,
                portfolio=portfolio,
                settlement_account=settlement_account,
            )
        except (PortfolioError, SettlementError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": (
                        "invalid_portfolio"
                        if isinstance(exc, PortfolioError)
                        else str(exc)
                    )
                },
            ) from exc
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "runtimeKind": "hosted",
        }

    @router.post(
        "/api/v1/pawnhouse/games/{game_id}/connector-participants",
        status_code=201,
    )
    async def add_connector_participant(
        game_id: _Id,
        body: AddConnectorParticipantBody,
        request: Request,
    ) -> dict[str, object]:
        try:
            principal = await auth.authenticate(request)
            await auth.require_csrf(request, principal)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from None
        try:
            settlement_account = (
                None
                if body.settlement_account is None
                else SettlementAccount(
                    chain_id=body.settlement_account.chain_id,
                    address=body.settlement_account.address,
                    custody_mode=body.settlement_account.custody_mode,
                )
            )
            portfolio = Portfolio.initial(
                cash_atomic=gold(body.portfolio.cash),
                holdings=body.portfolio.holdings,
            )
            participant_id = await repository.add_connector_participant(
                game_id=game_id,
                user_id=principal.user_id,
                agent_id=body.agent_id,
                portfolio=portfolio,
                settlement_account=settlement_account,
            )
        except (PortfolioError, SettlementError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": (
                        "invalid_portfolio"
                        if isinstance(exc, PortfolioError)
                        else str(exc)
                    )
                },
            ) from exc
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "runtimeKind": "connector",
        }

    return router


def create_pawnhouse_router(
    *,
    repository: PostgresPawnhouseRepository,
    dev_token: str,
    auth: ConnectorAuth | None = None,
    confirmation_reader: EvmJsonRpcConfirmationReader | None = None,
) -> APIRouter:
    if len(dev_token) < 16:
        raise RuntimeError("ADX_ARENA_DEV_TOKEN must contain at least 16 characters")

    router = APIRouter(tags=["pawnhouse-dev"])
    router.include_router(
        create_pawnhouse_read_router(repository=repository)
    )
    if auth is not None:
        router.include_router(
            create_pawnhouse_participation_router(
                repository=repository,
                auth=auth,
            )
        )

    def authorize(value: str | None) -> None:
        if value != dev_token:
            raise HTTPException(
                status_code=403,
                detail={"code": "invalid_development_token"},
            )

    @router.post("/api/dev/pawnhouse/games", status_code=201)
    async def create_game(
        body: CreateGameBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            settlement_config = (
                SettlementConfig()
                if body.settlement is None
                else SettlementConfig(
                    authorization_mode=(
                        body.settlement.authorization_mode
                    ),
                    chain_id=body.settlement.chain_id,
                    token_address=body.settlement.token_address,
                    token_symbol=body.settlement.token_symbol,
                    token_decimals=body.settlement.token_decimals,
                    required_confirmations=(
                        body.settlement.required_confirmations
                    ),
                )
            )
            events = build_event_schedule(
                round_count=body.round_count,
                seed=body.event_seed,
                deck_id=body.event_deck_id,
                mode=body.event_mode,
            )
            return await repository.create_game(
                game_id=body.game_id,
                events=events,
                event_seed=body.event_seed,
                event_deck_id=body.event_deck_id,
                event_mode=body.event_mode,
                action_timeout_ms=body.action_timeout_ms,
                max_participants=body.max_participants,
                settlement_config=settlement_config,
            )
        except (EventDeckError, SettlementError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": str(exc)},
            ) from None
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post(
        "/api/dev/pawnhouse/games/{game_id}/rule-participants",
        status_code=201,
    )
    async def add_rule_participant(
        game_id: _Id,
        body: AddRuleParticipantBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            portfolio = Portfolio.initial(
                cash_atomic=gold(body.portfolio.cash),
                holdings=body.portfolio.holdings,
            )
            strategy = RuleStrategy(
                intent=body.strategy.intent,
                good=body.strategy.good,
                target_price_atomic=gold(body.strategy.target_price),
                public_message=body.strategy.public_message,
            )
            participant_id = await repository.add_rule_participant(
                game_id=game_id,
                user_id=body.user_id,
                agent_id=body.agent_id,
                portfolio=portfolio,
                strategy=strategy,
            )
        except PortfolioError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_portfolio"},
            ) from exc
        except (ValueError, PawnhouseRepositoryError) as exc:
            if isinstance(exc, PawnhouseRepositoryError):
                raise _repository_error(exc) from None
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_rule_participant"},
            ) from None
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "runtimeKind": "rule",
        }

    @router.post("/api/dev/pawnhouse/games/{game_id}/start")
    async def start_game(
        game_id: _Id,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.start_game(game_id=game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post("/api/dev/pawnhouse/games/{game_id}/run-rule-market")
    async def run_rule_market(
        game_id: _Id,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.run_rule_market(game_id=game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post(
        "/api/dev/pawnhouse/games/{game_id}/run-hosted-market",
        status_code=202,
    )
    async def run_agent_market(
        game_id: _Id,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.enqueue_agent_runtime_run(
                game_id=game_id
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post(
        "/api/dev/pawnhouse/settlement-intents/"
        "{settlement_intent_id}/approval"
    )
    async def record_settlement_approval(
        settlement_intent_id: _OpaqueId,
        body: RecordSettlementApprovalBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        if body.human_confirmed is not True:
            raise HTTPException(
                status_code=422,
                detail={"code": "human_confirmation_required"},
            )
        try:
            return await repository.record_settlement_approval(
                settlement_intent_id=settlement_intent_id,
                approved_intent_hash=body.approved_intent_hash,
                authorization_nonce=body.authorization_nonce,
                approval_source=body.approval_source,
            )
        except (SettlementError, PawnhouseRepositoryError) as exc:
            if isinstance(exc, PawnhouseRepositoryError):
                raise _repository_error(exc) from None
            raise HTTPException(
                status_code=422,
                detail={"code": str(exc)},
            ) from None

    @router.post(
        "/api/dev/pawnhouse/settlement-intents/"
        "{settlement_intent_id}/submission"
    )
    async def record_settlement_submission(
        settlement_intent_id: _OpaqueId,
        body: RecordSettlementSubmissionBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        if body.human_confirmed is not True:
            raise HTTPException(
                status_code=422,
                detail={"code": "human_confirmation_required"},
            )
        try:
            return await repository.record_settlement_submission(
                settlement_intent_id=settlement_intent_id,
                tx_hash=body.tx_hash,
                authorization_nonce=body.authorization_nonce,
                approved_intent_hash=body.approved_intent_hash,
                submission_source=body.submission_source,
            )
        except (SettlementError, PawnhouseRepositoryError) as exc:
            if isinstance(exc, PawnhouseRepositoryError):
                raise _repository_error(exc) from None
            raise HTTPException(
                status_code=422,
                detail={"code": str(exc)},
            ) from None

    @router.post(
        "/api/dev/pawnhouse/settlement-intents/"
        "{settlement_intent_id}/recover-confirmation"
    )
    async def recover_settlement_confirmation(
        settlement_intent_id: _OpaqueId,
        response: Response,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        if confirmation_reader is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "settlement_confirmation_unavailable"},
            )
        try:
            intent, tx_hash = (
                await repository.settlement_confirmation_target(
                    settlement_intent_id=settlement_intent_id
                )
            )
            confirmation = await confirmation_reader.read(intent, tx_hash)
            if confirmation is None:
                response.status_code = 202
                return await repository.mark_confirmation_timeout(
                    settlement_intent_id=settlement_intent_id
                )
            if confirmation.success is False:
                return await repository.record_chain_reverted(
                    settlement_intent_id=settlement_intent_id,
                    tx_hash=tx_hash,
                )
            if (
                confirmation.confirmation_count
                < intent.required_confirmations
            ):
                response.status_code = 202
                return await repository.mark_confirmation_timeout(
                    settlement_intent_id=settlement_intent_id
                )
            await repository.record_chain_confirmation(
                settlement_intent_id=settlement_intent_id,
                confirmation=confirmation,
            )
            return await repository.commit_confirmed_inventory(
                settlement_intent_id=settlement_intent_id
            )
        except ChainReadError:
            raise HTTPException(
                status_code=503,
                detail={"code": "settlement_chain_read_failed"},
            ) from None
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    return router


__all__ = [
    "create_pawnhouse_participation_router",
    "create_pawnhouse_read_router",
    "create_pawnhouse_router",
]
