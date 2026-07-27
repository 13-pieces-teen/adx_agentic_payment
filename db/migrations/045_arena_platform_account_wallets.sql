BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- `user_id` has always been the wallet binding primary key and the payment
-- mandate authority. The provider subject is retained only as compatibility
-- metadata for wallets allocated before password accounts were admitted.
ALTER TABLE arena402.user_wallets
    ALTER COLUMN github_subject DROP NOT NULL;

COMMENT ON COLUMN arena402.user_wallets.github_subject IS
    'Optional legacy GitHub subject; user_id is the platform wallet authority';

RESET ROLE;

COMMIT;
