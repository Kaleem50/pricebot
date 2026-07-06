db/migrations/003_beta_requests.sql is failing with:

ERROR: 23514: check constraint "usage_events_event_type_check" 
of relation "usage_events" is violated by some row

The migration tries to ADD a new CHECK constraint on usage_events.event_type
but the table already has an existing constraint with different allowed values,
and existing rows violate the new constraint.

Fix the migration:
1. First DROP the existing constraint before adding the new one:
   ALTER TABLE usage_events 
   DROP CONSTRAINT IF EXISTS usage_events_event_type_check;
   
   Then add the new one with all required values:
   ALTER TABLE usage_events
   ADD CONSTRAINT usage_events_event_type_check
   CHECK (event_type IN (
     'batch_submitted',
     'batch_completed', 
     'price_applied',
     'credential_validated',
     'sync_completed',
     'api_call',
     'email_sent'
   ));

2. The fix must include ALL original values from the existing constraint
   PLUS the new ones ('api_call', 'email_sent') — not replace them.
   Check what values currently exist in the DB:
   SELECT DISTINCT event_type FROM usage_events;
   And include all of them in the new constraint.

3. Make the migration idempotent — wrap constraint operations in
   DO $$ BEGIN ... EXCEPTION WHEN duplicate_object THEN NULL; END $$;
   blocks so re-running never fails.

After fixing, run python3 db/migrate.py and confirm it completes
with no 400 errors. Report the final migration output.