# sh-lakebase

Select Health Lakebase environment as a Databricks Asset Bundle (DABs).

## What's here

| Path | Purpose |
| --- | --- |
| `databricks.yml` | Bundle config + targets (`dev` = westus2 workspace, `central` = sandbox) |
| `resources/lakebase.yml` | Lakebase Autoscaling **project** (auto-creates `production` branch + `primary` endpoint); commented examples for branches/endpoints/catalogs/synced-tables |
| `resources/jobs.yml` | 4 **serverless** jobs: `setup_schema`, `insert_jiva_rows`, `insert_census_rows`, `dataapi_examples` |
| `src/setup_schema.py` | Notebook — creates the `apitest` database + both tables + index (idempotent) |
| `src/setup_dataapi_role.py` | Notebook — grants the Data API SP its Postgres role; reads the current SP id from the secret scope (FEVM-proof, no local psql) |
| `src/insert_jiva_rows.py` | Notebook — appends **10** jiva rows per run |
| `src/insert_census_rows.py` | Notebook — appends **100** census rows per run (`CensusId` = MAX+1) |
| `src/dataapi_examples.py` | Notebook — Data API (PostgREST) examples via Python `requests`: select/filter, sort, count, offset + keyset pagination, POST |
| `sql/schema.sql` | Table + index DDL to paste into the **Lakebase SQL Editor** when the environment can't open a direct Postgres (5432) connection (so the `setup_schema` job can't run) |
| `sql/dataapi_role_setup.sql` | Service-principal role grants for the PostgREST Data API (manual, post-enable) |
| `scripts/bootstrap_identity.sh` | Re-runnable: creates the SP + OAuth secret, the admin/operator groups, and the secret scope for a fresh FEVM workspace (run before `bundle deploy`) |
| `scripts/lakebase_prehook.py` | CI/CD pre-hook: for **every** `postgres_projects` across all `resources/lakebase_*.yml`, creates the instance the legacy-provisioned way if missing (unlocks Dual Networking) and binds the autoscaling keys. `--phase post` sets the autoscale CU window after deploy. `--dry-run` prints the plan without acting; `--only <id>` scopes to one value stream. |

DABs manages the **infrastructure** (project/branch/endpoint) and the **jobs**. The table
DDL lives in `src/setup_schema.py` (psycopg2) **and** mirrored in `sql/schema.sql` for
environments that can't use a driver — keep the two in sync if columns change.

## If the environment can't open Postgres :5432 (Public Preview gating)

Some environments can't open a direct Postgres (5432) connection to Lakebase. In that case
**the psycopg2 jobs (`setup_schema`, `insert_*`, `setup_dataapi_role`) won't run**, and the
**Data API (PostgREST) is HTTP-only and cannot run DDL/DCL**. Split the work by operation:

| Operation | Type | How (no 5432) |
| --- | --- | --- |
| Create `apitest` **database** | — | `bundle deploy` (postgres_catalog `create_database_if_missing: true`) or `databricks postgres create-database` — both HTTP APIs |
| Create **tables + index** (DDL) | DDL | Paste **`sql/schema.sql`** into the **Lakebase SQL Editor** (Compute → Database instances → `sh-lakebase` → SQL editor, connected to `apitest`) |
| Grant the Data API **SP role** (`GRANT …`) | DCL | Paste `sql/dataapi_role_setup.sql` into the **SQL Editor** (substitute the SP id; PostgREST can't run GRANT) |
| **Insert / query** rows | DML | Use the **Data API** (PostgREST over HTTPS) — see `src/dataapi_examples.py` |

So in a 5432-blocked env: schema + grants are **one-time SQL-Editor** steps; all ongoing
data access goes through the **Data API**.

## Fresh-start flow

```bash
databricks bundle validate -t dev
databricks bundle deploy   -t dev               # creates the project + 3 jobs
databricks bundle run setup_schema       -t dev # builds database + tables + index
databricks bundle run insert_jiva_rows   -t dev # +10 jiva rows
databricks bundle run insert_census_rows -t dev # +100 census rows (re-run to add more)
```

> **WARNING:** don't change immutable project fields (`project_id`, `pg_version`) once
> the project exists. DABs can't replace a Lakebase project in place — it tries to
> recreate, which deletes all data and then fails on the locked id. To change them,
> delete the project first.

## Targets

| Target | Mode | Workspace | UC catalog | Identity |
| --- | --- | --- | --- | --- |
| `dev` (default) | `development` | westus2 (`adb-XXXXXXXXXXXXXXXX`) | `sh_lakebase_apitest` | deploys/runs as **you** |
| `prod` | `production` | `adb-XXXXXXXXXXXXXXXX` | `sh_lakebase_apitest_prod` | deploys/runs as **you** (operator), `root_path` `/Workspace/Shared/...` |

**dev + prod share a metastore**, so the UC catalog name is per-target (`pg_catalog_id`
variable) — same name in one metastore collides with "Cross workspace access is not allowed".

**Why prod isn't SP-governed:** full prod governance (SP `run_as`, group ACLs, SP-scoped
`root_path`) needs stable, workspace-bound identities that FEVM throwaway workspaces don't
provide — SP `run_as` requires re-granting `servicePrincipal.user` on a new SP every cycle,
and a SCIM SP has no home folder for `root_path`. So prod is operated like dev. The
`lakebase-deploy` SP still exists (bootstrap) and powers the **Data API** via the secret
scope. Restore governance only for a long-lived (non-FEVM) prod deployed via CI as the SP.

## CI/CD auth: the `gh-actions` profile (MSAL vs OAuth-M2M)

The GitHub workflow (`.github/workflows/databricks-cicd.yml`) authenticates every CLI
call with **`--profile gh-actions`**, written to `~/.databrickscfg` by the "Set up
Databricks CLI authentication" step. **Only the auth lines in that profile differ between
environments — the profile NAME stays `gh-actions`, so nothing downstream changes.**

Client (real) — Managed Identity / MSAL:
```ini
[gh-actions]
host            = <workspace-url>
azure_client_id = <ARM_CLIENT_ID>
azure_use_msi   = true
```

This repo's test workspace — OAuth M2M service principal (MSAL can't be reproduced on
this workspace's Entra tenant):
```ini
[gh-actions]
host          = <workspace-url>
client_id     = <DATABRICKS_CLIENT_ID>
client_secret = <DATABRICKS_CLIENT_SECRET>
```

To switch between them, swap only the auth lines in the cfg-writing step. **Do NOT also
set `DATABRICKS_*` as job/step env vars** — env auth OVERRIDES the profile (env wins over
`.databrickscfg`), causing silent auth-source confusion. Auth lives in the profile only.

GitHub Environment settings the workflow expects, per target:
`vars.DATABRICKS_HOST`, `vars.DATABRICKS_CLIENT_ID` (or `vars.ARM_CLIENT_ID` for MSAL),
`secrets.DATABRICKS_CLIENT_SECRET`, and optional `vars.LAKEBASE_SECRET_SCOPE`.

## FEVM rebuild cycle (~every 2 weeks)

These workspaces are FEVM-provisioned and get **destroyed and rebuilt** on a ~2-week TTL.
Each rebuild produces a **new host and a new workspace id**. The bundle is built so the
**only** workspace-pinned value you must edit per cycle is the target `host` line in
`databricks.yml` — the workspace id and the Data API endpoint host are both derived at
**runtime** by the notebooks (so they need no edits).

> **Auth caveat:** `bundle ... -t <target>` auto-selects the profile whose host matches the
> target, so those just work. But `bootstrap_identity.sh` is a *non-bundle* script — it
> provisions whatever your CLI auth resolves to, and your **default profile may be a
> different workspace**. Always run bootstrap with an explicit profile and confirm the host
> it prints. Create one profile per workspace: `databricks auth login --host <host> --profile <name>`.

Run this against each freshly minted workspace (examples use `-t prod` / the `sh-prod` profile):

```bash
# 1. Create/refresh a profile for the new workspace
databricks auth login --host https://<new-workspace-host> --profile <profile>

# 2. Bootstrap workspace identity — creates the SP + OAuth secret, the two groups, and the
#    `sh-lakebase` secret scope. Run with the target profile; it cd's out of the bundle dir
#    itself and PRINTS the workspace host — confirm it before it proceeds.
DATABRICKS_CONFIG_PROFILE=<profile> ./scripts/bootstrap_identity.sh

# 3. Update the target `host:` line in databricks.yml to the new workspace.
#    (No SP/group values to paste — prod is operator-run.)

# 4. Drop the orphaned UC catalog from the destroyed workspace (else deploy fails with
#    "Cross workspace access is not allowed"). NOTE: `databricks catalogs delete` REFUSES
#    managed-postgres catalogs — use the postgres API with the full catalogs/<id> path.
#    Use the right name per target: sh_lakebase_apitest (dev) / sh_lakebase_apitest_prod (prod):
databricks postgres delete-catalog catalogs/sh_lakebase_apitest_prod   # ignore "does not exist"

# 5. Deploy + seed
databricks bundle deploy            -t prod
databricks bundle run setup_schema  -t prod
databricks bundle run insert_jiva_rows   -t prod
databricks bundle run insert_census_rows -t prod

# 6. Data API: enable in the UI, then grant the SP its Postgres role
databricks bundle run setup_dataapi_role -t prod
databricks bundle run dataapi_examples   -t prod   # optional: verify end-to-end
```

> If `bundle deploy` ever errors with `WAL serial ... ahead of expected`, the local state
> is corrupt — safe to clear (nothing is deployed yet):
> `rm -f .databricks/bundle/<target>/resources.json{,.wal}` then redeploy.

> The `lakebase-deploy` SP powers the **Data API** (its creds live in the `sh-lakebase`
> secret scope). It must exist *before* `setup_dataapi_role`/`dataapi_examples`, which is
> why bootstrap runs first each cycle.

## Data API (PostgREST over HTTP)

The Data API lets external clients query the database over HTTPS (no Postgres driver).
It must be **enabled per workspace** (UI step below).

### Reference values — STALE EXAMPLE (regenerated each FEVM cycle)

> ⚠️ The values below are from a **prior** workspace and are **not current** — every FEVM
> rebuild produces a new endpoint host, workspace id, and SP. They're kept only to show the
> shape. Re-derive the live values each cycle: endpoint host via the command below; the SP
> (`lakebase-deploy`) client id is printed by `scripts/bootstrap_identity.sh`; the workspace
> id is self-derived by the notebook (or read it from the new host `adb-<workspace-id>`).

| | |
| --- | --- |
| Endpoint host | `ep-XXXXXXXX.database.REGION.azuredatabricks.net` |
| Workspace ID | `XXXXXXXXXXXXXXXX` |
| Database | `apitest` |
| REST base | `https://<host>/api/2.0/workspace/<workspace-id>/rest/<database>` |
| Service principal | `lakebase-dataapi-test` — client id `00000000-0000-0000-0000-000000000000` |

> The endpoint host changes if the project is recreated — re-derive with:
> `databricks postgres list-endpoints projects/sh-lakebase/branches/production -o json | jq -r '.[0].status.hosts.host'`

### Enable + grant (per FEVM cycle)

1. **Enable the Data API** in the UI (Compute → Database instances → `sh-lakebase` → Data API → Enable). Creates the `authenticator` role. *No CLI/DABs option for this step.*
2. **Grant the SP its Postgres role** — run the bundle job (reads the current SP id from the
   secret scope, so it always targets the live bootstrap SP — no hardcoded id, no local psql):
   ```bash
   databricks bundle run setup_dataapi_role -t dev
   ```
   > Manual fallback (needs local `psql`): `sql/dataapi_role_setup.sql` takes the SP id as a
   > `-v sp_id=...` variable — see the header of that file.

### Query it

```bash
# 1) Mint an SP secret (store it; shown once). The client id is not a secret.
SECRET=$(databricks service-principal-secrets-proxy create <SP_APPLICATION_ID> -o json | jq -r '.secret')

# 2) Mint a Lakebase OAuth token as the SP (config-file ignored, forces M2M).
REST="https://ep-XXXXXXXX.database.REGION.azuredatabricks.net/api/2.0/workspace/XXXXXXXXXXXXXXXX/rest/apitest"
SP_TOKEN=$(DATABRICKS_CONFIG_FILE=/dev/null DATABRICKS_AUTH_TYPE=oauth-m2m \
  DATABRICKS_HOST="https://adb-XXXXXXXXXXXXXXXX.NN.azuredatabricks.net" \
  DATABRICKS_CLIENT_ID="00000000-0000-0000-0000-000000000000" DATABRICKS_CLIENT_SECRET="$SECRET" \
  databricks postgres generate-database-credential projects/sh-lakebase/branches/production/endpoints/primary -o json | jq -r '.token')

# 3) Query (PostgREST syntax). Append /public/<table>.
curl -s -H "Authorization: Bearer $SP_TOKEN" \
  "$REST/public/epic_hospital_census?select=CensusId,FacilityName,LineOfBusiness4&CensusId=in.(100001,100002)" | jq

# total count (Content-Range header)
curl -s -D - -o /dev/null -H "Authorization: Bearer $SP_TOKEN" -H "Prefer: count=exact" \
  "$REST/public/epic_hospital_census?select=CensusId" | grep -i content-range
```

### Consume it from a notebook (in the bundle)

`src/dataapi_examples.py` (job **`dataapi_examples`**) reimplements these calls with Python
`requests`. It reads the SP credentials from a **secret scope** (never hardcoded) and mints
the Lakebase token as the SP. One-time setup of the scope:

```bash
SECRET=$(databricks service-principal-secrets-proxy create <SP_APPLICATION_ID> -o json | jq -r '.secret')
databricks secrets create-scope sh-lakebase
databricks secrets put-secret  sh-lakebase sp_client_id     --string-value 00000000-0000-0000-0000-000000000000
databricks secrets put-secret  sh-lakebase sp_client_secret --string-value "$SECRET"
```
Then: `databricks bundle run dataapi_examples -t dev`

Notes: tokens expire ~1h (re-mint); never commit the SP **secret** (the client id is fine);
column names are case-sensitive — quote with double quotes in PostgREST (`"CensusId"`), but
with **backticks** in Databricks SQL.

See the internal runbook (Obsidian: *Lakebase Data API - HTTP Query Steps*) for full Data API
query examples, pagination (offset vs keyset), and the Private Link networking notes.
