# Databricks notebook source
# MAGIC %pip install databricks-sdk --upgrade -q

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
# Lakebase Data API (PostgREST) examples over the sh_is_lakebase database.
# Data now comes from SYNCED TABLES (continuously replicated from Unity Catalog
# sources), which live in the `epic_census` and `pdax_quality` Postgres schemas.
# The REST path is /rest/<database>/<schema>/<table> — note the schema segment is
# now epic_census / pdax_quality, not public.
#
# IMPORTANT: synced tables are READ-ONLY. The Data API can SELECT them but cannot
# INSERT/UPDATE/DELETE (the old jiva INSERT example is gone) — writes must happen
# at the Unity Catalog source, then replicate down.
#
# The Data API must be called as the SERVICE PRINCIPAL (the owner identity can't be
# assumed by `authenticator`). SP creds are read from a secret scope — never hardcoded.
import json
import requests
from databricks.sdk import WorkspaceClient

# project_id/database/secret_scope have NO defaults on purpose (must target the
# instance explicitly). workspace_id blank = auto-derived at runtime (FEVM-proof).
dbutils.widgets.text("project_id", "")
dbutils.widgets.text("database", "")
dbutils.widgets.text("workspace_id", "")          # blank -> auto-derived at runtime (FEVM-proof)
dbutils.widgets.text("secret_scope", "")
PROJECT = dbutils.widgets.get("project_id").strip()
DB      = dbutils.widgets.get("database").strip()
SCOPE   = dbutils.widgets.get("secret_scope").strip()
_missing = [n for n, v in (("project_id", PROJECT), ("database", DB), ("secret_scope", SCOPE)) if not v]
if _missing:
    raise ValueError(f"Missing required parameter(s): {', '.join(_missing)}. "
                     f"Run with --params project_id=<slug>,database=<postgres_db>,secret_scope=<scope>.")
WS_ID_OVERRIDE = dbutils.widgets.get("workspace_id").strip()

# Owner client: derive the Lakebase endpoint host + the workspace URL.
w = WorkspaceClient()
parent = f"projects/{PROJECT}/branches/production"
host = next(iter(w.postgres.list_endpoints(parent=parent))).as_dict()["status"]["hosts"]["host"]
ws_url = w.config.host
# Workspace id for the REST path: use the override if supplied, else self-derive. The
# notebook runs IN the workspace, so it knows its own id — survives FEVM recreation with
# no config edits (the workspace id changes every time the instance is rebuilt).
WS_ID = WS_ID_OVERRIDE or str(w.get_workspace_id())

# SP creds from the secret scope -> mint a Lakebase OAuth token as the SP.
sp_id     = dbutils.secrets.get(SCOPE, "sp_client_id")
sp_secret = dbutils.secrets.get(SCOPE, "sp_client_secret")
w_sp = WorkspaceClient(host=ws_url, client_id=sp_id, client_secret=sp_secret)
token = w_sp.postgres.generate_database_credential(endpoint=f"{parent}/endpoints/primary").token

REST = f"https://{host}/api/2.0/workspace/{WS_ID}/rest/{DB}"
H = {"Authorization": f"Bearer {token}"}
print("REST base:", REST)

# COMMAND ----------
# 1) SELECT columns + filter on the epic_census schema: core_count IN (...)
#    Source: system.compute.node_types (one row per instance node type).
r = requests.get(f"{REST}/epic_census/node_types",
                 params={"select": "node_type,core_count,memory_mb",
                         "core_count": "in.(4,8,16)"}, headers=H)
print("status", r.status_code)
print(json.dumps(r.json(), indent=2))

# COMMAND ----------
# 2) Filter + sort: memory_mb > 100000, biggest first
r = requests.get(f"{REST}/epic_census/node_types",
                 params={"select": "node_type,core_count,memory_mb",
                         "memory_mb": "gt.100000", "order": "memory_mb.desc", "limit": "5"}, headers=H)
print("status", r.status_code)
print(json.dumps(r.json(), indent=2))

# COMMAND ----------
# 3) Total count via the Content-Range header (Prefer: count=exact)
r = requests.get(f"{REST}/epic_census/node_types",
                 params={"select": "node_type", "limit": "1"},
                 headers={**H, "Prefer": "count=exact"})
print("Content-Range:", r.headers.get("Content-Range"))   # e.g. 0-0/123

# COMMAND ----------
# 4) OFFSET/LIMIT pagination LOOP — walk the WHOLE table page by page.
#    `offset` grows by the page size each iteration; stop when a page comes back short
#    (fewer than PAGE rows) or empty. Simple and supports random access, BUT the database
#    re-scans and skips `offset` rows every request, so cost grows the deeper you page —
#    prefer keyset (#5) for large tables. An ORDER BY is REQUIRED for stable paging.
PAGE = 25
offset, pages, total, keys = 0, 0, 0, []
while True:
    r = requests.get(f"{REST}/epic_census/node_types",
                     params={"select": "node_type", "order": "node_type.asc",
                             "limit": str(PAGE), "offset": str(offset)}, headers=H)
    r.raise_for_status()                       # surface PostgREST errors instead of crashing on a dict
    rows = r.json()
    if not rows:                               # empty page -> past the end, done
        break
    keys.extend(row["node_type"] for row in rows)
    total += len(rows); pages += 1
    print(f"  page {pages}: offset={offset} got {len(rows)} -> node_type {rows[0]['node_type']}..{rows[-1]['node_type']}")
    if len(rows) < PAGE:                        # short page -> last page, avoid one extra empty request
        break
    offset += PAGE
print(f"offset paging: walked {total} rows in {pages} pages of {PAGE} (unique={len(set(keys))})")

# COMMAND ----------
# 5) KEYSET pagination (recommended): page forward by the last node_type seen — flat cost, no count
last, pages, total = "", 0, 0
while True:
    rows = requests.get(f"{REST}/epic_census/node_types",
                        params={"select": "node_type", "order": "node_type.asc",
                                "node_type": f"gt.{last}", "limit": "40"}, headers=H).json()
    if not rows:
        break
    last = rows[-1]["node_type"]; total += len(rows); pages += 1
print(f"keyset: walked {total} rows in {pages} pages (last node_type={last})")

# COMMAND ----------
# 6) Cross-schema read: the pdax_quality schema (source: system.query.history).
#    This replaces the old INSERT example — synced tables are READ-ONLY, so the Data API
#    can only SELECT them. To change data, write at the UC source and let it replicate.
r = requests.get(f"{REST}/pdax_quality/query_history",
                 params={"select": "statement_id,statement_type,execution_status",
                         "limit": "5"}, headers=H)
print("status", r.status_code)
print(json.dumps(r.json(), indent=2))
