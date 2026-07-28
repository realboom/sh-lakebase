# Databricks notebook source
# MAGIC %pip install psycopg2-binary databricks-sdk --upgrade -q

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# Grant a reader role SELECT on the synced-table schemas so the data is browsable via the
# DIRECT Postgres path (Lakebase SQL editor / Catalog Explorer "Data" preview). Those run as
# YOUR Postgres role; synced tables are created+owned by the sync pipeline's role, so a fresh
# database gives other roles no access -> "failed query" with an empty cause. (UC / SQL-warehouse
# federation is unaffected — it reads via a managed credential.)
#
# Runs IN the workspace (in-workspace Postgres connections don't need port 5432 / SD PL) and
# connects as the current user via a minted database credential. It first PRINTS ownership so we
# know whether the grants are permitted (must be run by an owner of the objects or a superuser).
import json
import psycopg2
from databricks.sdk import WorkspaceClient

report = {}   # collected + returned via dbutils.notebook.exit so the run output is inspectable

# project_id/database have NO defaults on purpose (grants must not target the wrong
# instance). schemas has a real default set; grant_to blank = current user (by design).
dbutils.widgets.text("project_id", "")
dbutils.widgets.text("database", "")
dbutils.widgets.text("schemas", "epic_census,pdax_quality")   # comma-separated
dbutils.widgets.text("grant_to", "")                          # blank -> current user
PROJECT = dbutils.widgets.get("project_id").strip()
DB      = dbutils.widgets.get("database").strip()
_missing = [n for n, v in (("project_id", PROJECT), ("database", DB)) if not v]
if _missing:
    raise ValueError(f"Missing required parameter(s): {', '.join(_missing)}. "
                     f"Run with --params project_id=<slug>,database=<postgres_db>[,schemas=...,grant_to=...].")
SCHEMAS = [s.strip() for s in dbutils.widgets.get("schemas").split(",") if s.strip()]

w = WorkspaceClient()
parent = f"projects/{PROJECT}/branches/production"
endpoint = f"{parent}/endpoints/primary"
host = next(iter(w.postgres.list_endpoints(parent=parent))).as_dict()["status"]["hosts"]["host"]
user = w.current_user.me().user_name
GRANT_TO = dbutils.widgets.get("grant_to").strip() or user

tok = w.postgres.generate_database_credential(endpoint=endpoint).token
c = psycopg2.connect(host=host, port=5432, dbname=DB, user=user, password=tok, sslmode="require")
c.autocommit = True
cur = c.cursor()

# COMMAND ----------
# --- Introspect: who am I, am I a superuser, and who owns the synced schemas/tables? ---
cur.execute("SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)")
me, is_super = cur.fetchone()
report["connected_as"] = me
report["superuser"] = is_super

cur.execute("""
    SELECT n.nspname, r.rolname
    FROM pg_namespace n JOIN pg_roles r ON n.nspowner = r.oid
    WHERE n.nspname = ANY(%s)
""", (SCHEMAS,))
report["schema_owners"] = {sch: owner for sch, owner in cur.fetchall()}

cur.execute("""
    SELECT schemaname, tablename, tableowner
    FROM pg_tables WHERE schemaname = ANY(%s)
    ORDER BY schemaname, tablename
""", (SCHEMAS,))
report["table_owners"] = {f"{sch}.{tbl}": owner for sch, tbl, owner in cur.fetchall()}

cur.execute("""
    SELECT r.rolname FROM pg_auth_members m
    JOIN pg_roles r ON m.roleid = r.oid
    WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
""")
report["member_of"] = [row[0] for row in cur.fetchall()]

# COMMAND ----------
# --- Grant SELECT/USAGE to the reader on each synced schema (idempotent). ---
# Per-statement try/except so one failure (e.g. not the owner) is reported, not fatal.
report["grant_to"] = GRANT_TO
report["grants"] = []
for sch in SCHEMAS:
    for stmt in (
        f'GRANT USAGE ON SCHEMA "{sch}" TO "{GRANT_TO}"',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{sch}" TO "{GRANT_TO}"',
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{sch}" GRANT SELECT ON TABLES TO "{GRANT_TO}"',
    ):
        try:
            cur.execute(stmt)
            report["grants"].append({"stmt": stmt, "result": "OK"})
        except psycopg2.Error as e:
            report["grants"].append({"stmt": stmt, "result": "FAILED", "error": str(e).strip()})
# Verify the grant actually landed (GRANT ON ALL TABLES can skip tables silently).
report["verify_select"] = {}
for sch in SCHEMAS:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s", (sch,))
    for (tbl,) in cur.fetchall():
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (GRANT_TO, f'{sch}.{tbl}'))
        report["verify_select"][f"{sch}.{tbl}"] = cur.fetchone()[0]
c.close()

print(json.dumps(report, indent=2))
dbutils.notebook.exit(json.dumps(report))
