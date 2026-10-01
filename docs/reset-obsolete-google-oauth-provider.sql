-- One-time clean-history reset for installations that applied the retired
-- core-owned Google Workspace provider migration before the 1.0 baseline.
--
-- Run this only during a maintenance window, before the normal
-- `flow-steward database upgrade --maintenance-confirmed --confirm` command.
-- The transaction is intentionally fail-closed: any retained secret or
-- reference must be migrated or removed deliberately before this reset.

BEGIN;

LOCK TABLE
    public.providers,
    public.provider_secrets,
    public.provider_health_events,
    public.notification_routes,
    public.notification_delivery_logs,
    public.external_sources,
    public.project_connections,
    public.platform_schema_migrations
IN SHARE ROW EXCLUSIVE MODE;

DO $reset_obsolete_google_oauth_provider$
DECLARE
    obsolete_provider_id uuid;
    obsolete_provider_count integer;
BEGIN
    SELECT count(*), (array_agg(id ORDER BY id))[1]
      INTO obsolete_provider_count, obsolete_provider_id
      FROM public.providers
     WHERE provider_key = 'google_workspace';

    IF obsolete_provider_count > 1 THEN
        RAISE EXCEPTION
            'clean-history reset refused: obsolete provider key is not unique';
    END IF;

    IF obsolete_provider_id IS NOT NULL AND (
        EXISTS (
            SELECT 1 FROM public.provider_secrets
             WHERE provider_id = obsolete_provider_id
        )
        OR EXISTS (
            SELECT 1 FROM public.provider_health_events
             WHERE provider_id = obsolete_provider_id
        )
        OR EXISTS (
            SELECT 1 FROM public.notification_routes
             WHERE provider_id = obsolete_provider_id
        )
        OR EXISTS (
            SELECT 1 FROM public.notification_delivery_logs
             WHERE provider_id = obsolete_provider_id
        )
        OR EXISTS (
            SELECT 1 FROM public.external_sources
             WHERE provider_id = obsolete_provider_id
        )
    ) THEN
        RAISE EXCEPTION
            'clean-history reset refused: obsolete provider still has references or secrets';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.project_connections
         WHERE extension_id = 'flowsteward.google-workspace'
           AND connection_type = 'google_workspace_account'
    ) THEN
        RAISE EXCEPTION
            'clean-history reset refused: obsolete provider connection still exists';
    END IF;

    DELETE FROM public.providers
     WHERE id = obsolete_provider_id;

    DELETE FROM public.platform_schema_migrations
     WHERE migration_id = '20260825090000_google_workspace_provider.sql';
END
$reset_obsolete_google_oauth_provider$;

COMMIT;
