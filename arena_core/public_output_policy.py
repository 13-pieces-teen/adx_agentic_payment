"""Sanitize public Runtime messages before any durable write.

The policy is deliberately deterministic. A rejected message is never returned
to callers, included in an exception, or copied into diagnostic metadata.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from .ingress_security import sensitive_text_reason, unsafe_unicode_reason


PUBLIC_OUTPUT_POLICY_VERSION: Final[str] = "arena.public-output.v1"
MAX_PUBLIC_MESSAGE_CHARS: Final[int] = 100
_MIN_STRATEGY_MATCH_CHARS: Final[int] = 12
_MAX_STRATEGY_SCAN_CHARS: Final[int] = 32_000

_HTML_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"</?[a-z][^>]*>|&#?\w+;", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class PublicOutputDecision:
    """Only this sanitized value may be passed to persistence."""

    message: str | None
    message_replaced: bool
    policy_version: str = PUBLIC_OUTPUT_POLICY_VERSION
    replacement_reason: str | None = None


class PublicOutputPolicy:
    """Reject obvious secret, PII, markup, and strategy disclosure."""

    def sanitize(
        self,
        *,
        message: str | None,
        action: str,
        price: Decimal | str | int | None = None,
        role: str | None = None,
        strategy_instructions: str | None = None,
    ) -> PublicOutputDecision:
        if message is None:
            return PublicOutputDecision(
                message=None,
                message_replaced=False,
            )

        normalized = unicodedata.normalize("NFKC", message).strip()
        reason = self._replacement_reason(normalized, strategy_instructions)
        if reason is None:
            return PublicOutputDecision(
                message=normalized,
                message_replaced=False,
            )

        return PublicOutputDecision(
            message=self._neutral_message(action=action, price=price, role=role),
            message_replaced=True,
            replacement_reason=reason,
        )

    def _replacement_reason(
        self, message: str, strategy_instructions: str | None
    ) -> str | None:
        if not message:
            return "empty"
        if len(message) > MAX_PUBLIC_MESSAGE_CHARS:
            return "length"
        unicode_reason = unsafe_unicode_reason(message)
        if unicode_reason == "unsafe_unicode_control":
            return "control_character"
        if unicode_reason == "unsafe_unicode_format":
            return "format_character"
        if _HTML_PATTERN.search(message):
            return "markup"
        if sensitive_text_reason(message) is not None:
            return "secret_or_pii"
        if strategy_instructions and self._copies_strategy(
            message, strategy_instructions
        ):
            return "strategy_copy"
        return None

    @staticmethod
    def _compact(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(character for character in normalized if character.isalnum())

    def _copies_strategy(self, message: str, strategy: str) -> bool:
        compact_message = self._compact(message)
        compact_strategy = self._compact(strategy)
        if len(compact_message) < _MIN_STRATEGY_MATCH_CHARS:
            return False
        if len(compact_strategy) < _MIN_STRATEGY_MATCH_CHARS:
            return False

        # An unexpectedly large private strategy should fail closed rather
        # than turning one public message into an unbounded scan.
        if len(compact_strategy) > _MAX_STRATEGY_SCAN_CHARS:
            return True
        if compact_message in compact_strategy:
            return True

        # Detect a copied contiguous fragment even when the model adds an
        # innocent-looking prefix/suffix. The public message is capped at 100
        # characters, so this loop is bounded independently of strategy size.
        window_count = len(compact_message) - _MIN_STRATEGY_MATCH_CHARS + 1
        for offset in range(window_count):
            fragment = compact_message[
                offset : offset + _MIN_STRATEGY_MATCH_CHARS
            ]
            if fragment in compact_strategy:
                return True
        return False

    @staticmethod
    def _neutral_message(
        *,
        action: str,
        price: Decimal | str | int | None,
        role: str | None,
    ) -> str:
        safe_role = role if role in {"buyer", "seller"} else "agent"
        if action == "propose" and price is not None:
            try:
                normalized_price = format(Decimal(str(price)), "f")
            except (InvalidOperation, ValueError):
                normalized_price = "the stated price"
            return f"{safe_role} proposes {normalized_price}."
        if action == "accept":
            return f"{safe_role} accepts the latest offer."
        if action == "reject":
            return f"{safe_role} rejects the latest offer."
        if action in {"buy", "sell", "pass"}:
            return f"{safe_role} submitted a {action} action."
        return "The agent submitted an Arena action."
