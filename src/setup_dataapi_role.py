# Databricks notebook source
# MAGIC %pip install psycopg2-binary databricks-sdk --upgrade -q

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# Grant the Data API service principal a Postgres role so PostgREST can act as it.
# Idempotent + FEVM-proof: the SP application id is read from the secret scope (so it always
# targets the CURRENT bootstrap SP, which changes every workspace rebuild) — never hardcoded.
#
# PREREQS (run in this order each FEVM cycle):
#   1. ./scripts/bootstrap_identity.sh        (creates the SP + stores creds in the scope)
#   2. bundle run setup_schema                (creates db + tables)
#   3. Enable the Data API in the UI          (Compute > Database instances > Data API > Enable)
#      -> this creates the `authenticator` role that the grants below reference
#   4. bundle run setup_dataapi_role          (this notebook)
import psycopg2
from databricks.sdk import WorkspaceClient

# No defaults on purpose: this grants a Postgres role, so a missing param must FAIL,
# never silently target the wrong instance/database. Pass all three at run time
# (job --params or notebook widgets).
dbutils.widgets.text("project_id", "")
dbutils.widgets.text("database", "")
# PRIMARY: pass the SP application id directly (sourced from the project's permissions
# block by the pre-hook). This drops the Databricks-secret-scope dependency entirely.
dbutils.widgets.text("identity", "")
# LEGACY FALLBACK (original repo): read the SP id from a secret scope. Used only if
# `identity` is blank. Kept so the original single-SP/KV setup still works.
dbutils.widgets.text("secret_scope", "")
dbutils.widgets.text("client_id_key", "")   # vault key name holding the SP client id
PROJECT  = dbutils.widgets.get("project_id").strip()
DB       = dbutils.widgets.get("database").strip()
IDENTITY = dbutils.widgets.get("identity").strip()
SCOPE    = dbutils.widgets.get("secret_scope").strip()
CLIENT_ID_KEY = dbutils.widgets.get("client_id_key").strip() or "sp_client_id"
_missing = [n for n, v in (("project_id", PROJECT), ("database", DB)) if not v]
if _missing:
    raise ValueError(f"Missing required parameter(s): {', '.join(_missing)}. "
                     f"Run with --params project_id=<slug>,database=<postgres_db>,identity=<sp_app_id> "
                     f"(or legacy secret_scope=<scope>[,client_id_key=<key>]).")

# SP id: prefer the direct `identity` param; fall back to the secret scope for the
# legacy setup. One of the two must resolve.
if IDENTITY:
    SP = IDENTITY
elif SCOPE:
    SP = dbutils.secrets.get(SCOPE, CLIENT_ID_KEY)   # legacy per-value-stream vault key
else:
    raise ValueError("Provide the SP application id via `identity=<sp_app_id>` "
                     "(preferred) or the legacy `secret_scope=<scope>[,client_id_key=<key>]`.")

w = WorkspaceClient()
parent = f"projects/{PROJECT}/branches/production"
endpoint = f"{parent}/endpoints/primary"
host = next(iter(w.postgres.list_endpoints(parent=parent))).as_dict()["status"]["hosts"]["host"]
user = w.current_user.me().user_name   # connect as the project owner

tok = w.postgres.generate_database_credential(endpoint=endpoint).token
c = psycopg2.connect(host=host, port=5432, dbname=DB, user=user, password=tok, sslmode="require")
c.autocommit = True
cur = c.cursor()
print(f"granting Data API role to SP {SP} on {host}/{DB} as {user}")

cur.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")

# Create the SP's Postgres role (no-op if it already exists).
try:
    cur.execute("SELECT databricks_create_role(%s, 'SERVICE_PRINCIPAL')", (SP,))
    print(f"created role {SP}")
except psycopg2.Error as e:
    print(f"role {SP} already exists or create skipped: {e}")

# The one grant that specifically needs superuser and enables the Data API path:
# let PostgREST's `authenticator` assume this SP's role. Not schema-scoped.
cur.execute(f'GRANT "{SP}" TO authenticator')

# Table/sequence DML: enumerate ALL user schemas (not just `public`) so a developer
# adding a new database/schema gets Data API access on the next run. Mirrors the schema
# enumeration in setup_data_role. GRANTs are idempotent; identifiers quoted (SP id has hyphens).
cur.execute("""
    SELECT schema_name FROM information_schema.schemata
    WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND schema_name NOT LIKE 'pg_temp%' AND schema_name NOT LIKE 'pg_toast_temp%'
    ORDER BY schema_name
""")
schemas = [r[0] for r in cur.fetchall()]
print(f"granting on schemas: {schemas}")
for sch in schemas:
    for stmt in (
        f'GRANT USAGE ON SCHEMA "{sch}" TO "{SP}"',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{sch}" TO "{SP}"',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{sch}" TO "{SP}"',
        # Cover objects created later, too.
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{sch}" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{SP}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{sch}" GRANT USAGE, SELECT ON SEQUENCES TO "{SP}"',
    ):
        cur.execute(stmt)
c.close()
print(f"Data API role setup complete for {SP} on {len(schemas)} schema(s)")
