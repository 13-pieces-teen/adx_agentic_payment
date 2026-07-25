"""Clean-slate game aggregate for the King's Pawnhouse."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .events import WorldEvent, WorldSnapshot, WorldState, schedule_commitment
from .portfolio import Portfolio, distribute_balanced_portfolios
from .ranking import RankingEntry, calculate_rankings


class GameError(ValueError):
    pass


class GamePhase(str, Enum):
    REGISTRATION = "registration"
    PORTFOLIO_SETUP = "portfolio_setup"
    PORTFOLIO_LOCKED = "portfolio_locked"
    RUNNING = "running"
    FINAL_VALUATION = "final_valuation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RoundPhase(str, Enum):
    EVENT_REVEAL = "event_reveal"
    DECIDE = "decide"
    MATCH = "match"
    NEGOTIATE = "negotiate"
    SETTLE = "settle"
    ROUND_CLOSE = "round_close"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class GameConfig:
    round_count: int = 5
    action_timeout_ms: int = 90_000
    max_negotiation_turns: int = 3
    min_participants: int = 2
    max_participants: int = 16
    portfolio_mode: str = "manual"

    def __post_init__(self) -> None:
        if self.round_count < 1 or self.round_count > 20:
            raise GameError("round_count must be between 1 and 20")
        if self.action_timeout_ms < 100 or self.action_timeout_ms > 900_000:
            raise GameError("action_timeout_ms is outside the Arena bound")
        if self.max_negotiation_turns < 2 or self.max_negotiation_turns > 6:
            raise GameError("max_negotiation_turns must be between 2 and 6")
        if (
            self.min_participants < 2
            or self.max_participants < self.min_participants
        ):
            raise GameError("invalid participant bounds")
        if self.portfolio_mode not in {"manual", "balanced_auto"}:
            raise GameError("invalid portfolio_mode")


@dataclass(slots=True)
class GameParticipant:
    user_id: str
    agent_id: str
    portfolio: Portfolio | None = None


@dataclass(slots=True)
class PawnhouseGame:
    game_id: str
    config: GameConfig
    events: tuple[WorldEvent, ...]
    event_seed: str
    phase: GamePhase = GamePhase.REGISTRATION
    current_round: int = 0
    round_phase: RoundPhase | None = None
    participants: dict[str, GameParticipant] = field(default_factory=dict)
    _users: set[str] = field(default_factory=set, repr=False)
    world: WorldState = field(init=False)
    event_commitment: str = field(init=False)
    rankings: tuple[RankingEntry, ...] = ()

    def __post_init__(self) -> None:
        if not self.game_id:
            raise GameError("game_id is required")
        if not self.event_seed:
            raise GameError("event seed is required")
        if len(self.events) != self.config.round_count:
            raise GameError("one scheduled event is required for every round")
        reveal_rounds = [event.reveal_round for event in self.events]
        if reveal_rounds != list(range(1, self.config.round_count + 1)):
            raise GameError("event schedule must cover each round exactly once")
        catalog = {event.event_id: event for event in self.events}
        if len(catalog) != len(self.events):
            raise GameError("event schedule cannot contain duplicate event ids")
        self.world = WorldState(catalog)
        self.event_commitment = schedule_commitment(
            self.events,
            seed=self.event_seed,
        )

    def join(self, *, user_id: str, agent_id: str) -> None:
        if self.phase not in {
            GamePhase.REGISTRATION,
            GamePhase.PORTFOLIO_SETUP,
        }:
            raise GameError("game no longer accepts participants")
        if not user_id or not agent_id:
            raise GameError("user_id and agent_id are required")
        if user_id in self._users:
            raise GameError("one user may join a game only once")
        if agent_id in self.participants:
            raise GameError("an agent may join a game only once")
        if len(self.participants) >= self.config.max_participants:
            raise GameError("game participant limit reached")
        self._users.add(user_id)
        self.participants[agent_id] = GameParticipant(
            user_id=user_id,
            agent_id=agent_id,
        )
        self.phase = GamePhase.PORTFOLIO_SETUP

    def configure_portfolio(self, *, agent_id: str, portfolio: Portfolio) -> None:
        if self.phase is not GamePhase.PORTFOLIO_SETUP:
            raise GameError("portfolios can only change during setup")
        participant = self.participants.get(agent_id)
        if participant is None:
            raise GameError("agent is not a game participant")
        participant.portfolio = portfolio

    def lock_portfolios(self) -> None:
        if self.phase is not GamePhase.PORTFOLIO_SETUP:
            raise GameError("game is not in portfolio setup")
        if len(self.participants) < self.config.min_participants:
            raise GameError("not enough participants")
        if self.config.portfolio_mode == "balanced_auto":
            portfolios = distribute_balanced_portfolios(
                tuple(self.participants),
                seed=self.event_seed,
            )
            for agent_id, portfolio in portfolios.items():
                self.participants[agent_id].portfolio = portfolio
        if any(item.portfolio is None for item in self.participants.values()):
            raise GameError("every participant must configure a portfolio")
        self.phase = GamePhase.PORTFOLIO_LOCKED

    def start(self) -> WorldSnapshot:
        if self.phase is not GamePhase.PORTFOLIO_LOCKED:
            raise GameError("portfolios must be locked before start")
        self.phase = GamePhase.RUNNING
        self.current_round = 1
        self.round_phase = RoundPhase.EVENT_REVEAL
        return self.reveal_current_event()

    def reveal_current_event(self) -> WorldSnapshot:
        if (
            self.phase is not GamePhase.RUNNING
            or self.round_phase is not RoundPhase.EVENT_REVEAL
        ):
            raise GameError("game is not ready to reveal an event")
        event = self.events[self.current_round - 1]
        snapshot = self.world.reveal(
            event.event_id,
            round_index=self.current_round,
        )
        self.round_phase = RoundPhase.DECIDE
        return snapshot

    def move_round_phase(self, expected: RoundPhase, target: RoundPhase) -> None:
        if self.phase is not GamePhase.RUNNING or self.round_phase is not expected:
            raise GameError("invalid round phase transition")
        allowed = {
            RoundPhase.DECIDE: RoundPhase.MATCH,
            RoundPhase.MATCH: RoundPhase.NEGOTIATE,
            RoundPhase.NEGOTIATE: RoundPhase.SETTLE,
            RoundPhase.SETTLE: RoundPhase.ROUND_CLOSE,
            RoundPhase.ROUND_CLOSE: RoundPhase.COMPLETED,
        }
        if allowed.get(expected) is not target:
            raise GameError("invalid round phase transition")
        self.round_phase = target

    def next_round(self) -> WorldSnapshot | None:
        if (
            self.phase is not GamePhase.RUNNING
            or self.round_phase is not RoundPhase.COMPLETED
        ):
            raise GameError("current round is not complete")
        if self.current_round >= self.config.round_count:
            self.phase = GamePhase.FINAL_VALUATION
            self.round_phase = None
            return None
        self.current_round += 1
        self.round_phase = RoundPhase.EVENT_REVEAL
        return self.reveal_current_event()

    def complete(self) -> tuple[RankingEntry, ...]:
        if self.phase is not GamePhase.FINAL_VALUATION:
            raise GameError("game is not ready for final valuation")
        snapshot = self.world.snapshot(self.config.round_count)
        portfolios: dict[str, Portfolio] = {}
        for agent_id, participant in self.participants.items():
            if participant.portfolio is None:
                raise GameError("participant portfolio is missing")
            portfolios[agent_id] = participant.portfolio
        self.rankings = calculate_rankings(
            portfolios,
            snapshot.final_prices,
        )
        self.phase = GamePhase.COMPLETED
        return self.rankings

    def portfolio_snapshot(self) -> Mapping[str, Portfolio]:
        return {
            agent_id: participant.portfolio
            for agent_id, participant in self.participants.items()
            if participant.portfolio is not None
        }


__all__ = [
    "GameConfig",
    "GameError",
    "GameParticipant",
    "GamePhase",
    "PawnhouseGame",
    "RoundPhase",
]
