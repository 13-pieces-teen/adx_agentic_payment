BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.runtime_runs (
    runtime_run_id TEXT PRIMARY KEY CHECK (runtime_run_id <> ''),
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    round_id TEXT NOT NULL,
    runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('hosted')),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'leased', 'running', 'completed', 'failed')
    ),
    stage TEXT NOT NULL DEFAULT 'decide' CHECK (
        stage IN ('decide', 'match', 'negotiate', 'completed')
    ),
    leased_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    safe_error_code TEXT CHECK (
        safe_error_code IS NULL
        OR (
            safe_error_code <> ''
            AND char_length(safe_error_code) <= 100
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (round_id, runtime_kind),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id) ON DELETE CASCADE,
    CHECK (
        (
            status IN ('queued', 'leased', 'running')
            AND completed_at IS NULL
        )
        OR (
            status IN ('completed', 'failed')
            AND completed_at IS NOT NULL
        )
    ),
    CHECK (
        (status IN ('leased', 'running') AND leased_by IS NOT NULL)
        OR (status NOT IN ('leased', 'running'))
    )
);

CREATE INDEX runtime_runs_claim_idx
    ON arena402.runtime_runs (
        status,
        lease_expires_at,
        created_at,
        runtime_run_id
    );

GRANT SELECT, INSERT, UPDATE, DELETE ON arena402.runtime_runs
TO adx_arena_core;
GRANT SELECT ON arena402.runtime_runs TO adx_arena_api;

RESET ROLE;

COMMIT;
