"""
A2A AgentCard Extensions — ADX Agent Arena

Defines how agents declare their trading capabilities using standard A2A AgentCard
+ ADX extension metadata for BYOAgent discovery.
"""

from __future__ import annotations
from typing import Optional

INTENT_EXTENSION_URI = "https://adx.agentic.payment/intent/v1"


def build_buy_skill(asset_class: str, description: str, max_price: float,
                    currency: str = "INJ", tags: list[str] | None = None) -> dict:
    return {
        "id": f"buy-{asset_class}",
        "name": f"Buy {asset_class}",
        "description": description,
        "tags": ["intent:buy", f"asset:{asset_class}", f"currency:{currency}"] + (tags or []),
        "examples": [f"I want to buy {asset_class}", f"Looking for {asset_class} under {max_price} {currency}"],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    }


def build_sell_skill(asset_class: str, description: str, min_price: float,
                     currency: str = "INJ", tags: list[str] | None = None) -> dict:
    return {
        "id": f"sell-{asset_class}",
        "name": f"Sell {asset_class}",
        "description": description,
        "tags": ["intent:sell", f"asset:{asset_class}", f"currency:{currency}"] + (tags or []),
        "examples": [f"I'm selling {asset_class}", f"{asset_class} available starting at {min_price} {currency}"],
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    }


def build_intent_extension_metadata(intent_dict: dict) -> dict:
    return {
        INTENT_EXTENSION_URI: {
            "version": "2.0",
            "intent_type": intent_dict["intent_type"],
            "asset_class": intent_dict["asset_class"],
            "description": intent_dict["description"],
            "quantity": intent_dict["quantity"],
            "price_constraint": {
                "currency": intent_dict.get("currency", "INJ"),
                "min": intent_dict.get("min_price", 0),
                "ideal": intent_dict.get("ideal_price", 0),
                "max": intent_dict.get("max_price", float("inf")),
            },
            "negotiation_style": intent_dict.get("negotiation_style", "balanced"),
            "tags": intent_dict.get("tags", []),
        }
    }


def parse_intent_from_agent_card(agent_card: dict) -> Optional[dict]:
    capabilities = agent_card.get("capabilities", {})
    extensions = capabilities.get("extensions", [])
    for ext in extensions:
        if ext.get("uri") == INTENT_EXTENSION_URI:
            params = ext.get("params", {})
            adx_data = params.get(INTENT_EXTENSION_URI, params)
            if adx_data:
                return adx_data

    metadata = agent_card.get("metadata", {})
    if INTENT_EXTENSION_URI in metadata:
        return metadata[INTENT_EXTENSION_URI]

    skills = agent_card.get("skills", [])
    buy_skills = [s for s in skills if any(t.startswith("intent:buy") for t in s.get("tags", []))]
    sell_skills = [s for s in skills if any(t.startswith("intent:sell") for t in s.get("tags", []))]
    if buy_skills or sell_skills:
        primary = (buy_skills or sell_skills)[0]
        tags = primary.get("tags", [])
        asset_class = "service"
        for tag in tags:
            if tag.startswith("asset:"):
                asset_class = tag.split(":", 1)[1]
        return {
            "intent_type": "buy" if buy_skills else "sell",
            "asset_class": asset_class,
            "description": primary.get("description", ""),
            "tags": tags,
        }
    return None


async def discover_agent_intents(agent_url: str, httpx_client=None) -> list[dict]:
    import httpx
    well_known = f"{agent_url}/.well-known/agent-card.json"
    async def _fetch():
        if httpx_client:
            return await httpx_client.get(well_known)
        async with httpx.AsyncClient(timeout=10.0) as c:
            return await c.get(well_known)
    try:
        resp = await _fetch()
        resp.raise_for_status()
        intent = parse_intent_from_agent_card(resp.json())
        return [intent] if intent else []
    except Exception:
        return []
