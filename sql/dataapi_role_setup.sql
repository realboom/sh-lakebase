-- Data API role setup — lets the `authenticator` role assume a service principal
-- so the PostgREST Data API can act as it. Run as the project owner, connected to
-- the `apitest` database, AFTER enabling the Data API in the workspace UI
-- (Compute > Database instances > <project> > Data API > Enable), which creates
-- the `authenticator` role.
--
-- PREFERRED PATH: run the `setup_dataapi_role` job instead — it reads the current SP
-- id from the secret scope (FEVM-proof) and needs no local psql. This .sql is the
-- manual fallback. The SP id changes every FEVM rebuild, so it is NOT hardcoded here:
-- pass it at invocation, e.g.
--   PGPASSWORD=$TOKEN psql "host=$HOST ... dbname=apitest user=$EMAIL sslmode=require" \
--     -v sp_id="$(databricks secrets get-secret sh-lakebase sp_client_id -o json | jq -r .value | base64 -d)" \
--     -f sql/dataapi_role_setup.sql
--
-- Do NOT use the database owner identity for the Data API — its role is
-- Lakebase-managed and cannot be assumed by `authenticator`. Use a service principal.

CREATE EXTENSION IF NOT EXISTS databricks_auth;

SELECT databricks_create_role(:'sp_id', 'SERVICE_PRINCIPAL');

GRANT :"sp_id" TO authenticator;
GRANT USAGE ON SCHEMA public TO :"sp_id";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"sp_id";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"sp_id";
