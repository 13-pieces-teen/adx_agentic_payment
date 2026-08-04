"""Versioned structured actions returned by Arena Agent runtimes.

These models intentionally describe candidate actions only.  Whether an
action is legal for the current game state remains an Arena responsibility.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
)

MAX_DECIMAL_DIGITS = 38
MAX_DECIMAL_PLACES = 18
MAX_PUBLIC_MESSAGE_LENGTH = 100

_FIXED_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _to_camel(field_name: str) -> str:
    head, *tail = field_name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _StrictWireModel(BaseModel):
    """Shared configuration for immutable, strict JSON wire models."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


def _parse_fixed_decimal(value: object) -> Decimal:
    """Accept a canonical fixed-point string and reject float coercion.

    The wire contract deliberately excludes signs and exponent notation.  A
    later Arena validation step applies game-specific precision and amount
    bounds; this parser only establishes deterministic decimal semantics.
    """

    if not isinstance(value, str) or not _FIXED_DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(
            "value must be a non-negative fixed-point decimal string"
        )

    integer_part, separator, fractional_part = value.partition(".")
    if len(integer_part) + len(fractional_part) > MAX_DECIMAL_DIGITS:
        raise ValueError(
            f"value must contain at most {MAX_DECIMAL_DIGITS} decimal digits"
        )
    if separator and len(fractional_part) > MAX_DECIMAL_PLACES:
        raise ValueError(
            f"value must contain at most {MAX_DECIMAL_PLACES} decimal places"
        )

    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - regex is the guard.
        raise ValueError("value must be a valid fixed-point decimal") from exc
    if not parsed.is_finite():
        raise ValueError("value must be finite")
    return parsed


def _serialize_fixed_decimal(value: Decimal) -> str:
    return format(value, "f")


FixedDecimal: TypeAlias = Annotated[
    Decimal,
    BeforeValidator(_parse_fixed_decimal),
    PlainSerializer(_serialize_fixed_decimal, return_type=str),
]

NonNegativeFixedDecimal: TypeAlias = Annotated[
    Decimal,
    BeforeValidator(_parse_fixed_decimal),
    Field(ge=Decimal("0")),
    PlainSerializer(_serialize_fixed_decimal, return_type=str),
]

PositiveFixedDecimal: TypeAlias = Annotated[
    Decimal,
    BeforeValidator(_parse_fixed_decimal),
    Field(gt=Decimal("0")),
    PlainSerializer(_serialize_fixed_decimal, return_type=str),
]

OrderQuantity: TypeAlias = Literal[1]

GoodId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

PublicMessage: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_PUBLIC_MESSAGE_LENGTH),
]


class BuyAction(_StrictWireModel):
    action: Literal["buy"]
    good: GoodId
    quantity: OrderQuantity = Field(
        default=1,
        exclude_if=lambda value: value == 1,
    )
    limit_price: PositiveFixedDecimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    public_price: PositiveFixedDecimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    message: PublicMessage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("message must contain visible text")
        return value


class SellAction(_StrictWireModel):
    action: Literal["sell"]
    good: GoodId
    quantity: OrderQuantity = Field(
        default=1,
        exclude_if=lambda value: value == 1,
    )
    limit_price: PositiveFixedDecimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    public_price: PositiveFixedDecimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    message: PublicMessage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("message must contain visible text")
        return value


class PassAction(_StrictWireModel):
    action: Literal["pass"]


class ProposeAction(_StrictWireModel):
    action: Literal["propose"]
    price: PositiveFixedDecimal
    message: PublicMessage

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain visible text")
        return value


class AcceptAction(_StrictWireModel):
    action: Literal["accept"]


class RejectAction(_StrictWireModel):
    action: Literal["reject"]
    message: PublicMessage | None = None

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("message must contain visible text")
        return value


DecideActionV1: TypeAlias = Annotated[
    BuyAction | SellAction | PassAction,
    Field(discriminator="action"),
]

NegotiateActionV1: TypeAlias = Annotated[
    ProposeAction | AcceptAction | RejectAction,
    Field(discriminator="action"),
]

AgentActionV1: TypeAlias = Annotated[
    BuyAction
    | SellAction
    | PassAction
    | ProposeAction
    | AcceptAction
    | RejectAction,
    Field(discriminator="action"),
]


__all__ = [
    "AcceptAction",
    "AgentActionV1",
    "BuyAction",
    "DecideActionV1",
    "FixedDecimal",
    "GoodId",
    "MAX_DECIMAL_DIGITS",
    "MAX_DECIMAL_PLACES",
    "MAX_PUBLIC_MESSAGE_LENGTH",
    "NegotiateActionV1",
    "NonNegativeFixedDecimal",
    "OrderQuantity",
    "PassAction",
    "PositiveFixedDecimal",
    "ProposeAction",
    "PublicMessage",
    "RejectAction",
    "SellAction",
]
