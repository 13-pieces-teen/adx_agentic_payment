"""Arena-mediated, bounded buyer/seller negotiation state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


NegotiationRole = Literal["buyer", "seller"]
NegotiationActionKind = Literal["propose", "accept", "reject"]


class NegotiationError(ValueError):
    pass


class NegotiationStatus(str, Enum):
    ACTIVE = "active"
    ACCEPTED_PENDING_SETTLEMENT = "accepted_pending_settlement"
    REJECTED = "rejected"
    TIMEOUT = "timeout"

    @property
    def terminal(self) -> bool:
        return self is not NegotiationStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class NegotiationAction:
    action: NegotiationActionKind
    price_atomic: int | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.action == "propose":
            if self.price_atomic is None or self.price_atomic <= 0:
                raise NegotiationError("propose requires a positive price")
            if self.message is None or not self.message.strip():
                raise NegotiationError("propose requires a public message")
        elif self.price_atomic is not None:
            raise NegotiationError(f"{self.action} cannot include a price")
        if self.action == "accept" and self.message is not None:
            raise NegotiationError("accept cannot include a message")
        if self.message is not None:
            if not self.message.strip() or len(self.message) > 100:
                raise NegotiationError(
                    "public negotiation messages must contain 1-100 characters"
                )


@dataclass(frozen=True, slots=True)
class NegotiationTurn:
    sequence: int
    role: NegotiationRole
    action: NegotiationAction


@dataclass(slots=True)
class Negotiation:
    negotiation_id: str
    buyer_participant_id: str
    seller_participant_id: str
    max_turns: int = 3
    status: NegotiationStatus = NegotiationStatus.ACTIVE
    turns: list[NegotiationTurn] = field(default_factory=list)
    accepted_price_atomic: int | None = None

    def __post_init__(self) -> None:
        if self.max_turns < 2 or self.max_turns > 6:
            raise NegotiationError("max_turns must be between 2 and 6")
        if self.buyer_participant_id == self.seller_participant_id:
            raise NegotiationError("buyer and seller must be different")

    @property
    def next_role(self) -> NegotiationRole:
        return "buyer" if len(self.turns) % 2 == 0 else "seller"

    def apply(
        self,
        *,
        role: NegotiationRole,
        action: NegotiationAction,
    ) -> NegotiationTurn:
        if self.status is not NegotiationStatus.ACTIVE:
            raise NegotiationError("negotiation is already terminal")
        if role != self.next_role:
            raise NegotiationError("action was submitted out of turn")
        sequence = len(self.turns) + 1
        if sequence > self.max_turns:
            raise NegotiationError("negotiation turn limit reached")
        if sequence == 1 and action.action != "propose":
            raise NegotiationError("buyer must open with a proposal")
        if sequence == self.max_turns and action.action == "propose":
            raise NegotiationError(
                "the last negotiation turn must accept or reject"
            )
        if action.action == "accept":
            if not self.turns:
                raise NegotiationError("there is no proposal to accept")
            previous = self.turns[-1]
            if previous.role == role or previous.action.action != "propose":
                raise NegotiationError(
                    "accept must target the counterparty's latest proposal"
                )
            self.status = NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
            self.accepted_price_atomic = previous.action.price_atomic
        elif action.action == "reject":
            self.status = NegotiationStatus.REJECTED

        turn = NegotiationTurn(sequence=sequence, role=role, action=action)
        self.turns.append(turn)
        return turn

    def expire(self) -> None:
        if self.status is NegotiationStatus.ACTIVE:
            self.status = NegotiationStatus.TIMEOUT


__all__ = [
    "Negotiation",
    "NegotiationAction",
    "NegotiationActionKind",
    "NegotiationError",
    "NegotiationRole",
    "NegotiationStatus",
    "NegotiationTurn",
]
