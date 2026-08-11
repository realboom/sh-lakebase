# Databricks DABs CI/CD — workflow guide

`databricks-cicd.yml` has **three jobs**, each fired by a different event.

| Job | Trigger | What it does |
|-----|---------|--------------|
| `validate-dev` | Pull request → main | `bundle validate -t dev` (safety check, no deploy) |
| `deploy-prod` | Push/merge → main | `bundle validate -t prod` + `bundle deploy -t prod` (steady state) |
| `onboard-lakebase` | **Manual** (Actions → Run workflow) | Create a NEW Lakebase instance **or** just deploy changes to an existing one |

Auth is OAuth M2M (service principal). Per-environment GitHub Environments hold:
- **Variables:** `DATABRICKS_HOST`
- **Secrets:** `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`
- (prod) add **required reviewers** on the `prod` environment to gate deploys.

---

## The `onboard-lakebase` manual job — inputs

Run it from **Actions → Databricks DABs CI/CD → Run workflow**.

Inputs render in this order:

| Input | When to set it | Notes |
|-------|----------------|-------|
| `target` | always | `dev` or `prod` — selects creds + (prod) approval gate |
| `create_new_instance` | to create a new instance | ☑ = create a brand-new instance. ☐ = deploy only |
| `resource_key` | when creating | DABs `postgres_projects` KEY (underscores, e.g. `sh_lakebase_proj_b`) from the committed `resources/lakebase_proj_*.yml`; used for the bind step |
| `instance_name` | when creating | SLUG / project_id (hyphens, e.g. `sh-lakebase-is-proj-b`). Fresh, never-used slug — never recycle a deleted one (7-day retention, see gotchas). |
| `capacity` | when creating | Transient provisioned SKU (`CU_1` smallest). The 0.5→2 autoscaling window is applied after adoption. |
| `run_data_api_grants` | AFTER manually enabling the Data API | ☑ runs the Data API SP role grant. **Only check this once you've clicked "Enable Data API" on the instance in the UI** (that manual step creates the `authenticator` role the grant needs). |

### The two independent toggles (they map to a partly-manual lifecycle)

```
create_new_instance ☑ :  create → bind → deploy → PATCH 0.5–2 CU window
        (manual, in UI):  click "Enable Data API" on the instance   ← no API for this
run_data_api_grants ☑ :  deploy (no-op) → grant Data API SP role
```

### What runs, by scenario

- **New instance:** check `create_new_instance` (+ key/slug/capacity). Later, after clicking Enable Data API in the UI, run again with `run_data_api_grants` ☑ + `instance_name`.
- **Update existing** (add a synced table, etc.): both boxes ☐ → validate + deploy only.
- **Grants only:** `run_data_api_grants` ☑ + `instance_name` → deploy (no-op) then grant.

Ops jobs (`setup_dataapi_role`, `setup_synced_table_grants`, `dataapi_examples`) are
**shared/reusable** — declared once in `resources/jobs.yml`, run per-instance with params:
```
databricks bundle run setup_dataapi_role -t dev \
  --params project_id=sh-lakebase-is-proj-b,database=sh_is_lakebase,secret_scope=sh-lakebase
```
They are NOT declared per-instance (that would create 3× jobs per instance).

---

## Adding a new instance end-to-end (config-as-data)

CI deploys from committed code — it can't invent the resource file at run time. So:

1. **PR:** copy `resources/lakebase_proj_a.yml` → `resources/lakebase_proj_<x>.yml`, then
   swap the per-instance tokens:
   - resource keys `sh_lakebase_proj_a` → `sh_lakebase_proj_<x>` (and the `proj_a_*` job keys)
   - `project_id: sh-lakebase-is-proj-a` → `sh-lakebase-is-proj-<x>`
   - catalog stem `sh_lakebase_is_proj_a` → `sh_lakebase_is_proj_<x>`
   - display name / tag values
   Get it reviewed + merged to main.
2. **Run `onboard-lakebase`:** `create_new_instance` = ☑, `instance_name` = the new slug,
   `resource_key` = the new project key, pick `target`.

Per-instance identity is **hardcoded in each file** (not shared variables); only
env-varying values (`environment`, `pg_storage_catalog`, `pg_storage_schema`) are shared
in `databricks.yml`. The catalog is composed per-env: `${environment}_sh_lakebase_is_proj_<x>`.

---

## Notes / gotchas
- **Runners:** jobs use `runs-on: self-hosted`. If self-hosted runners are disabled, this
  workflow is a reference — run the equivalent steps locally (see your project's
  onboarding runbook).
- **Never `bundle destroy`** a shared bundle — it deletes instances + data. Delete a single
  instance with `databricks database delete-database-instance <name>`.
- **Enabling the Data API has NO API/CLI/DABs path** — it is a manual UI click
  (Compute → Database instances → <instance> → Enable Data API). This is an irreducible
  manual gate between create and grant, so full hands-off onboarding is not possible today.
- **Deleting a slug does not free it for 7 days** — Lakebase soft-deletes with a 7-day
  retention/recovery window (`delete_time` → `purge_time`). The slug stays reserved (GET
  returns a tombstone) until purge. `--purge` is deprecated; there is no early-reclaim.
  So NEVER recycle a slug — increment: proj-a → proj-b → proj-c.
- **Headless self-hosted runner has no OS keyring.** `databricks auth token` fails on a
  runner started as a service ("OS keyring unreachable: exit status 36" → empty token →
  401). Mint OAuth tokens for curl directly from the SP creds via `/oidc/v1/token` instead
  (the PATCH step does this). `bundle deploy`/`bundle run` are unaffected (they use the SP
  env vars directly).
- **No `continue-on-error`** — every step fails loudly. (An earlier version had it on the
  grant step, which turned the job green while the step errored — hidden failure. Removed.)
- **`resource_key` (underscores) ≠ `instance_name` (slug, hyphens).** Also, ALL resource
  keys incl. synced-table keys must be per-instance-namespaced (`proj_<x>_...`) or
  `bundle validate` errors on duplicate keys.
- **CI env config goes stale on FEVM rebuild** — the workspace host + SP creds in the
  GitHub environment must point at a STABLE workspace, or every rebuild breaks CI.
- **Global host** (`instance-<uid>.database.azuredatabricks.net`) works over the standard
  front-end Private Link (no SD PL); it serves **Postgres wire only**. The HTTP Data API is
  on the regional host and needs Service Direct Private Link.
