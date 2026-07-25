-- Restore the current Credential when a same-Provider Runtime PATCH fails.
--
-- Update validation reuses the already-bound Credential. The candidate config
-- remains private until complete_credential_validation applies it by CAS. A
-- failed/cancelled update must therefore restore the existing Credential to
-- valid; create/replace failures keep their existing invalidation semantics.

CREATE OR REPLACE FUNCTION restore_failed_hosted_update_credential()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $restore_failed_hosted_update_credential$
BEGIN
    IF NEW.job_kind = 'update'
       AND NEW.status IN ('failed', 'cancelled')
       AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE public.arena_model_credentials
        SET status = 'valid',
            updated_at = clock_timestamp()
        WHERE credential_id = NEW.credential_id
          AND status = 'pending_validation';
    END IF;
    RETURN NEW;
END
$restore_failed_hosted_update_credential$;

ALTER FUNCTION restore_failed_hosted_update_credential()
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION restore_failed_hosted_update_credential()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS hosted_update_validation_restore_credential
    ON hosted_credential_validation_jobs;
CREATE TRIGGER hosted_update_validation_restore_credential
AFTER UPDATE OF status ON hosted_credential_validation_jobs
FOR EACH ROW
EXECUTE FUNCTION restore_failed_hosted_update_credential();
