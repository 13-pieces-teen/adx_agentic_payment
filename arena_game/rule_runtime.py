"""Deterministic internal Runtime used to prove the Arena state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .goods import GoodId
from .negotiation import NegotiationAction, NegotiationRole


RuleIntent = Literal["buy", "sell", "pass"]


@dataclass(frozen=True, slots=True)
class RuleStrategy:
    intent: RuleIntent
    good: GoodId
    target_price_atomic: int
    public_message: str


@dataclass(frozen=True, slots=True)
class RuleDecision:
    action: RuleIntent
    good: GoodId | None


class RuleRuntime:
    def __init__(self, strategy: RuleStrategy) -> None:
        self.strategy = strategy

    def decide(self) -> RuleDecision:
        return RuleDecision(
            action=self.strategy.intent,
            good=(
                None
                if self.strategy.intent == "pass"
                else self.strategy.good
            ),
        )

    def negotiate(
        self,
        *,
        role: NegotiationRole,
        sequence: int,
        latest_counterparty_price_atomic: int | None,
        max_turns: int,
    ) -> NegotiationAction:
        if sequence == 1:
            if role != "buyer":
                raise ValueError("buyer must take the first turn")
            return NegotiationAction(
                action="propose",
                price_atomic=self.strategy.target_price_atomic,
                message=self.strategy.public_message,
            )
        if latest_counterparty_price_atomic is None:
            raise ValueError("a response requires a counterparty quote")
        acceptable = (
            latest_counterparty_price_atomic
            <= self.strategy.target_price_atomic
            if role == "buyer"
            else latest_counterparty_price_atomic
            >= self.strategy.target_price_atomic
        )
        if acceptable:
            return NegotiationAction(action="accept")
        if sequence >= max_turns:
            return NegotiationAction(
                action="reject",
                message="价差未合，今日就此作罢。",
            )
        return NegotiationAction(
            action="propose",
            price_atomic=self.strategy.target_price_atomic,
            message=self.strategy.public_message,
        )


__all__ = [
    "RuleDecision",
    "RuleIntent",
    "RuleRuntime",
    "RuleStrategy",
]
