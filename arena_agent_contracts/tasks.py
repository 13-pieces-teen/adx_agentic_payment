"""Immutable Arena-owned task envelopes and participant-view schemas."""

from __future__ import annotations

import hashlib
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
    OrderQuantity,
    PositiveFixedDecimal,
    PublicMessage,
)

AGENT_TASK_SCHEMA_VERSION_V1: Final = "arena.agent-task.v1"


def market_select_request_set_token(request_ids: list[str]) -> str:
    """Derive a bounded task-key suffix without exposing request contents."""

    canonical = "\x00".join(sorted(request_ids)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
ArenaTaskKindV1: TypeAlias = Literal[
    "arena.decide",
    "arena.negotiate",
    "arena.market.intent",
    "arena.market.rfq",
    "arena.market.select",
]


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
    event_implied_final: dict[
        GoodId,
        NonNegativeFixedDecimal,
    ] = Field(default_factory=dict)
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
        if (
            self.event_implied_final
            and set(self.event_implied_final) != set(self.market)
        ):
            raise ValueError(
                "eventImpliedFinal must cover the same goods as market"
            )
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


class ArenaMarketIntentInputV1(ArenaDecideInputV1):
    """Frozen participant view for one real-Agent market intent task."""

    phase: Literal["market_intent"]
    market_protocol: Literal["agent_a2a.v1"] = "agent_a2a.v1"
    market_expires_at: UtcDateTime

    @model_validator(mode="after")
    def validate_market_lifetime(self) -> "ArenaMarketIntentInputV1":
        if self.market_expires_at <= self.deadline_at:
            raise ValueError(
                "marketExpiresAt must outlive the intent task deadline"
            )
        return self


class ArenaMarketDirectoryEntryV1(_StrictWireModel):
    """One public, unranked seller intent visible to a buyer Agent."""

    intent_id: Identifier
    agent_id: Identifier
    display_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=100),
    ]
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


class ArenaPriorRfqAttemptV1(_StrictWireModel):
    """One terminal RFQ attempt from the same frozen buyer directory."""

    attempt_sequence: Annotated[int, Field(ge=1, le=3)]
    target_intent_id: Identifier
    status: Literal[
        "rejected",
        "counterparty_busy",
        "expired",
        "cancelled",
        "timed_out",
    ]


class ArenaMarketRfqInputV1(_StrictWireModel):
    """Frozen public directory plus the buyer's private hard boundary."""

    phase: Literal["market_rfq"]
    market_protocol: Literal["agent_a2a.v1"] = "agent_a2a.v1"
    game_id: Identifier
    round_id: Identifier
    round_index: PositiveInt
    buyer_intent_id: Identifier
    good: GoodId
    quantity: OrderQuantity = 1
    public_price: PositiveFixedDecimal
    limit_price: PositiveFixedDecimal
    cash: NonNegativeFixedDecimal
    directory: list[ArenaMarketDirectoryEntryV1] = Field(default_factory=list)
    max_outbound_rfq: Annotated[int, Field(ge=1, le=3)] = 3
    attempt_sequence: Annotated[int, Field(ge=1, le=3)] = 1
    remaining_rfq_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    prior_attempts: list[ArenaPriorRfqAttemptV1] = Field(
        default_factory=list
    )
    events: list[ArenaPublicEventV1] = Field(default_factory=list)
    deadline_at: UtcDateTime

    @model_validator(mode="after")
    def validate_directory(self) -> "ArenaMarketRfqInputV1":
        intent_ids = [entry.intent_id for entry in self.directory]
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("directory must not contain duplicate intent IDs")
        if any(entry.good != self.good for entry in self.directory):
            raise ValueError("directory entries must match the buyer good")
        if self.remaining_rfq_attempts != 4 - self.attempt_sequence:
            raise ValueError(
                "remainingRfqAttempts must include the current bounded attempt"
            )
        if len(self.prior_attempts) != self.attempt_sequence - 1:
            raise ValueError(
                "priorAttempts must cover every earlier RFQ attempt"
            )
        sequences = [
            attempt.attempt_sequence for attempt in self.prior_attempts
        ]
        if sequences != list(range(1, self.attempt_sequence)):
            raise ValueError("priorAttempts must be ordered and contiguous")
        attempted_targets = {
            attempt.target_intent_id for attempt in self.prior_attempts
        }
        if attempted_targets.intersection(intent_ids):
            raise ValueError(
                "directory must contain only unattempted frozen targets"
            )
        return self


class ArenaInboundRfqV1(_StrictWireModel):
    """One public buyer request delivered to the target seller Agent."""

    request_id: Identifier
    buyer_agent_id: Identifier
    buyer_display_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=100),
    ]
    opening_price: PositiveFixedDecimal
    message: PublicMessage
    received_at: UtcDateTime

    @field_validator("buyer_display_name", "message")
    @classmethod
    def reject_blank_public_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("public text must contain visible characters")
        return value


class ArenaMarketSelectInputV1(_StrictWireModel):
    """Frozen inbound RFQs plus the seller's private hard boundary."""

    phase: Literal["market_select"]
    market_protocol: Literal["agent_a2a.v1"] = "agent_a2a.v1"
    game_id: Identifier
    round_id: Identifier
    round_index: PositiveInt
    seller_intent_id: Identifier
    good: GoodId
    quantity: OrderQuantity = 1
    public_price: PositiveFixedDecimal
    limit_price: PositiveFixedDecimal
    inventory_available: NonNegativeInt
    requests: list[ArenaInboundRfqV1] = Field(default_factory=list)
    max_engagements: Literal[1] = 1
    events: list[ArenaPublicEventV1] = Field(default_factory=list)
    deadline_at: UtcDateTime

    @model_validator(mode="after")
    def validate_requests(self) -> "ArenaMarketSelectInputV1":
        request_ids = [request.request_id for request in self.requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("requests must not contain duplicate request IDs")
        return self


class ArenaAgentTaskV1(_StrictWireModel):
    """One immutable Arena-owned logical action.

    The model validates transport-level consistency only.  Arena remains
    responsible for phase, assets, quotes and all other business legality.
    """

    task_id: Identifier
    kind: ArenaTaskKindV1
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
    input: (
        ArenaDecideInputV1
        | ArenaNegotiateInputV1
        | ArenaMarketIntentInputV1
        | ArenaMarketRfqInputV1
        | ArenaMarketSelectInputV1
    )

    @field_validator("input", mode="before")
    @classmethod
    def parse_input_for_kind(
        cls, value: object, info: ValidationInfo
    ) -> (
        ArenaDecideInputV1
        | ArenaNegotiateInputV1
        | ArenaMarketIntentInputV1
        | ArenaMarketRfqInputV1
        | ArenaMarketSelectInputV1
    ):
        kind = info.data.get("kind")
        if kind == "arena.decide":
            if type(value) is ArenaDecideInputV1:
                return value
            return ArenaDecideInputV1.model_validate(value)
        if kind == "arena.negotiate":
            if isinstance(value, ArenaNegotiateInputV1):
                return value
            return ArenaNegotiateInputV1.model_validate(value)
        if kind == "arena.market.intent":
            if isinstance(value, ArenaMarketIntentInputV1):
                return value
            return ArenaMarketIntentInputV1.model_validate(value)
        if kind == "arena.market.rfq":
            if isinstance(value, ArenaMarketRfqInputV1):
                return value
            return ArenaMarketRfqInputV1.model_validate(value)
        if kind == "arena.market.select":
            if isinstance(value, ArenaMarketSelectInputV1):
                return value
            return ArenaMarketSelectInputV1.model_validate(value)
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
            if type(self.input) is not ArenaDecideInputV1:
                raise ValueError("arena.decide requires ArenaDecideInputV1")
            if self.negotiation_id is not None:
                raise ValueError("arena.decide must not include negotiationId")
            expected_key = (
                f"{self.game_id}:{self.round_id}:{self.game_agent_id}:decide"
            )
        elif self.kind == "arena.negotiate":
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
        else:
            input_types = {
                "arena.market.intent": ArenaMarketIntentInputV1,
                "arena.market.rfq": ArenaMarketRfqInputV1,
                "arena.market.select": ArenaMarketSelectInputV1,
            }
            expected_type = input_types[self.kind]
            if not isinstance(self.input, expected_type):
                raise ValueError(f"{self.kind} requires {expected_type.__name__}")
            if self.negotiation_id is not None:
                raise ValueError(f"{self.kind} must not include negotiationId")
            suffix = self.kind.removeprefix("arena.market.")
            base_key = (
                f"{self.game_id}:{self.round_id}:{self.game_agent_id}:"
                f"market-{suffix}"
            )
            if self.kind == "arena.market.rfq":
                assert isinstance(self.input, ArenaMarketRfqInputV1)
                expected_key = (
                    f"{base_key}:{self.input.attempt_sequence}"
                )
                if (
                    self.input.attempt_sequence == 1
                    and self.idempotency_key == base_key
                ):
                    expected_key = base_key
            else:
                expected_key = base_key
                if self.kind == "arena.market.select":
                    assert isinstance(
                        self.input,
                        ArenaMarketSelectInputV1,
                    )
                    request_set_token = market_select_request_set_token(
                        [
                            request.request_id
                            for request in self.input.requests
                        ]
                    )
                    derived_key = f"{base_key}:{request_set_token}"
                    if self.idempotency_key != base_key:
                        expected_key = derived_key

        if self.idempotency_key != expected_key:
            raise ValueError(
                "idempotencyKey does not match the Arena v1 derivation"
            )
        return self


__all__ = [
    "AGENT_TASK_SCHEMA_VERSION_V1",
    "ArenaAgentTaskV1",
    "ArenaInboundRfqV1",
    "ArenaMarketDirectoryEntryV1",
    "ArenaMarketIntentInputV1",
    "ArenaPriorRfqAttemptV1",
    "ArenaMarketRfqInputV1",
    "ArenaMarketSelectInputV1",
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
    "ArenaTaskKindV1",
    "market_select_request_set_token",
]
