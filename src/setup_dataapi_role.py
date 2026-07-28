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
dbutils.widgets.text("secret_scope", "")
PROJECT = dbutils.widgets.get("project_id").strip()
DB      = dbutils.widgets.get("database").strip()
SCOPE   = dbutils.widgets.get("secret_scope").strip()
_missing = [n for n, v in (("project_id", PROJECT), ("database", DB), ("secret_scope", SCOPE)) if not v]
if _missing:
    raise ValueError(f"Missing required parameter(s): {', '.join(_missing)}. "
                     f"Run with --params project_id=<slug>,database=<postgres_db>,secret_scope=<scope>.")

SP = dbutils.secrets.get(SCOPE, "sp_client_id")   # current Data API SP (from bootstrap)

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

# GRANTs are idempotent — safe to re-run. Identifiers must be quoted (the SP id has hyphens).
for stmt in (
    f'GRANT "{SP}" TO authenticator',
    f'GRANT USAGE ON SCHEMA public TO "{SP}"',
    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{SP}"',
    f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{SP}"',
):
    cur.execute(stmt)
c.close()
print("Data API role setup complete")
