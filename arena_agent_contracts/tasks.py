"""Immutable Arena-owned task envelopes and participant-view schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .actions import (
    DecideActionV1,
    GoodId,
    NonNegativeFixedDecimal,
    PositiveFixedDecimal,
    PublicMessage,
)

AGENT_TASK_SCHEMA_VERSION_V1: Final = "arena.agent-task.v1"


def _to_camel(field_name: str) -> str:
    head, *tail = field_name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


Identifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ShortText: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0)]
AllowedDecideAction: TypeAlias = Literal["buy", "sell", "pass"]


def _default_allowed_actions() -> list[AllowedDecideAction]:
    return ["buy", "sell", "pass"]


def _parse_aware_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        wire_value = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(wire_value)
        except ValueError as exc:
            raise ValueError("value must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("value must be an aware datetime or ISO-8601 string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("value must include a timezone")
    return parsed.astimezone(timezone.utc)


def _serialize_utc_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UtcDateTime: TypeAlias = Annotated[
    AwareDatetime,
    BeforeValidator(_parse_aware_datetime),
    PlainSerializer(_serialize_utc_datetime, return_type=str),
]


def _reject_floats(value: object) -> object:
    """Keep public event payloads JSON-safe without financial float values."""

    if isinstance(value, float):
        raise ValueError("floating-point values are not allowed in task snapshots")
    if isinstance(value, dict):
        for nested in value.values():
            _reject_floats(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_floats(nested)
    return value


PublicJsonValue: TypeAlias = Annotated[
    JsonValue,
    BeforeValidator(_reject_floats),
]


class ArenaPublicEventV1(_StrictWireModel):
    event_id: Identifier
    event_type: ShortText
    occurred_at: UtcDateTime
    summary: Annotated[str, StringConstraints(max_length=500)] = ""
    payload: dict[str, PublicJsonValue] = Field(default_factory=dict)


class ArenaReputationV1(_StrictWireModel):
    failed_negotiations: NonNegativeInt


class ArenaGoodRuleV1(_StrictWireModel):
    good: GoodId
    fixed_quantity: PositiveInt = 1
    price_decimal_places: Annotated[int, Field(ge=0, le=18)] = 6


class ArenaMarketActivityV1(_StrictWireModel):
    """Public, bounded activity summary for one traded good."""

    good: GoodId
    last_clearing_price: NonNegativeFixedDecimal | None = None
    volume: NonNegativeInt = 0
    buy_pressure_bps: int = Field(ge=-10_000, le=10_000)
    spread_bps: NonNegativeInt | None = None


class ArenaDecideLimitsV1(_StrictWireModel):
    allowed_actions: list[AllowedDecideAction] = Field(
        default_factory=_default_allowed_actions
    )
    allowed_goods: list[GoodId] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_values(self) -> "ArenaDecideLimitsV1":
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("allowedActions must not contain duplicates")
        if len(self.allowed_goods) != len(set(self.allowed_goods)):
            raise ValueError("allowedGoods must not contain duplicates")
        return self


class ArenaCompletedActionV1(_StrictWireModel):
    round_id: Identifier
    action: DecideActionV1


class ArenaTradeSummaryV1(_StrictWireModel):
    round_id: Identifier
    negotiation_id: Identifier
    role: Literal["buyer", "seller"]
    good: GoodId
    quantity: PositiveInt
    price: PositiveFixedDecimal


class ArenaDecideInputV1(_StrictWireModel):
    """Frozen participant view for one ``arena.decide`` task."""

    phase: Literal["decide"]
    game_id: Identifier
    round_id: Identifier
    round_index: PositiveInt
    cash: NonNegativeFixedDecimal
    holdings: dict[GoodId, NonNegativeInt]
    market: dict[GoodId, NonNegativeFixedDecimal]
    events: list[ArenaPublicEventV1] = Field(default_factory=list)
    reputation: ArenaReputationV1
    limits: ArenaDecideLimitsV1 = Field(default_factory=ArenaDecideLimitsV1)
    completed_actions: list[ArenaCompletedActionV1] = Field(default_factory=list)
    completed_trades: list[ArenaTradeSummaryV1] = Field(default_factory=list)
    goods: list[ArenaGoodRuleV1] = Field(default_factory=list)
    market_activity: list[ArenaMarketActivityV1] = Field(default_factory=list)
    deadline_at: UtcDateTime

    @model_validator(mode="after")
    def reject_duplicate_good_rules(self) -> "ArenaDecideInputV1":
        good_ids = [item.good for item in self.goods]
        if len(good_ids) != len(set(good_ids)):
            raise ValueError("goods must not contain duplicate good identifiers")
        return self


class ArenaPublicCounterpartyV1(_StrictWireModel):
    agent_id: Identifier | None = None
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = (
        None
    )
    failed_negotiations: NonNegativeInt


class ArenaNegotiationMessageV1(_StrictWireModel):
    turn_sequence: PositiveInt = Field(
        validation_alias=AliasChoices("turnSequence", "turn"),
        serialization_alias="turnSequence",
    )
    from_role: Literal["buyer", "seller"] = Field(
        validation_alias=AliasChoices("from", "fromRole"),
        serialization_alias="from",
    )
    action: Literal["propose", "accept", "reject"]
    price: PositiveFixedDecimal | None = None
    message: PublicMessage | None = None

    @model_validator(mode="after")
    def enforce_action_shape(self) -> "ArenaNegotiationMessageV1":
        if self.action == "propose" and self.price is None:
            raise ValueError("propose history entries require price")
        if self.action != "propose" and self.price is not None:
            raise ValueError(f"{self.action} history entries must not include price")
        if self.action == "accept" and self.message is not None:
            raise ValueError("accept history entries must not include message")
        if self.message is not None and not self.message.strip():
            raise ValueError("message must contain visible text")
        return self


class ArenaCounterpartyQuoteV1(_StrictWireModel):
    turn_sequence: PositiveInt
    from_role: Literal["buyer", "seller"] = Field(alias="from")
    price: PositiveFixedDecimal


class ArenaNegotiateInputV1(_StrictWireModel):
    """Frozen participant view for one ``arena.negotiate`` turn."""

    phase: Literal["negotiate"]
    game_id: Identifier
    round_id: Identifier
    round_index: PositiveInt
    negotiation_id: Identifier
    role: Literal["buyer", "seller"]
    good: GoodId
    quantity: PositiveInt
    limit_price: PositiveFixedDecimal | None = None
    cash: NonNegativeFixedDecimal
    inventory_available: NonNegativeInt
    counterparty: ArenaPublicCounterpartyV1
    events: list[ArenaPublicEventV1] = Field(default_factory=list)
    history: list[ArenaNegotiationMessageV1] = Field(default_factory=list)
    latest_counterparty_quote: ArenaCounterpartyQuoteV1 | None = None
    turn_sequence: PositiveInt
    remaining_turns: NonNegativeInt
    deadline_at: UtcDateTime

    @model_validator(mode="after")
    def validate_turn_context(self) -> "ArenaNegotiateInputV1":
        if self.latest_counterparty_quote is not None:
            if self.latest_counterparty_quote.from_role == self.role:
                raise ValueError("latestCounterpartyQuote must come from the counterparty")
            if self.latest_counterparty_quote.turn_sequence >= self.turn_sequence:
                raise ValueError(
                    "latestCounterpartyQuote must precede the current turnSequence"
                )
        return self


class ArenaAgentTaskV1(_StrictWireModel):
    """One immutable Arena-owned logical action.

    The model validates transport-level consistency only.  Arena remains
    responsible for phase, assets, quotes and all other business legality.
    """

    task_id: Identifier
    kind: Literal["arena.decide", "arena.negotiate"]
    schema_version: Literal["arena.agent-task.v1"]
    game_id: Identifier
    round_id: Identifier
    game_agent_id: Identifier
    negotiation_id: Identifier | None = None
    deadline_at: UtcDateTime
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    input_hash: Annotated[
        str,
        StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    input: ArenaDecideInputV1 | ArenaNegotiateInputV1

    @field_validator("input", mode="before")
    @classmethod
    def parse_input_for_kind(
        cls, value: object, info: ValidationInfo
    ) -> ArenaDecideInputV1 | ArenaNegotiateInputV1:
        kind = info.data.get("kind")
        if kind == "arena.decide":
            if isinstance(value, ArenaDecideInputV1):
                return value
            return ArenaDecideInputV1.model_validate(value)
        if kind == "arena.negotiate":
            if isinstance(value, ArenaNegotiateInputV1):
                return value
            return ArenaNegotiateInputV1.model_validate(value)
        raise ValueError("kind must be validated before input")

    @model_validator(mode="after")
    def enforce_envelope_consistency(self) -> "ArenaAgentTaskV1":
        if self.input.game_id != self.game_id:
            raise ValueError("input.gameId must match task gameId")
        if self.input.round_id != self.round_id:
            raise ValueError("input.roundId must match task roundId")
        if self.input.deadline_at != self.deadline_at:
            raise ValueError("input.deadlineAt must match task deadlineAt")

        if self.kind == "arena.decide":
            if not isinstance(self.input, ArenaDecideInputV1):
                raise ValueError("arena.decide requires ArenaDecideInputV1")
            if self.negotiation_id is not None:
                raise ValueError("arena.decide must not include negotiationId")
            expected_key = (
                f"{self.game_id}:{self.round_id}:{self.game_agent_id}:decide"
            )
        else:
            if not isinstance(self.input, ArenaNegotiateInputV1):
                raise ValueError("arena.negotiate requires ArenaNegotiateInputV1")
            if self.negotiation_id is None:
                raise ValueError("arena.negotiate requires negotiationId")
            if self.input.negotiation_id != self.negotiation_id:
                raise ValueError(
                    "input.negotiationId must match task negotiationId"
                )
            expected_key = (
                f"{self.game_id}:{self.round_id}:{self.negotiation_id}:"
                f"{self.input.turn_sequence}:{self.game_agent_id}:negotiate"
            )

        if self.idempotency_key != expected_key:
            raise ValueError(
                "idempotencyKey does not match the Arena v1 derivation"
            )
        return self


__all__ = [
    "AGENT_TASK_SCHEMA_VERSION_V1",
    "ArenaAgentTaskV1",
    "ArenaCompletedActionV1",
    "ArenaCounterpartyQuoteV1",
    "ArenaDecideInputV1",
    "ArenaDecideLimitsV1",
    "ArenaGoodRuleV1",
    "ArenaMarketActivityV1",
    "ArenaNegotiateInputV1",
    "ArenaNegotiationMessageV1",
    "ArenaPublicCounterpartyV1",
    "ArenaPublicEventV1",
    "ArenaReputationV1",
    "ArenaTradeSummaryV1",
    "Identifier",
    "PublicJsonValue",
    "UtcDateTime",
]
