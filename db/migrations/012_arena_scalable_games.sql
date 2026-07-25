BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE OR REPLACE FUNCTION arena402.enforce_game_participant_limit()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, arena402
AS $$
DECLARE
    v_max_participants INTEGER;
    v_participant_count INTEGER;
BEGIN
    SELECT max_participants
    INTO v_max_participants
    FROM arena402.games
    WHERE game_id = NEW.game_id
    FOR UPDATE;

    IF v_max_participants IS NULL THEN
        RAISE EXCEPTION 'game not found'
            USING ERRCODE = '23503';
    END IF;

    SELECT count(*)
    INTO v_participant_count
    FROM arena402.game_participants
    WHERE game_id = NEW.game_id;

    IF v_participant_count >= v_max_participants THEN
        RAISE EXCEPTION 'participant limit reached'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL
    ON FUNCTION arena402.enforce_game_participant_limit()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS game_participants_limit_guard
    ON arena402.game_participants;

CREATE TRIGGER game_participants_limit_guard
BEFORE INSERT ON arena402.game_participants
FOR EACH ROW
EXECUTE FUNCTION arena402.enforce_game_participant_limit();

RESET ROLE;

COMMIT;
