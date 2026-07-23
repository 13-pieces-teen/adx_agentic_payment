import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || '';
const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';

export const supabase = createClient(supabaseUrl, supabaseKey);

// Types from our DB schema
export interface Agent {
  id: string;
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

// Hooks
export async function getAgents(): Promise<Agent[]> {
  const { data } = await supabase.from('agents').select('*');
  return (data || []) as Agent[];
}

export async function getAgent(id: string): Promise<Agent | null> {
  const { data } = await supabase.from('agents').select('*').eq('id', id).single();
  return data as Agent | null;
}

export async function getListings(assetClass?: string): Promise<Listing[]> {
  let q = supabase.from('listings').select('*').eq('is_active', true);
  if (assetClass) q = q.eq('asset_class', assetClass);
  const { data } = await q.order('created_at', { ascending: false });
  return (data || []) as Listing[];
}

export async function getLeaderboard(
  assetClass = '',
  minBattles = 0,
  limit = 50
): Promise<LeaderboardEntry[]> {
  const { data } = await supabase.rpc('get_leaderboard', {
    p_asset_class: assetClass,
    p_min_battles: minBattles,
    p_limit: limit,
  });
  return (data || []) as LeaderboardEntry[];
}

export async function getBattleFeed(limit = 20): Promise<Battle[]> {
  const { data } = await supabase
    .from('battles')
    .select('*')
    .order('ended_at', { ascending: false })
    .limit(limit);
  return (data || []) as Battle[];
}

export function subscribeBattles(callback: (battle: Battle) => void) {
  return supabase
    .channel('battles-feed')
    .on(
      'postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'battles' },
      (payload) => callback(payload.new as Battle)
    )
    .subscribe();
}

export function subscribeListings(callback: (listing: Listing) => void) {
  return supabase
    .channel('listings-feed')
    .on(
      'postgres_changes',
      { event: 'INSERT', schema: 'public', table: 'listings' },
      (payload) => callback(payload.new as Listing)
    )
    .subscribe();
}
