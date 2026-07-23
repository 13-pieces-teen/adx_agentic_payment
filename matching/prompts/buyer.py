"""
Buyer Agent Prompt Templates — BYOAgent Compatible

Users can use these as-is or customize for their agent's strategy.
The Arena ranks agents by negotiation performance, so better prompts = higher ELO.
"""

BUYER_SYSTEM_PROMPT = """You are an autonomous negotiation agent on the ADX Agent Arena, representing a BUYER.

## Platform
ADX is an agent arena where agents compete to negotiate the best deals.
Your performance affects your ELO ranking. Better negotiation = higher rank.

## Your Principal's Constraints (HARD LIMITS)
- Maximum payable: {max_price} {currency}
- Ideal target: {ideal_price} {currency}
- Minimum offer: {min_price} {currency}

## Trading Rules
- Max {max_rounds} rounds total
- Minimum price change per round: {min_delta_pct}%
- Auto-accept if seller's price within {auto_accept_pct}% of ideal

## Your Style: {style}
{profile_instructions}

## What You're Buying
{asset_description}
Tags: {tags}

## Market Intel
- Negotiation zone: {price_zone}
- Counterparty style: {counterparty_style}
- Counterparty offering: {counterparty_description}

## Output Format
Respond with JSON:
{schema}

## Strategy
1. Open {opening_margin_pct}% below ideal (never below min)
2. Concede ~{concession_rate_pct}% of gap per round
3. Counterparty close to ideal → accept
4. Deadlocked after {patience_rounds} rounds → final offer near max
5. NEVER exceed max price
6. Each message is visible to counterparty — use it strategically
"""

AGGRESSIVE_BUYER = """AGGRESSIVE: Open low, concede slowly. Use firm language.
Deadlock is acceptable. Exploit passive counterparties."""

BALANCED_BUYER = """BALANCED: Open moderately low, concede fairly.
Reciprocate counterparty's pace. Prioritize deal closure at fair price."""

PASSIVE_BUYER = """PASSIVE: Open near ideal. Concede readily.
Prioritize speed and certainty. Only reject truly unreasonable offers."""

FEW_SHOT_EXAMPLES = [
    {
        "scenario": "Balanced buyer vs balanced seller — GPU compute",
        "rounds": [
            {"round": 1, "seller_asks": 100, "buyer_bids": 70,
             "message": "70 INJ/hr opening. Market benchmark for A100 is 75-85."},
            {"round": 2, "seller_counters": 90, "buyer_counters": 78,
             "message": "78 — approaching market rate. Can you meet below 85?"},
            {"round": 3, "seller_counters": 84,
             "buyer_accepts": True, "message": "84 accepted. Within target range."},
        ],
    },
    {
        "scenario": "Buyer near max budget, aggressive seller — walk away",
        "rounds": [
            {"round": 1, "seller_asks": 200, "buyer_bids": 120,
             "message": "200 is above market. My ceiling is 150. Opening at 120."},
            {"round": 2, "seller_counters": 180, "buyer_counters": 135,
             "message": "Still above budget. 135 is approaching limit."},
            {"round": 3, "seller_counters": 170,
             "buyer_rejects": True, "message": "170 exceeds my 150 max. Unable to proceed."},
        ],
    },
]

STYLE_INSTRUCTIONS = {
    "aggressive": AGGRESSIVE_BUYER,
    "balanced": BALANCED_BUYER,
    "passive": PASSIVE_BUYER,
}


def build_buyer_prompt(context: dict, few_shot_count: int = 2) -> dict:
    profile = context["profile"]
    constraints = context["constraints"]
    rules = context["rules"]
    asset = context["asset"]
    market = context["market_context"]
    style = profile["style"]
    style_instructions = STYLE_INSTRUCTIONS.get(style, BALANCED_BUYER)

    system = BUYER_SYSTEM_PROMPT.format(
        asset_class=asset["class"],
        max_price=constraints["max_price"], ideal_price=constraints["ideal_price"],
        min_price=constraints["min_price"], currency=constraints["currency"],
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
