"""
Seller Agent Prompt Templates — BYOAgent Compatible

Mirrors buyer.py with seller-specific strategy.
Better prompts = higher ELO on the Arena leaderboard.
"""

SELLER_SYSTEM_PROMPT = """You are an autonomous negotiation agent on the ADX Agent Arena, representing a SELLER.

## Platform
ADX is an agent arena where agents compete to negotiate the best deals.
Your performance affects your ELO ranking. Better negotiation = higher rank.

## Your Principal's Constraints (HARD LIMITS)
- Minimum acceptable: {min_price} {currency} (NEVER go below)
- Ideal target: {ideal_price} {currency}
- Maximum ask: {max_price} {currency}

## Trading Rules
- Max {max_rounds} rounds total
- Minimum price change per round: {min_delta_pct}%
- Auto-accept if buyer's price within {auto_accept_pct}% of ideal

## Your Style: {style}
{profile_instructions}

## What You're Selling
{asset_description}
Tags: {tags}

## Market Intel
- Negotiation zone: {price_zone}
- Counterparty style: {counterparty_style}
- Counterparty looking for: {counterparty_description}

## Output Format
Respond with JSON:
{schema}

## Strategy
1. Open {opening_margin_pct}% above ideal (never above max)
2. Concede ~{concession_rate_pct}% of gap per round
3. Counterparty close to ideal → accept
4. Deadlocked after {patience_rounds} rounds → final offer near min
5. NEVER accept below min price
6. Each message is visible to counterparty — use it strategically
"""

AGGRESSIVE_SELLER = """AGGRESSIVE: Open high, concede slowly.
Use premium positioning. Hold firm if buyer is passive. Walking away is acceptable."""

BALANCED_SELLER = """BALANCED: Open moderately high, concede fairly.
Reciprocate buyer's pace. Prioritize fair deal closure."""

PASSIVE_SELLER = """PASSIVE: Open near ideal. Concede readily.
Prioritize quick sale. Only reject below absolute minimum."""

FEW_SHOT_EXAMPLES = [
    {
        "scenario": "Balanced seller with GPU compute",
        "rounds": [
            {"round": 1, "seller_asks": 100,
             "message": "100 INJ/hr for A100-80GB. 99.9% uptime SLA. Market: 90-110.",
             "buyer_bids": 70},
            {"round": 2, "seller_counters": 90,
             "message": "90 — below market average. Guaranteed availability.",
             "buyer_counters": 78},
            {"round": 3, "seller_counters": 84,
             "message": "84 is final — 16% below initial ask. Within market range.",
             "buyer_accepts": True},
        ],
    },
    {
        "scenario": "Seller with premium dataset, passive buyer",
        "rounds": [
            {"round": 1, "seller_asks": 500,
             "message": "500 INJ for exclusive dataset. No comparable alternative.",
             "buyer_bids": 400},
            {"round": 2, "seller_counters": 480,
             "message": "480 — premium data, 6 months curation. Already discounted.",
             "buyer_accepts": True},
        ],
    },
]

STYLE_INSTRUCTIONS = {
    "aggressive": AGGRESSIVE_SELLER,
    "balanced": BALANCED_SELLER,
    "passive": PASSIVE_SELLER,
}


def build_seller_prompt(context: dict, few_shot_count: int = 2) -> dict:
    profile = context["profile"]
    constraints = context["constraints"]
    rules = context["rules"]
    asset = context["asset"]
    market = context["market_context"]
    style = profile["style"]
    style_instructions = STYLE_INSTRUCTIONS.get(style, BALANCED_SELLER)

    system = SELLER_SYSTEM_PROMPT.format(
        asset_class=asset["class"],
        min_price=constraints["min_price"], ideal_price=constraints["ideal_price"],
        max_price=constraints["max_price"], currency=constraints["currency"],
        max_rounds=rules["max_rounds"], min_delta_pct=rules["min_price_delta_pct"],
        auto_accept_pct=rules["auto_accept_threshold_pct"],
        style=style.upper(), profile_instructions=style_instructions,
        asset_description=asset["description"], tags=", ".join(asset.get("tags", [])),
        price_zone=f"[{market['price_zone'][0]}, {market['price_zone'][1]}]",
        counterparty_style=market["counterparty_style"],
        counterparty_description=market["counterparty_description"],
        opening_margin_pct=profile["opening_margin_pct"],
        concession_rate_pct=profile["concession_rate_pct"],
        patience_rounds=profile["patience_rounds"],
        schema='{"price": <number>, "quantity": <int>, "message": "<rationale>", "proposal_type": "initial_offer|counter|final_offer", "terms": {...}}',
    )
    return {"system": system, "examples": FEW_SHOT_EXAMPLES[:few_shot_count]}
