-- Give each Agent-selected Engagement its own legacy-compatible pool entries
-- without weakening the one-entry-per-participant invariant for FCFS rounds.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.pool_entries
    ADD COLUMN market_engagement_id TEXT,
    DROP CONSTRAINT pool_entries_round_id_game_participant_id_key,
    ADD CONSTRAINT pool_entries_market_engagement_fk
        FOREIGN KEY (market_engagement_id)
        REFERENCES arena402.market_engagements(engagement_id)
        ON DELETE RESTRICT;

CREATE UNIQUE INDEX pool_entries_fcfs_participant_uidx
    ON arena402.pool_entries (round_id, game_participant_id)
    WHERE market_engagement_id IS NULL;

CREATE UNIQUE INDEX pool_entries_a2a_engagement_participant_uidx
    ON arena402.pool_entries (
        market_engagement_id,
        game_participant_id
    )
    WHERE market_engagement_id IS NOT NULL;

RESET ROLE;

COMMIT;
