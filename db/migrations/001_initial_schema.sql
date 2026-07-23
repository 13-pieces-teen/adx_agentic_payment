-- ============================================================
-- ADX Agent Arena — Initial Schema
-- Supabase (PostgreSQL 15+)
-- ============================================================

-- Enable UUID generation
create extension if not exists "uuid-ossp";

-- ============================================================
-- 1. AGENTS — BYOAgent registry
-- ============================================================

create table agents (
    id              text primary key,                  -- agent_id from AgentRegistration
    owner_id        text not null,                     -- user who owns this agent
    name            text not null,                     -- human-readable name
    description     text default '',
    llm_provider    text not null default 'openai',    -- openai | anthropic | deepseek | local | custom
    llm_model       text not null default 'gpt-4o',
    api_key_hmac    text not null default '',          -- HMAC of the user's API key (NEVER plaintext)
    endpoint        text default '',                   -- custom endpoint URL
    max_tokens      integer not null default 4096,
    temperature     real not null default 0.7,
    negotiation_style   text not null default 'balanced',  -- aggressive | balanced | passive
    strategy_description text default '',
    tradable_assets     jsonb not null default '[]'::jsonb,  -- ["compute", "data", ...]
    trade_direction     text not null default 'both',         -- buy | sell | both
    status              text not null default 'offline',      -- online | offline | in_battle | suspended
    elo_rating          real not null default 1000.0,
    battles_fought      integer not null default 0,
    battles_won         integer not null default 0,
    total_earned        real not null default 0.0,
    total_saved         real not null default 0.0,
    registered_at       timestamptz not null default now(),
    last_seen           timestamptz not null default now()
);

-- Indexes
create index idx_agents_owner on agents(owner_id);
create index idx_agents_status on agents(status);
create index idx_agents_elo on agents(elo_rating desc);
create index idx_agents_tradable on agents using gin(tradable_assets);
create index idx_agents_style on agents(negotiation_style);

-- ============================================================
-- 2. LISTINGS — Resource listings (sellers post these)
-- ============================================================

create table listings (
    id                  text primary key,                  -- listing_id
    seller_agent_id     text not null references agents(id),
    seller_name         text not null default '',
    asset_class         text not null,                     -- compute | storage | data | service | token | bandwidth
    title               text not null,
    description         text default '',
    quantity            integer not null default 1,
    unit                text not null default 'hour',       -- hour | GB | request | etc.
    min_price           real not null default 0.0,
    ideal_price         real not null default 0.0,
    max_price           real not null default 0.0,
    currency            text not null default 'USDC',       -- per STANDARDS.md, USDC for settlement
    available_until     timestamptz default null,           -- null = no expiry
    tags                jsonb not null default '[]'::jsonb,
    created_at          timestamptz not null default now(),
    is_active           boolean not null default true
);

create index idx_listings_asset on listings(asset_class);
create index idx_listings_seller on listings(seller_agent_id);
create index idx_listings_active on listings(is_active) where is_active = true;
create index idx_listings_tags on listings using gin(tags);
create index idx_listings_price on listings(ideal_price);

-- ============================================================
-- 3. INTENTS — Buy/sell intents (linked to agents)
-- ============================================================

create table intents (
    id                      text primary key,              -- intent_id
    agent_id                text not null references agents(id),
    agent_name              text not null default '',
    intent_type             text not null,                 -- buy | sell
    asset_class             text not null,
    description             text default '',
    quantity                integer not null default 1,
    min_price               real not null default 0.0,
    ideal_price             real not null default 0.0,
    max_price               real not null default 0.0,
    currency                text not null default 'USDC',
    max_rounds              integer not null default 5,
    min_price_delta_pct     real not null default 1.0,
    auto_accept_threshold_pct real not null default 5.0,
    negotiation_style       text not null default 'balanced',
    tags                    jsonb not null default '[]'::jsonb,
    listing_id              text default null,              -- links to ResourceListing (for sell)
    created_at              timestamptz not null default now(),
    ttl_seconds             integer not null default 3600,
    is_active               boolean not null default true,

    -- Price zone cache (computed by matching engine, stored for query efficiency)
    matched_price_low       real default null,
    matched_price_high      real default null
);

create index idx_intents_agent on intents(agent_id);
create index idx_intents_type_asset on intents(intent_type, asset_class);
create index idx_intents_active on intents(is_active) where is_active = true;
create index idx_intents_listing on intents(listing_id);

-- ============================================================
-- 4. NEGOTIATION SESSIONS — Active and completed negotiations
-- ============================================================

create table negotiation_sessions (
    id              text primary key,                      -- session_id
    buyer_intent_id text not null references intents(id),
    seller_intent_id text not null references intents(id),
    buyer_agent_id  text not null references agents(id),
    seller_agent_id text not null references agents(id),
    state           text not null default 'idle',           -- idle | offer_sent | counter_offer | evaluating | accepted | rejected | timeout
    current_round   integer not null default 0,
    max_rounds      integer not null default 5,
    price_zone_low  real not null default 0.0,
    price_zone_high real not null default 0.0,
    proposals       jsonb not null default '[]'::jsonb,     -- array of Proposal objects
    final_price     real default null,
    winner_agent_id text default null,
    started_at      timestamptz not null default now(),
    ended_at        timestamptz default null
);

create index idx_sessions_buyer on negotiation_sessions(buyer_agent_id);
create index idx_sessions_seller on negotiation_sessions(seller_agent_id);
create index idx_sessions_state on negotiation_sessions(state);

-- ============================================================
-- 5. BATTLES — Arena battle records (immutable history)
-- ============================================================

create table battles (
    id                  text primary key,                  -- battle_id
    session_id          text not null references negotiation_sessions(id),
    asset_class         text not null,
    description         text default '',
    agent_a_id          text not null references agents(id),   -- buyer
    agent_b_id          text not null references agents(id),   -- seller
    agent_a_name        text not null default '',
    agent_b_name        text not null default '',
    outcome             text not null,                     -- buyer_win | seller_win | draw | buyer_surrender | seller_surrender | timeout
    winner_agent_id     text default '',
    final_price         real not null default 0.0,
    currency            text not null default 'USDC',
    quantity            integer not null default 1,
    total_value         real not null default 0.0,
    rounds_taken        integer not null default 0,
    duration_seconds    real not null default 0.0,
    buyer_ideal         real not null default 0.0,
    seller_ideal        real not null default 0.0,
    buyer_savings_pct   real not null default 0.0,
    seller_premium_pct  real not null default 0.0,
    agent_a_elo_before  real not null default 1000.0,
    agent_a_elo_after   real not null default 1000.0,
    agent_a_elo_delta   real not null default 0.0,
    agent_b_elo_before  real not null default 1000.0,
    agent_b_elo_after   real not null default 1000.0,
    agent_b_elo_delta   real not null default 0.0,
    started_at          timestamptz not null default now(),
    ended_at            timestamptz not null default now(),

    -- Settlement reference (populated after on-chain tx confirms)
    tx_hash             text default '',                   -- Injective transaction hash
    settlement_status   text default 'pending'             -- pending | confirmed | failed | disputed
);

create index idx_battles_session on battles(session_id);
create index idx_battles_agent_a on battles(agent_a_id);
create index idx_battles_agent_b on battles(agent_b_id);
create index idx_battles_outcome on battles(outcome);
create index idx_battles_asset on battles(asset_class);
create index idx_battles_ended on battles(ended_at desc);
create index idx_battles_value on battles(total_value desc);

-- ============================================================
-- 6. ROW LEVEL SECURITY (RLS)
-- ============================================================

-- Enable RLS on all tables
alter table agents enable row level security;
alter table listings enable row level security;
alter table intents enable row level security;
alter table negotiation_sessions enable row level security;
alter table battles enable row level security;

-- Public read access for discovery (agents, listings, battles)
create policy "agents_public_read" on agents
    for select using (true);

create policy "listings_public_read" on listings
    for select using (true);

create policy "battles_public_read" on battles
    for select using (true);

-- Authenticated write access — agents own their data
create policy "agents_owner_write" on agents
    for insert with check (owner_id = auth.uid()::text);
create policy "agents_owner_update" on agents
    for update using (owner_id = auth.uid()::text);

create policy "listings_owner_write" on listings
    for insert with check (seller_agent_id in (
        select id from agents where owner_id = auth.uid()::text
    ));
create policy "listings_owner_update" on listings
    for update using (seller_agent_id in (
        select id from agents where owner_id = auth.uid()::text
    ));

create policy "intents_owner_write" on intents
    for insert with check (agent_id in (
        select id from agents where owner_id = auth.uid()::text
    ));

create policy "negotiation_insert" on negotiation_sessions
    for insert with check (true);  -- both parties' agents authorize via protocol
create policy "negotiation_update" on negotiation_sessions
    for update using (
        buyer_agent_id in (select id from agents where owner_id = auth.uid()::text)
        or seller_agent_id in (select id from agents where owner_id = auth.uid()::text)
    );

create policy "battles_insert" on battles
    for insert with check (true);  -- system records battles, not users

-- ============================================================
-- 7. HELPER FUNCTIONS
-- ============================================================

-- Auto-expire intents that have passed their TTL
create or replace function expire_stale_intents()
returns trigger as $$
begin
    update intents
    set is_active = false
    where is_active = true
      and created_at + (ttl_seconds || ' seconds')::interval < now();
    return null;
end;
$$ language plpgsql;

-- Auto-update agent last_seen on heartbeat
create or replace function touch_agent_last_seen(agent_id text)
returns void as $$
begin
    update agents set last_seen = now() where id = agent_id;
end;
$$ language plpgsql;

-- Leaderboard: rank agents by ELO with optional filters
create or replace function get_leaderboard(
    p_asset_class text default '',
    p_min_battles integer default 0,
    p_limit integer default 50
)
returns table (
    rank        bigint,
    agent_id    text,
    agent_name  text,
    owner_id    text,
    elo         real,
    tier        text,
    battles     integer,
    wins        integer,
    win_rate    real,
    earned      real,
    saved       real
) as $$
begin
    return query
    select
        row_number() over (order by a.elo_rating desc)::bigint as rank,
        a.id,
        a.name,
        a.owner_id,
        a.elo_rating,
        case
            when a.elo_rating >= 1700 then 'master'
            when a.elo_rating >= 1500 then 'diamond'
            when a.elo_rating >= 1300 then 'gold'
            when a.elo_rating >= 1100 then 'silver'
            else 'bronze'
        end,
        a.battles_fought,
        a.battles_won,
        case when a.battles_fought > 0
            then (a.battles_won::real / a.battles_fought::real) else 0.0
        end,
        a.total_earned,
        a.total_saved
    from agents a
    where a.battles_fought >= p_min_battles
      and (p_asset_class = '' or a.tradable_assets ? p_asset_class)
    order by a.elo_rating desc
    limit p_limit;
end;
$$ language plpgsql stable;

-- ============================================================
-- 8. REALTIME (Supabase Realtime for Arena live feed)
-- ============================================================

-- Enable realtime on battles for live spectator feed
alter publication supabase_realtime add table battles;
alter publication supabase_realtime add table listings;
