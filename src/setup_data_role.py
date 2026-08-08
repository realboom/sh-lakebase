# Databricks notebook source
# MAGIC %pip install psycopg2-binary databricks-sdk --upgrade -q

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# Grant an identity DIRECT-CONNECTION Postgres data access on a Lakebase database.
#
# This is the DATA-PLANE counterpart to the control-plane grants the pre-hook does over
# the CLI. It creates the identity's OAuth Postgres role and GRANTs table DML — but does
# NOT grant it TO `authenticator` (that is the Data API path, setup_dataapi_role). So this
# gives direct psql/JDBC/driver access and does NOT require "Enable Data API".
#
# Identities + privileges are DERIVED from each project's `permissions:` block by the
# post-hook, which runs this once per (project, database, principal). Privilege policy
# (post-hook decides, passed in via `privileges`):
#   • SERVICE_PRINCIPAL -> SELECT,INSERT,UPDATE,DELETE  (runtime writer)
#   • USER              -> SELECT                       (read-only inspector)
#
# DESIGN — grants are ADD-ONLY BY DESIGN. The permissions block BOOTSTRAPS initial data
# access; ongoing data-plane changes (add/remove/adjust privileges) are managed directly
# in Postgres by an admin/DBA and are intentionally NOT reconciled or pruned. Do NOT add
# a drift/revoke reconciler — that fights how Postgres grants (live SQL state) actually
# work. FUTURE: if DABs gains first-class Postgres data-plane grant resources, this whole
# notebook + post-hook orchestration collapses into the bundle and DABs can own both
# planes declaratively (real add AND remove). Until then this is the bridge.
#
# Runs AS the project owner (the creator SP, a databricks_superuser member) over an OAuth
# database credential — the only session type allowed to create OAuth roles. Idempotent.
#
# IMPORTANT: the identity being onboarded must NOT be the database owner / a
# databricks_superuser member (that is only relevant for the authenticator path, which
# this notebook skips — so there is no 42501 concern here; kept for parity of intent).
import re

import psycopg2
from databricks.sdk import WorkspaceClient

# No defaults on the targeting params — a missing value must FAIL, never silently grant
# against the wrong instance/database/identity.
dbutils.widgets.text("project_id", "")
dbutils.widgets.text("database", "")
dbutils.widgets.text("identity", "")            # SP application id (UUID) or user email
dbutils.widgets.text("identity_type", "")       # SERVICE_PRINCIPAL | USER | GROUP
dbutils.widgets.text("privileges", "SELECT")    # comma-sep table privileges to grant

PROJECT   = dbutils.widgets.get("project_id").strip()
DB        = dbutils.widgets.get("database").strip()
IDENTITY  = dbutils.widgets.get("identity").strip()
ID_TYPE   = dbutils.widgets.get("identity_type").strip().upper()
PRIVS_RAW = dbutils.widgets.get("privileges").strip()

_missing = [n for n, v in (("project_id", PROJECT), ("database", DB),
                           ("identity", IDENTITY), ("identity_type", ID_TYPE)) if not v]
if _missing:
    raise ValueError(f"Missing required parameter(s): {', '.join(_missing)}. "
                     f"Run with --params project_id=<slug>,database=<db>,identity=<id>,"
                     f"identity_type=<SERVICE_PRINCIPAL|USER|GROUP>[,privileges=SELECT,INSERT,...].")

if ID_TYPE not in ("SERVICE_PRINCIPAL", "USER", "GROUP"):
    raise ValueError(f"identity_type must be SERVICE_PRINCIPAL, USER, or GROUP (got '{ID_TYPE}').")

# Sanitize the privilege list against an allow-set (these are interpolated into SQL, so
# never pass them through unchecked). Anything unexpected fails loudly.
# Accept either '+' or ',' as the separator: the caller (prehook) uses '+' because
# `bundle run --params` is itself comma-delimited and would split a comma-joined value.
ALLOWED_PRIVS = {"SELECT", "INSERT", "UPDATE", "DELETE"}
PRIVS = [p.strip().upper() for p in re.split(r"[+,]", PRIVS_RAW) if p.strip()]
bad = [p for p in PRIVS if p not in ALLOWED_PRIVS]
if bad:
    raise ValueError(f"Unsupported privilege(s) {bad}. Allowed: {sorted(ALLOWED_PRIVS)}.")
if not PRIVS:
    PRIVS = ["SELECT"]
PRIV_SQL = ", ".join(PRIVS)

w = WorkspaceClient()
parent = f"projects/{PROJECT}/branches/production"
endpoint = f"{parent}/endpoints/primary"
host = next(iter(w.postgres.list_endpoints(parent=parent))).as_dict()["status"]["hosts"]["host"]
user = w.current_user.me().user_name   # connect as the project owner (superuser)

tok = w.postgres.generate_database_credential(endpoint=endpoint).token
c = psycopg2.connect(host=host, port=5432, dbname=DB, user=user, password=tok, sslmode="require")
c.autocommit = True
cur = c.cursor()
print(f"granting {ID_TYPE} '{IDENTITY}' [{PRIV_SQL}] on {host}/{DB} as {user}")

cur.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")

# Create the identity's OAuth Postgres role (no-op if it already exists). The role name
# used in GRANTs is the identity string itself (UUID for SPs, email for users, display
# name for groups) — always double-quoted (hyphens/@ require it).
try:
    cur.execute("SELECT databricks_create_role(%s, %s)", (IDENTITY, ID_TYPE))
    print(f"created role {IDENTITY}")
except psycopg2.Error as e:
    print(f"role {IDENTITY} already exists or create skipped: {e}")

# Enumerate ALL user schemas in the database (not just `public`) so a developer adding a
# new schema gets the right privileges on the next run. System/internal schemas excluded.
cur.execute("""
    SELECT schema_name FROM information_schema.schemata
    WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND schema_name NOT LIKE 'pg_temp%' AND schema_name NOT LIKE 'pg_toast_temp%'
    ORDER BY schema_name
""")
schemas = [r[0] for r in cur.fetchall()]
print(f"granting on schemas: {schemas}")

# Data-plane GRANTs per schema (idempotent). NOTE: no `GRANT <role> TO authenticator` —
# this is the direct-connection path, not the Data API path.
writes = bool({"INSERT", "UPDATE"} & set(PRIVS))
for sch in schemas:
    stmts = [
        f'GRANT USAGE ON SCHEMA "{sch}" TO "{IDENTITY}"',
        f'GRANT {PRIV_SQL} ON ALL TABLES IN SCHEMA "{sch}" TO "{IDENTITY}"',
        # Cover tables created later, too.
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{sch}" GRANT {PRIV_SQL} ON TABLES TO "{IDENTITY}"',
    ]
    # Sequences: only meaningful when write privileges are present (SERIAL/identity inserts).
    if writes:
        stmts.append(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{sch}" TO "{IDENTITY}"')
        stmts.append(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{sch}" GRANT USAGE, SELECT ON SEQUENCES TO "{IDENTITY}"')
    for stmt in stmts:
        cur.execute(stmt)
c.close()
print(f"data role setup complete for {IDENTITY} ({PRIV_SQL}) on {len(schemas)} schema(s)")
