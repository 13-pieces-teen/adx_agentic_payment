"""Versioned event presets derived from WORLD_AND_EVENTS(1).md."""

from __future__ import annotations

from typing import Final

from .events import EffectKind, EventEffect, WorldEvent
from .money import gold


MVP_EVENT_CATALOG: Final[dict[str, WorldEvent]] = {
    "palace-requisition": WorldEvent(
        event_id="palace-requisition",
        display_name="王宫征召",
        narrative=(
            "北境战事再起。王宫奉皇帝之命，限量高价征收精铁，售罄即止。"
        ),
        reveal_round=1,
        duration_rounds=1,
        effects=(
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="market",
                basis_points=10_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="final",
                basis_points=1_000,
            ),
            EventEffect(
                kind=EffectKind.CREATE_ROYAL_ORDER,
                good="iron",
                target="market",
                order_price_atomic=gold("15"),
                order_limit=200,
            ),
        ),
    ),
    "new-iron-mine": WorldEvent(
        event_id="new-iron-mine",
        display_name="新矿开采",
        narrative="王城外发现新铁矿，精铁流通骤增，旧库存价格承压。",
        reveal_round=2,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.SUPPLY_INDEX_ADD_BPS,
                good="iron",
                target="market",
                basis_points=3_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="market",
                basis_points=-2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="final",
                basis_points=-1_000,
            ),
        ),
    ),
    "granary-fire": WorldEvent(
        event_id="granary-fire",
        display_name="粮仓失火",
        narrative="王城粮仓化为灰烬，粮草流通收缩，饥饿推高了每袋粮的价格。",
        reveal_round=3,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.SUPPLY_INDEX_ADD_BPS,
                good="grain",
                target="market",
                basis_points=-2_500,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="grain",
                target="market",
                basis_points=3_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="grain",
                target="final",
                basis_points=1_500,
            ),
        ),
    ),
    "noble-gem-fever": WorldEvent(
        event_id="noble-gem-fever",
        display_name="贵族狂热",
        narrative="传言新王将以宝石铺满王座，贵族争相囤积，泡沫开始膨胀。",
        reveal_round=2,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.BUBBLE_ADD_BPS,
                good="gems",
                target="market",
                basis_points=2_500,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="both",
                basis_points=2_500,
            ),
        ),
    ),
    "coronation-cancelled": WorldEvent(
        event_id="coronation-cancelled",
        display_name="加冕取消",
        narrative="新王死于宫变。宝石王座无人问津，泡沫顷刻回归基本面。",
        reveal_round=5,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.BUBBLE_CLEAR,
                good="gems",
                target="market",
            ),
            EventEffect(
                kind=EffectKind.PRICE_RESET_TO_BASE,
                good="gems",
                target="both",
            ),
        ),
    ),
    "barbarian-siege": WorldEvent(
        event_id="barbarian-siege",
        display_name="蛮族围城",
        narrative="补给线被切断，粮草成为活命之物，宝石却再也换不来一块面包。",
        reveal_round=4,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="grain",
                target="market",
                basis_points=5_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="grain",
                target="final",
                basis_points=3_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="market",
                basis_points=-4_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="final",
                basis_points=-2_500,
            ),
        ),
    ),
    "peace-rumor": WorldEvent(
        event_id="peace-rumor",
        display_name="议和传闻",
        narrative="三位王子或将停战。军需价格回落，贵族重新谈起奢侈和体面。",
        reveal_round=4,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="market",
                basis_points=-2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="iron",
                target="final",
                basis_points=-1_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="warhorse",
                target="market",
                basis_points=-2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="warhorse",
                target="final",
                basis_points=-1_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="market",
                basis_points=2_500,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="final",
                basis_points=1_000,
            ),
        ),
    ),
    "merchant-caravan": WorldEvent(
        event_id="merchant-caravan",
        display_name="商队入城",
        narrative="南方商队穿过封锁抵达王城，粮草与宝石的短期供给同时增加。",
        reveal_round=1,
        duration_rounds=1,
        effects=(
            EventEffect(
                kind=EffectKind.SUPPLY_INDEX_ADD_BPS,
                good="grain",
                target="market",
                basis_points=2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="grain",
                target="market",
                basis_points=-1_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="market",
                basis_points=-1_000,
            ),
        ),
    ),
    "royal-wedding": WorldEvent(
        event_id="royal-wedding",
        display_name="王室婚礼",
        narrative="王室突然宣布联姻，贵族争购宝石与战马以赶赴盛典。",
        reveal_round=1,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="gems",
                target="both",
                basis_points=2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="warhorse",
                target="both",
                basis_points=1_500,
            ),
        ),
    ),
    "stable-plague": WorldEvent(
        event_id="stable-plague",
        display_name="马厩疫病",
        narrative="王城马厩爆发疫病，战马交易骤冷，幸存马匹的终场估值承压。",
        reveal_round=1,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.SUPPLY_INDEX_ADD_BPS,
                good="warhorse",
                target="market",
                basis_points=-2_000,
            ),
            EventEffect(
                kind=EffectKind.PRICE_MULTIPLY_BPS,
                good="warhorse",
                target="both",
                basis_points=-2_000,
            ),
        ),
    ),
}


DEMO_EVENT_IDS: Final[tuple[str, ...]] = (
    "palace-requisition",
    "noble-gem-fever",
    "granary-fire",
    "peace-rumor",
    "coronation-cancelled",
)


def demo_events() -> tuple[WorldEvent, ...]:
    return tuple(MVP_EVENT_CATALOG[event_id] for event_id in DEMO_EVENT_IDS)


__all__ = [
    "DEMO_EVENT_IDS",
    "MVP_EVENT_CATALOG",
    "demo_events",
]
