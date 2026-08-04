"""Versioned Agent-driven Arena market actions and public artifacts.

These contracts express Agent economic choices.  They do not authorize a
market transition by themselves: every candidate still passes through the
Arena Result Sink and authoritative game-state validation.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .actions import (
    AcceptAction,
    BuyAction,
    DecideActionV1,
    GoodId,
    NegotiateActionV1,
    OrderQuantity,
    PassAction,
    PositiveFixedDecimal,
    ProposeAction,
    PublicMessage,
    RejectAction,
    SellAction,
)
from .tasks import Identifier, NonNegativeInt, UtcDateTime


ARENA_MARKET_PROTOCOL_VERSION_V1: Final = "arena.market.v1"
MAX_OUTBOUND_RFQ_V1: Final = 3


def _to_camel(field_name: str) -> str:
    head, *tail = field_name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _StrictMarketWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


# Market intent tasks deliberately reuse the canonical buy | sell | pass
# discriminator. Task-specific validation requires publicPrice and limitPrice
# for buy/sell; legacy arena.decide tasks reject publicPrice.
PublishBuyIntentActionV1: TypeAlias = BuyAction
PublishSellIntentActionV1: TypeAlias = SellAction
MarketIntentActionV1: TypeAlias = DecideActionV1


class MarketDirectoryEntryV1(_StrictMarketWireModel):
    intent_id: Identifier
    agent_id: Identifier
    display_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=100),
    ]
    side: Literal["buy", "sell"]
    good: GoodId
    quantity: OrderQuantity = 1
    public_price: PositiveFixedDecimal
    failed_negotiations: NonNegativeInt = 0
    expires_at: UtcDateTime

    @field_validator("display_name")
    @classmethod
    def reject_blank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("displayName must contain visible text")
        return value


class MarketDirectoryV1(_StrictMarketWireModel):
    schema_version: Literal["arena.market.v1"] = ARENA_MARKET_PROTOCOL_VERSION_V1
    market_session_id: Identifier
    game_id: Identifier
    round_id: Identifier
    entries: tuple[MarketDirectoryEntryV1, ...] = ()
    expires_at: UtcDateTime

    @field_validator("entries")
    @classmethod
    def require_unique_intents(
        cls,
        value: tuple[MarketDirectoryEntryV1, ...],
    ) -> tuple[MarketDirectoryEntryV1, ...]:
        ids = [entry.intent_id for entry in value]
        if len(ids) != len(set(ids)):
            raise ValueError("directory intent IDs must be unique")
        return value


class NegotiationRequestCandidateV1(_StrictMarketWireModel):
    target_intent_id: Identifier
    opening_price: PositiveFixedDecimal
    message: PublicMessage

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain visible text")
        return value


class RequestNegotiationsActionV1(_StrictMarketWireModel):
    action: Literal["request_negotiations"]
    requests: Annotated[
        list[NegotiationRequestCandidateV1],
        Field(min_length=1, max_length=MAX_OUTBOUND_RFQ_V1),
    ]

    @field_validator("requests")
    @classmethod
    def require_unique_targets(
        cls,
        value: list[NegotiationRequestCandidateV1],
    ) -> list[NegotiationRequestCandidateV1]:
        targets = [request.target_intent_id for request in value]
        if len(targets) != len(set(targets)):
            raise ValueError("RFQ target intent IDs must be unique")
        return value


MarketRfqActionV1: TypeAlias = Annotated[
    RequestNegotiationsActionV1 | PassAction,
    Field(discriminator="action"),
]


class EngageRequestActionV1(_StrictMarketWireModel):
    action: Literal["engage"]
    request_id: Identifier


class RejectAllRequestsActionV1(_StrictMarketWireModel):
    action: Literal["reject_all"]
    message: PublicMessage | None = None

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("message must contain visible text")
        return value


MarketSelectionActionV1: TypeAlias = Annotated[
    EngageRequestActionV1 | RejectAllRequestsActionV1,
    Field(discriminator="action"),
]

AgentDrivenMarketActionV1: TypeAlias = Annotated[
    BuyAction
    | SellAction
    | PassAction
    | RequestNegotiationsActionV1
    | EngageRequestActionV1
    | RejectAllRequestsActionV1
    | ProposeAction
    | AcceptAction
    | RejectAction,
    Field(discriminator="action"),
]


__all__ = [
    "ARENA_MARKET_PROTOCOL_VERSION_V1",
    "AgentDrivenMarketActionV1",
    "EngageRequestActionV1",
    "MAX_OUTBOUND_RFQ_V1",
    "MarketDirectoryEntryV1",
    "MarketDirectoryV1",
    "MarketIntentActionV1",
    "MarketRfqActionV1",
    "MarketSelectionActionV1",
    "NegotiateActionV1",
    "NegotiationRequestCandidateV1",
    "PublishBuyIntentActionV1",
    "PublishSellIntentActionV1",
    "RejectAllRequestsActionV1",
    "RequestNegotiationsActionV1",
]
