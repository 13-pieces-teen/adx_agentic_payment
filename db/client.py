"""
ADX Supabase Client — Database-Backed Store Layer

Drop-in replacement for in-memory AgentRegistry, OrderBook, Leaderboard.
Each method mirrors the original interface so existing code works unchanged.

Usage:
    from db.client import ADXSupabase
    db = ADXSupabase(url="...", key="...")
    db.agents.register(agent)       # same API as AgentRegistry.register()
    db.orderbook.publish(intent)     # same API as OrderBook.publish()
    db.leaderboard.rank(...)        # same API as Leaderboard.rank()
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict
from typing import Optional

try:
    from supabase import create_client, Client as SupabaseClient
    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False
    SupabaseClient = None  # type: ignore
    def create_client(*args, **kwargs):
        raise ImportError("supabase-py not installed. Run: pip install supabase")


# ============================================================
# Main Client
# ============================================================


class ADXSupabase:
    """Unified Supabase client for ADX Agent Arena."""

    def __init__(self, url: str = "", key: str = ""):
        self.url = url or os.getenv("SUPABASE_URL", "")
        self.key = key or os.getenv("SUPABASE_ANON_KEY", "")
        self._client: Optional[SupabaseClient] = None

        # Sub-stores (lazy init after connect)
        self.agents = AgentDB(self)
        self.orderbook = OrderBookDB(self)
        self.leaderboard = LeaderboardDB(self)

    def connect(self) -> "SupabaseClient":
        if not HAS_SUPABASE:
            raise ImportError("supabase-py not installed. Run: pip install supabase")
        if self._client is None:
            if not self.url or not self.key:
                raise ValueError(
                    "SUPABASE_URL and SUPABASE_ANON_KEY required. "
                    "Set env vars or pass to ADXSupabase(url=..., key=...)"
                )
            self._client = create_client(self.url, self.key)
            self.agents._client = self._client
            self.orderbook._client = self._client
            self.leaderboard._client = self._client
        return self._client

    @property
    def client(self) -> SupabaseClient:
        return self.connect()


# ============================================================
# Agent DB — mirrors AgentRegistry interface
# ============================================================


class AgentDB:
    """Supabase-backed agent registry. Same API as AgentRegistry."""

    def __init__(self, db: ADXSupabase):
        self._db = db
        self._client: Optional[SupabaseClient] = None
        # Keep in-memory cache for fast access during negotiation
        self._cache: dict[str, dict] = {}

    @property
    def client(self) -> SupabaseClient:
        if self._client is None:
            self._client = self._db.connect()
        return self._client

    # ---- CRUD ----

    def register(self, agent) -> str:
        """Insert or update agent. `agent` can be AgentRegistration or dict."""
        data = agent.to_public_dict() if hasattr(agent, 'to_public_dict') else agent
        # Add internal fields not in public dict
        if hasattr(agent, 'llm'):
            data['api_key_hmac'] = agent.llm.api_key_hmac
            data['endpoint'] = agent.llm.endpoint
            data['max_tokens'] = agent.llm.max_tokens
            data['temperature'] = agent.llm.temperature

        row = {
            'id': data['agent_id'],
            'owner_id': data.get('owner_id', ''),
            'name': data.get('name', ''),
            'description': data.get('description', ''),
            'llm_provider': data.get('llm_provider', 'openai'),
            'llm_model': data.get('llm_model', 'gpt-4o'),
            'api_key_hmac': data.get('api_key_hmac', ''),
            'endpoint': data.get('endpoint', ''),
            'max_tokens': data.get('max_tokens', 4096),
            'temperature': data.get('temperature', 0.7),
            'negotiation_style': data.get('negotiation_style', 'balanced'),
            'strategy_description': data.get('strategy_description', ''),
            'tradable_assets': data.get('tradable_assets', []),
            'trade_direction': data.get('trade_direction', 'both'),
            'status': data.get('status', 'online'),
            'elo_rating': data.get('elo_rating', 1000.0),
            'battles_fought': data.get('battles_fought', 0),
            'battles_won': data.get('battles_won', 0),
            'total_earned': data.get('total_earned', 0.0),
            'total_saved': data.get('total_saved', 0.0),
        }

        self.client.table('agents').upsert(row, on_conflict='id').execute()
        self._cache[data['agent_id']] = row
        return data['agent_id']

    def unregister(self, agent_id: str) -> bool:
        self.client.table('agents').delete().eq('id', agent_id).execute()
        self._cache.pop(agent_id, None)
        return True

    def get(self, agent_id: str) -> Optional[dict]:
        if agent_id in self._cache:
            return self._cache[agent_id]
        result = self.client.table('agents').select('*').eq('id', agent_id).execute()
        if result.data:
            self._cache[agent_id] = result.data[0]
            return result.data[0]
        return None

    def list_by_owner(self, owner_id: str) -> list[dict]:
        result = self.client.table('agents').select('*').eq('owner_id', owner_id).execute()
        return result.data

    def find_by_asset(self, asset_class: str, direction: str = "both",
                      min_elo: float = 0) -> list[dict]:
        query = self.client.table('agents').select('*').neq('status', 'suspended')
        if min_elo > 0:
            query = query.gte('elo_rating', min_elo)
        # Filter by tradable_assets contains asset_class (JSONB)
        result = query.execute()
        agents = [
            a for a in result.data
            if asset_class in a.get('tradable_assets', [])
        ]
        if direction != 'both':
            agents = [
                a for a in agents
                if a.get('trade_direction') in (direction, 'both')
            ]
        agents.sort(key=lambda a: a.get('elo_rating', 0), reverse=True)
        return agents

    def set_status(self, agent_id: str, status: str) -> dict:
        result = self.client.table('agents').update({
            'status': status, 'last_seen': 'now()'
        }).eq('id', agent_id).execute()
        if result.data:
            self._cache[agent_id] = result.data[0]
        return result.data[0] if result.data else {}

    def heartbeat(self, agent_id: str):
        """Touches last_seen and sets online if offline."""
        self.client.rpc('touch_agent_last_seen', {'agent_id': agent_id}).execute()
        agent = self.get(agent_id)
        if agent and agent.get('status') == 'offline':
            self.set_status(agent_id, 'online')

    def update_elo(self, agent_id: str, new_elo: float, won: bool = False):
        """Update ELO and battle stats after a fight."""
        updates = {'elo_rating': new_elo, 'battles_fought': self.client.rpc(
            'increment_counter',
            {'table_name': 'agents', 'column': 'battles_fought', 'row_id': agent_id}
        )}
        if won:
            self.client.table('agents').update({
                'battles_won': self.client.rpc(
                    'increment_counter',
                    {'table_name': 'agents', 'column': 'battles_won', 'row_id': agent_id}
                )
            }).eq('id', agent_id).execute()

        self.client.table('agents').update({
            'elo_rating': new_elo,
            'battles_fought': self.get(agent_id).get('battles_fought', 0) + 1,
        }).eq('id', agent_id).execute()
        self._cache.pop(agent_id, None)

    @property
    def stats(self) -> dict:
        online = self.client.table('agents').select('id', count='exact').eq('status', 'online').execute()
        in_battle = self.client.table('agents').select('id', count='exact').eq('status', 'in_battle').execute()
        total = self.client.table('agents').select('id', count='exact').execute()
        return {
            'total_agents': total.count,
            'online': online.count,
            'in_battle': in_battle.count,
        }


# ============================================================
# OrderBook DB — mirrors OrderBook interface
# ============================================================


class OrderBookDB:
    """Supabase-backed order book. Same API as OrderBook."""

    def __init__(self, db: ADXSupabase):
        self._db = db
        self._client: Optional[SupabaseClient] = None

    @property
    def client(self) -> SupabaseClient:
        if self._client is None:
            self._client = self._db.connect()
        return self._client

    # ---- Listings ----

    def publish_listing(self, listing) -> str:
        """`listing` can be ResourceListing or dict."""
        data = listing.to_public_dict() if hasattr(listing, 'to_public_dict') else listing

        # Parse price_range from to_public_dict format
        price_range = data.get('price_range', {})
        row = {
            'id': data.get('listing_id', f"list_{uuid.uuid4().hex[:10]}"),
            'seller_agent_id': data.get('seller_agent_id', ''),
            'seller_name': data.get('seller_name', ''),
            'asset_class': data.get('asset_class', 'compute'),
            'title': data.get('title', ''),
            'description': data.get('description', ''),
            'quantity': data.get('quantity', 1),
            'unit': data.get('unit', 'hour'),
            'min_price': price_range.get('min', 0.0),
            'ideal_price': price_range.get('ideal', 0.0),
            'max_price': price_range.get('max', 0.0),
            'currency': price_range.get('currency', 'USDC'),
            'tags': data.get('tags', []),
        }
        self.client.table('listings').upsert(row, on_conflict='id').execute()
        return row['id']

    def get_listings(self, asset_class: str = "", available_only: bool = True) -> list[dict]:
        query = self.client.table('listings').select('*')
        if asset_class:
            query = query.eq('asset_class', asset_class)
        if available_only:
            query = query.eq('is_active', True)
        result = query.order('created_at', desc=True).execute()
        return result.data

    def remove_listing(self, listing_id: str) -> bool:
        self.client.table('listings').update({'is_active': False}).eq('id', listing_id).execute()
        return True

    # ---- Intents ----

    def publish(self, intent) -> str:
        """`intent` can be Intent or dict."""
        if hasattr(intent, 'intent_type'):
            data = {
                'id': intent.intent_id,
                'agent_id': intent.agent_id,
                'agent_name': intent.agent_name,
                'intent_type': intent.intent_type.value,
                'asset_class': intent.asset_class.value,
                'description': intent.description,
                'quantity': intent.quantity,
                'min_price': intent.price.min_acceptable,
                'ideal_price': intent.price.ideal,
                'max_price': intent.price.max_acceptable,
                'currency': intent.price.currency,
                'max_rounds': intent.rules.max_negotiation_rounds,
                'min_price_delta_pct': intent.rules.min_price_delta_pct,
                'auto_accept_threshold_pct': intent.rules.auto_accept_threshold_pct,
                'negotiation_style': intent.negotiation_style.value,
                'tags': intent.tags,
                'listing_id': intent.listing_id if hasattr(intent, 'listing_id') else '',
                'ttl_seconds': intent.ttl_seconds if hasattr(intent, 'ttl_seconds') else 3600,
            }
        else:
            data = intent
            data.setdefault('id', uuid.uuid4().hex)

        self.client.table('intents').upsert({
            'id': data.get('id', data.get('intent_id', uuid.uuid4().hex)),
            'agent_id': data.get('agent_id', ''),
            'agent_name': data.get('agent_name', ''),
            'intent_type': data.get('intent_type', 'buy'),
            'asset_class': data.get('asset_class', 'service'),
            'description': data.get('description', ''),
            'quantity': data.get('quantity', 1),
            'min_price': data.get('min_price', 0.0),
            'ideal_price': data.get('ideal_price', 0.0),
            'max_price': data.get('max_price', 0.0),
            'currency': data.get('currency', 'USDC'),
            'max_rounds': data.get('max_rounds', 5),
            'min_price_delta_pct': data.get('min_price_delta_pct', 1.0),
            'auto_accept_threshold_pct': data.get('auto_accept_threshold_pct', 5.0),
            'negotiation_style': data.get('negotiation_style', 'balanced'),
            'tags': data.get('tags', []),
            'listing_id': data.get('listing_id', ''),
            'ttl_seconds': data.get('ttl_seconds', 3600),
        }, on_conflict='id').execute()
        return data.get('id', data.get('intent_id', ''))

    def revoke(self, intent_id: str) -> bool:
        self.client.table('intents').update({'is_active': False}).eq('id', intent_id).execute()
        return True

    def get(self, intent_id: str) -> Optional[dict]:
        result = self.client.table('intents').select('*').eq('id', intent_id).execute()
        return result.data[0] if result.data else None

    def find_active_intents(self, intent_type: str = "", asset_class: str = "") -> list[dict]:
        """Find all active intents, optionally filtered."""
        query = self.client.table('intents').select('*').eq('is_active', True)
        if intent_type:
            query = query.eq('intent_type', intent_type)
        if asset_class:
            query = query.eq('asset_class', asset_class)
        return query.execute().data

    def cleanup_expired(self) -> int:
        """Mark expired intents inactive. Uses DB function."""
        self.client.rpc('expire_stale_intents').execute()
        # Count inactivated
        result = self.client.table('intents').select('id', count='exact').eq('is_active', False).execute()
        return result.count

    @property
    def stats(self) -> dict:
        buy = self.client.table('intents').select('id', count='exact').eq('intent_type', 'buy').eq('is_active', True).execute()
        sell = self.client.table('intents').select('id', count='exact').eq('intent_type', 'sell').eq('is_active', True).execute()
        listings = self.client.table('listings').select('id', count='exact').eq('is_active', True).execute()
        return {
            'buy_intents': buy.count,
            'sell_intents': sell.count,
            'listings': listings.count,
        }


# ============================================================
# Leaderboard DB — mirrors Leaderboard interface
# ============================================================


class LeaderboardDB:
    """Supabase-backed leaderboard. Same API as Leaderboard."""

    def __init__(self, db: ADXSupabase):
        self._db = db
        self._client: Optional[SupabaseClient] = None

    @property
    def client(self) -> SupabaseClient:
        if self._client is None:
            self._client = self._db.connect()
        return self._client

    def record_battle(self, battle) -> str:
        """Record a battle. `battle` can be BattleRecord or dict."""
        if hasattr(battle, 'to_public_dict'):
            data = battle.to_public_dict()
            # Add ELO/internal fields not in public dict
            data.update({
                'agent_a_elo_before': battle.agent_a_elo_before,
                'agent_a_elo_after': battle.agent_a_elo_after,
                'agent_a_elo_delta': battle.agent_a_elo_delta,
                'agent_b_elo_before': battle.agent_b_elo_before,
                'agent_b_elo_after': battle.agent_b_elo_after,
                'agent_b_elo_delta': battle.agent_b_elo_delta,
                'buyer_ideal': battle.buyer_ideal,
                'seller_ideal': battle.seller_ideal,
                'buyer_savings_pct': battle.buyer_savings_pct,
                'seller_premium_pct': battle.seller_premium_pct,
            })
        else:
            data = battle

        row = {
            'id': data.get('battle_id', f"battle_{uuid.uuid4().hex[:10]}"),
            'session_id': data.get('session_id', ''),
            'asset_class': data.get('asset_class', ''),
            'description': data.get('description', ''),
            'agent_a_id': data.get('agent_a_id', ''),
            'agent_b_id': data.get('agent_b_id', ''),
            'agent_a_name': data.get('agent_a_name', ''),
            'agent_b_name': data.get('agent_b_name', ''),
            'outcome': data.get('outcome', 'draw'),
            'winner_agent_id': data.get('winner_agent_id', ''),
            'final_price': data.get('final_price', 0.0),
            'currency': data.get('currency', 'USDC'),
            'quantity': data.get('quantity', 1),
            'total_value': data.get('total_value', 0.0),
            'rounds_taken': data.get('rounds_taken', 0),
            'duration_seconds': data.get('duration_seconds', 0.0),
            'buyer_ideal': data.get('buyer_ideal', 0.0),
            'seller_ideal': data.get('seller_ideal', 0.0),
            'buyer_savings_pct': data.get('buyer_savings_pct', 0.0),
            'seller_premium_pct': data.get('seller_premium_pct', 0.0),
            'agent_a_elo_before': data.get('agent_a_elo_before', 1000.0),
            'agent_a_elo_after': data.get('agent_a_elo_after', 1000.0),
            'agent_a_elo_delta': data.get('agent_a_elo_delta', 0.0),
            'agent_b_elo_before': data.get('agent_b_elo_before', 1000.0),
            'agent_b_elo_after': data.get('agent_b_elo_after', 1000.0),
            'agent_b_elo_delta': data.get('agent_b_elo_delta', 0.0),
            'started_at': data.get('started_at', time.strftime('%Y-%m-%dT%H:%M:%SZ')),
            'ended_at': data.get('ended_at', time.strftime('%Y-%m-%dT%H:%M:%SZ')),
        }
        self.client.table('battles').insert(row).execute()
        return row['id']

    def rank(self, asset_class: str = "", min_battles: int = 0, limit: int = 50) -> list[dict]:
        """Use the DB function for leaderboard ranking."""
        result = self.client.rpc('get_leaderboard', {
            'p_asset_class': asset_class,
            'p_min_battles': min_battles,
            'p_limit': limit,
        }).execute()
        return result.data

    def battle_feed(self, limit: int = 20) -> list[dict]:
        result = self.client.table('battles').select('*').order('ended_at', desc=True).limit(limit).execute()
        return result.data

    def agent_history(self, agent_id: str, limit: int = 20) -> list[dict]:
        result = self.client.table('battles').select('*').or_(
            f'agent_a_id.eq.{agent_id},agent_b_id.eq.{agent_id}'
        ).order('ended_at', desc=True).limit(limit).execute()
        return result.data

    def rising_stars(self, days: int = 7, top_n: int = 10) -> list[dict]:
        """Agents with biggest ELO gain in recent days."""
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - days * 86400))
        result = self.client.rpc('get_rising_stars', {
            'p_since': since, 'p_limit': top_n,
        }).execute() if hasattr(self.client, 'rpc') else self.client.table('battles').select('*').gte('ended_at', since).execute()
        # Simplified: aggregate ELO deltas client-side
        battles = result.data if result.data else []
        elo_gains: dict[str, float] = {}
        for b in battles:
            elo_gains[b['agent_a_id']] = elo_gains.get(b['agent_a_id'], 0) + b.get('agent_a_elo_delta', 0)
            elo_gains[b['agent_b_id']] = elo_gains.get(b['agent_b_id'], 0) + b.get('agent_b_elo_delta', 0)
        rising = sorted(elo_gains.items(), key=lambda x: x[1], reverse=True)[:top_n]
        return [{'agent_id': aid, 'elo_gain': round(gain, 1)} for aid, gain in rising]

    @property
    def stats(self) -> dict:
        total = self.client.table('battles').select('id', count='exact').execute()
        if total.count == 0:
            return {'total_battles': 0}
        draws = self.client.table('battles').select('id', count='exact').eq('outcome', 'draw').execute()
        return {
            'total_battles': total.count,
            'draw_rate_pct': round(draws.count / total.count * 100, 1) if total.count else 0,
        }


# ============================================================
# Quick Start
# ============================================================

def init_db(url: str = "", key: str = "") -> ADXSupabase:
    """Create and connect the Supabase client. Read env vars if args empty."""
    db = ADXSupabase(url=url, key=key)
    db.connect()
    return db
