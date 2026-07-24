export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_URL || '').replace(/\/$/, '');

interface ListResponse<T> {
  total?: number;
  agents?: T[];
  listings?: T[];
  leaderboard?: T[];
  battles?: T[];
}

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new Error(`ADX API returned HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

// Types from our DB schema
export interface Agent {
  id: string;
  agent_id?: string;
  owner_id: string;
  name: string;
  description: string;
  llm_provider: string;
  llm_model: string;
  negotiation_style: 'aggressive' | 'balanced' | 'passive';
  tradable_assets: string[];
  trade_direction: 'buy' | 'sell' | 'both';
  status: 'online' | 'offline' | 'in_battle' | 'suspended';
  elo_rating: number;
  battles_fought: number;
  battles_won: number;
  total_earned: number;
  total_saved: number;
}

export interface Listing {
  id: string;
  listing_id?: string;
  seller_agent_id: string;
  seller_name: string;
  asset_class: string;
  title: string;
  description: string;
  quantity: number;
  unit: string;
  min_price: number;
  ideal_price: number;
  max_price: number;
  currency: string;
  tags: string[];
  created_at: string;
  is_active: boolean;
}

export interface Battle {
  id: string;
  battle_id?: string;
  asset_class: string;
  description: string;
  agent_a_id: string;
  agent_b_id: string;
  agent_a_name: string;
  agent_b_name: string;
  outcome: string;
  winner_agent_id: string;
  final_price: number;
  currency: string;
  quantity: number;
  total_value: number;
  rounds_taken: number;
  duration_seconds: number;
  agent_a_elo_before: number;
  agent_a_elo_after: number;
  agent_a_elo_delta: number;
  agent_b_elo_before: number;
  agent_b_elo_after: number;
  agent_b_elo_delta: number;
  ended_at: string;
}

export interface LeaderboardEntry {
  rank: number;
  agent_id: string;
  agent_name: string;
  owner_id: string;
  elo: number;
  tier: 'bronze' | 'silver' | 'gold' | 'diamond' | 'master';
  battles: number;
  wins: number;
  win_rate: number;
  earned: number;
  saved: number;
}

type ListingWire = Omit<Listing, 'created_at'> & {
  price_range?: {
    min?: number;
    ideal?: number;
    max?: number;
    currency?: string;
  };
  created_at: string | number;
};

function timestamp(value: string | number): string {
  if (typeof value === 'number') {
    return new Date(value * 1000).toISOString();
  }
  return value;
}

function normalizeAgent(agent: Agent): Agent {
  return {
    ...agent,
    id: agent.id || agent.agent_id || '',
  };
}

function normalizeListing(listing: ListingWire): Listing {
  return {
    ...listing,
    id: listing.id || listing.listing_id || '',
    min_price: listing.min_price ?? listing.price_range?.min ?? 0,
    ideal_price: listing.ideal_price ?? listing.price_range?.ideal ?? 0,
    max_price: listing.max_price ?? listing.price_range?.max ?? 0,
    currency: listing.currency || listing.price_range?.currency || '',
    created_at: timestamp(listing.created_at),
    is_active: listing.is_active ?? true,
  };
}

function normalizeBattle(battle: Battle): Battle {
  return {
    ...battle,
    id: battle.id || battle.battle_id || '',
    ended_at: timestamp(battle.ended_at),
  };
}

// Hooks
export async function getAgents(): Promise<Agent[]> {
  const response = await apiGet<ListResponse<Agent>>('/api/agents');
  return (response.agents || []).map(normalizeAgent);
}

export async function getAgent(id: string): Promise<Agent | null> {
  const response = await fetch(`${API_BASE_URL}/api/agents/${encodeURIComponent(id)}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`ADX API returned HTTP ${response.status}`);
  return normalizeAgent((await response.json()) as Agent);
}

export async function getListings(assetClass?: string): Promise<Listing[]> {
  const response = await apiGet<ListResponse<ListingWire>>(
    `/api/listings${query({ asset_class: assetClass, available_only: 'true' })}`,
  );
  return (response.listings || []).map(normalizeListing);
}

export async function getLeaderboard(
  assetClass = '',
  minBattles = 0,
  limit = 50
): Promise<LeaderboardEntry[]> {
  const response = await apiGet<ListResponse<LeaderboardEntry>>(
    `/api/arena/leaderboard${query({
      asset_class: assetClass,
      min_battles: minBattles,
      limit,
    })}`,
  );
  return response.leaderboard || [];
}

export async function getBattleFeed(limit = 20): Promise<Battle[]> {
  const response = await apiGet<ListResponse<Battle>>(
    `/api/arena/battles${query({ limit })}`,
  );
  return (response.battles || []).map(normalizeBattle);
}

export function subscribeBattles(callback: (battle: Battle) => void) {
  const seen = new Set<string>();
  let stopped = false;
  const poll = async () => {
    try {
      const battles = await getBattleFeed(20);
      battles
        .slice()
        .reverse()
        .forEach((battle) => {
          if (!seen.has(battle.id)) {
            seen.add(battle.id);
            callback(battle);
          }
        });
    } catch {
      // The next poll retries; transient API errors should not break the page.
    }
  };
  void poll();
  const timer = window.setInterval(() => void poll(), 5000);
  return {
    unsubscribe() {
      stopped = true;
      window.clearInterval(timer);
    },
    get closed() {
      return stopped;
    },
  };
}

export function subscribeListings(callback: (listing: Listing) => void) {
  const seen = new Set<string>();
  const poll = async () => {
    try {
      const listings = await getListings();
      listings
        .slice()
        .reverse()
        .forEach((listing) => {
          if (!seen.has(listing.id)) {
            seen.add(listing.id);
            callback(listing);
          }
        });
    } catch {
      // The next poll retries; transient API errors should not break the page.
    }
  };
  void poll();
  const timer = window.setInterval(() => void poll(), 5000);
  return { unsubscribe: () => window.clearInterval(timer) };
}
