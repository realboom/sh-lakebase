#!/usr/bin/env python3
"""
Lakebase dual-network CI/CD pre-hook — multi-project, bind-by-declared-type.

Adapted from Marcin's single-project prehook for THIS project's value-stream
model: multiple resources/lakebase_*.yml files, each declaring its own
postgres_projects (+ postgres_catalogs, + optionally branches/endpoints later).

WHAT IT DOES (runs BEFORE `databricks bundle deploy`):
  For EACH postgres_projects entry the bundle resolves:
    Step 1  If the instance does NOT exist, create it the LEGACY PROVISIONED way
            (databricks database create-database-instance) and wait for AVAILABLE.
            Creating provisioned-first is what unlocks Dual Networking; the bundle
            then adopts it as an autoscaling project via bind.
    Step 2  BIND the autoscaling resource keys to that existing instance:
              • postgres_projects key         -> projects/<project_id>          [always]
              • postgres_branches key(s)       -> projects/<id>/branches/<branch> [only if declared]
              • postgres_endpoints key(s)      -> .../branches/<b>/endpoints/<ep> [only if declared]
            Catalogs and synced tables are NOT bound: the legacy create does not
            produce them, so `bundle deploy` creates them fresh (no collision).
            EXCEPTION: if a declared catalog already EXISTS in the workspace
            (e.g. after an undelete/restore), it is bound too, to avoid
            CATALOG_ALREADY_EXISTS on deploy.

WHY bind-by-declared-type: today this project declares only projects + catalogs,
so only the project is bound. The day the team adds a `postgres_branches` block
(to manage branches via CI/CD instead of the UI), the legacy create also yields
the production branch/endpoint — this hook then automatically binds those too,
with no script change. New branches (e.g. feature-x) still need no bind; deploy
creates them declaratively.

No unbind is required: instances are never declared as `database_instances`
resources, so the bundle only ever tracks them through the autoscaling keys.

Depends only on the Python standard library and the `databricks` CLI (v1.2.0+),
authenticated via env in CI (OAuth SP: DATABRICKS_HOST + DATABRICKS_CLIENT_ID +
DATABRICKS_CLIENT_SECRET) or a --profile locally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def log(msg: str) -> None:
    print(f"[lakebase-prehook] {msg}", flush=True)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a databricks CLI command, echoing it first."""
    log("$ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


# --------------------------------------------------------------------------- #
# Bundle config resolution
# --------------------------------------------------------------------------- #
def load_bundle_config(cli: str, target: str, profile: str | None) -> dict:
    """
    Resolve the merged bundle config via `databricks bundle validate -o json`.
    This merges every resources/*.yml, so we see ALL value-stream projects at once.
    NOTE: it does NOT interpolate ${var.*}/${resources.*} — we resolve those below.
    """
    cmd = [cli, "bundle", "validate", "-t", target, "-o", "json"]
    if profile:
        cmd += ["-p", profile]
    # Do NOT check=True: `bundle validate` can exit non-zero on a warning while
    # still emitting valid JSON on stdout. We treat "parseable JSON" as success and
    # only fail if the JSON itself is missing/unparseable.
    proc = run(cmd, check=False, capture=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        log(f"`bundle validate` produced no parseable JSON (exit {proc.returncode}); output was:")
        log(proc.stdout)
        raise SystemExit(
            "Could not resolve bundle config. If this is an auth error (e.g. 'Refresh "
            "token is invalid'), re-authenticate the profile / refresh CI credentials."
        )


def resolve_vars(value: str, config: dict) -> str:
    """Resolve ${var.NAME} references against the bundle's variables map."""
    variables = config.get("variables", {})

    def repl(m: re.Match) -> str:
        name = m.group(1)
        var = variables.get(name, {})
        resolved = var.get("value", var.get("default"))
        if resolved is None:
            raise SystemExit(f"Cannot resolve ${{var.{name}}} — no value/default in databricks.yml.")
        return str(resolved)

    return re.sub(r"\$\{var\.([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)


def _project_key_for_ref(parent: str) -> str | None:
    """Extract the postgres_projects key from a ${resources.postgres_projects.<key>.id} ref."""
    m = re.search(r"\$\{resources\.postgres_projects\.([^.}]+)\.", parent)
    return m.group(1) if m else None


def _branch_key_for_ref(parent: str) -> str | None:
    """Extract the postgres_branches key from a ${resources.postgres_branches.<key>.id} ref."""
    m = re.search(r"\$\{resources\.postgres_branches\.([^.}]+)\.", parent)
    return m.group(1) if m else None


def extract_projects(config: dict) -> list[dict]:
    """
    Build one work item per postgres_projects entry, attaching any branches,
    endpoints, and catalogs that belong to it (matched via their `parent`/`branch`
    references). Returns a list of dicts:
      {
        "proj_key": str, "project_id": str,
        "branches":  [(branch_key, branch_id), ...],
        "endpoints": [(endpoint_key, branch_id, endpoint_id), ...],
        "catalogs":  [(catalog_key, catalog_id), ...],
      }
    """
    resources = config.get("resources", {})
    projects = resources.get("postgres_projects", {})
    if not projects:
        raise SystemExit("No postgres_projects defined in the bundle — nothing to provision.")

    branches = resources.get("postgres_branches", {})
    endpoints = resources.get("postgres_endpoints", {})
    catalogs = resources.get("postgres_catalogs", {})

    items = []
    for proj_key, proj in projects.items():
        project_id = resolve_vars(str(proj["project_id"]), config)
        # pg_version from the bundle (int like 16/17/18). create-database-instance takes
        # the workspace DEFAULT otherwise, which can differ from what the bundle declares —
        # and pg_version is IMMUTABLE, so a mismatch means a destroy/recreate later. Pass it.
        pg_version = proj.get("pg_version")

        # branches whose parent references THIS project
        my_branches = []
        for bkey, b in branches.items():
            if _project_key_for_ref(str(b.get("parent", ""))) == proj_key:
                my_branches.append((bkey, str(b["branch_id"])))

        # endpoints whose parent references one of THIS project's branches
        my_branch_keys = {bkey for bkey, _ in my_branches}
        my_endpoints = []
        for ekey, ep in endpoints.items():
            parent = str(ep.get("parent", ""))
            bkey = _branch_key_for_ref(parent)
            if bkey in my_branch_keys:
                # map branch key -> branch_id
                bid = next((bid for k, bid in my_branches if k == bkey), None)
                if bid:
                    my_endpoints.append((ekey, bid, str(ep.get("endpoint_id", "primary"))))

        # catalogs whose branch references THIS project. Also collect the distinct
        # Postgres databases this project exposes (needed for per-database data grants).
        my_catalogs = []
        my_databases: list[str] = []
        for ckey, c in catalogs.items():
            if _project_key_for_ref(str(c.get("branch", ""))) == proj_key:
                cat_id = resolve_vars(str(c["catalog_id"]), config)
                my_catalogs.append((ckey, cat_id))
                db = c.get("postgres_database")
                if db and db not in my_databases:
                    my_databases.append(str(db))

        # permissions block on THIS project -> data-plane grant targets. Map the DABs ACL
        # principal fields to (identity, identity_type):
        #   service_principal_name -> SERVICE_PRINCIPAL (identity = app id UUID)
        #   user_name              -> USER              (identity = email)
        #   group_name             -> GROUP             (identity = group display name)
        my_principals: list[tuple[str, str]] = []
        for perm in (proj.get("permissions") or []):
            if perm.get("service_principal_name"):
                my_principals.append((str(perm["service_principal_name"]), "SERVICE_PRINCIPAL"))
            elif perm.get("user_name"):
                my_principals.append((str(perm["user_name"]), "USER"))
            elif perm.get("group_name"):
                my_principals.append((str(perm["group_name"]), "GROUP"))

        items.append({
            "proj_key": proj_key,
            "project_id": project_id,
            "pg_version": pg_version,
            "branches": my_branches,
            "endpoints": my_endpoints,
            "catalogs": my_catalogs,
            "databases": my_databases,
            "principals": my_principals,
        })
    return items


# --------------------------------------------------------------------------- #
# Step 1 — existence check + legacy-provisioned create
# --------------------------------------------------------------------------- #
def instance_exists(cli: str, name: str, profile: str | None) -> bool:
    """
    Check existence on the NEW postgres/projects API (`postgres get-project`), NOT the
    legacy `database get-database-instance`. Instances created/bound via the autoscaling
    (projects) surface — including any restored via undelete — are only visible here;
    the legacy API returns "not found" for them, which would wrongly trigger a re-create.
    """
    cmd = [cli, "postgres", "get-project", f"projects/{name}"]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    if proc.returncode == 0:
        try:
            d = json.loads(proc.stdout)
            # delete_time present => soft-deleted; treat as NOT existing (slug still reserved,
            # but there is no usable live project to adopt — surface it clearly).
            if d.get("delete_time"):
                log(f"Project '{name}' is SOFT-DELETED (purge_time={d.get('purge_time')}). "
                    f"Slug is reserved — cannot create until it purges or is --purge'd.")
                return False
            log(f"Project '{name}' exists (pg_version={d.get('status',{}).get('pg_version')}).")
        except json.JSONDecodeError:
            log(f"Project '{name}' exists.")
        return True
    log(f"Project '{name}' not found — will create it the legacy provisioned way.")
    return False


def create_legacy_instance(
    cli: str, name: str, capacity: str, profile: str | None,
    pg_version: int | str | None = None, dry_run: bool = False,
) -> None:
    # IMPORTANT — we create via the LEGACY `database create-database-instance` on purpose:
    # this is the provisioned path that unlocks DUAL NETWORKING (the whole reason for this
    # pre-hook). That path is pinned to Postgres 16 — it does NOT honor a requested
    # pg_version (verified: passing PG_VERSION_17 still yields 16). Version choice (16/17/18)
    # is only available via the NEW `postgres create-project` API (what the UI uses), but
    # that path is not confirmed to give dual networking, so we don't use it here.
    # Net: instances created by this hook are pg16 by constraint until SDPL replaces the
    # whole dual-networking cutover (at which point this pre-hook is removed). The bundle
    # may still declare pg_version: 17 — DABs tolerates the mismatch on a bound instance
    # (no destroy/recreate), and it's correct for a future version-flexible create path.
    # `pg_version` is accepted here only for signature stability; it is intentionally NOT
    # passed to the legacy create.
    cmd = [cli, "database", "create-database-instance", name, "--capacity", capacity]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would create instance '{name}' (capacity={capacity}, pg16 — legacy/dual-networking path): $ "
            + " ".join(cmd))
        return
    run(cmd)  # CLI waits for AVAILABLE by default
    log(f"Instance '{name}' created and AVAILABLE (capacity={capacity}, pg16 — legacy/dual-networking path).")


def set_autoscale_window(
    cli: str, project_id: str, branch_id: str, endpoint_id: str,
    min_cu: float, max_cu: float, profile: str | None,
    suspend_timeout: str | None = None, dry_run: bool = False,
) -> None:
    """
    Set an endpoint's autoscaling window AND scale-to-zero suspend timeout via the
    native CLI (no token-mint/curl), in a single update-endpoint call. DABs can't set
    these on an auto-created primary, so we do it post-deploy.

    Autoscaling constraints: min >= 0.5, max <= 64, (max - min) <= 16.
    suspend_timeout: scale-to-zero idle timer, e.g. "300s" (5 min) .. "604800s" (7 days).
      NOT on by default in this environment — pass it to enable scale-to-zero.
      Omit (None) to leave the endpoint's current suspend setting untouched.
    """
    name = f"projects/{project_id}/branches/{branch_id}/endpoints/{endpoint_id}"
    s2z = f", scale-to-zero after {suspend_timeout}" if suspend_timeout else ""
    if dry_run:
        log(f"DRY-RUN would set autoscale on {name}: {min_cu} -> {max_cu} CU{s2z}.")
        return

    # Two SEPARATE update-endpoint calls: the autoscaling window and the suspend timeout
    # use DIFFERENT update_mask paths and can't be combined. The suspend field is set via
    # mask `spec.suspension` with body {"spec":{"suspend_timeout_duration":"<n>s"}} —
    # NOT `spec.suspend_timeout_duration` (that path is rejected by the update_mask).
    def _update(mask: str, spec: dict, label: str) -> bool:
        cmd = [cli, "postgres", "update-endpoint", name, mask, "--json", json.dumps({"spec": spec})]
        if profile:
            cmd += ["-p", profile]
        proc = run(cmd, check=False, capture=True)
        print(proc.stdout, end="")
        if proc.returncode != 0:
            log(f"WARNING: could not set {label} on {name} (exit {proc.returncode}). "
                f"Set it manually or re-run --phase post.")
            return False
        return True

    ok_cu = _update(
        "spec.autoscaling_limit_min_cu,spec.autoscaling_limit_max_cu",
        {"autoscaling_limit_min_cu": min_cu, "autoscaling_limit_max_cu": max_cu},
        "autoscale window",
    )
    ok_s2z = True
    if suspend_timeout:
        ok_s2z = _update("spec.suspension", {"suspend_timeout_duration": suspend_timeout},
                         "scale-to-zero suspend timeout")
    if ok_cu and ok_s2z:
        log(f"Autoscale window set on {name}: {min_cu} -> {max_cu} CU{s2z}.")


def grant_project_admin(cli: str, project_id: str, group: str, profile: str | None, dry_run: bool = False) -> None:
    """
    Grant an account GROUP CAN_MANAGE on a Lakebase project (the closest thing to
    'owner' — Lakebase projects have no owner field, only CAN_USE/CAN_MANAGE ACLs).
    Uses the Permissions API surface: `permissions update database-projects <id>`.
    PATCH/update is additive, so this adds the grant without disturbing existing ACLs.
    """
    acl = {"access_control_list": [{"group_name": group, "permission_level": "CAN_MANAGE"}]}
    cmd = [cli, "permissions", "update", "database-projects", project_id, "--json", json.dumps(acl)]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would grant CAN_MANAGE on project '{project_id}' to group '{group}'.")
        return
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"WARNING: could not grant CAN_MANAGE on project '{project_id}' to '{group}' "
            f"(exit {proc.returncode}). Grant it manually or re-run --phase post.")
    else:
        log(f"Granted CAN_MANAGE on project '{project_id}' to group '{group}'.")


def grant_catalog_privileges(cli: str, catalog_id: str, group: str, profile: str | None, dry_run: bool = False) -> None:
    """
    Grant an account GROUP full control on a UC catalog WITHOUT transferring ownership
    (the SP/creator stays owner). We add both ALL_PRIVILEGES and MANAGE because
    ALL_PRIVILEGES intentionally EXCLUDES MANAGE (and EXTERNAL USE *). MANAGE is what
    lets the group grant/revoke to others — so ALL_PRIVILEGES + MANAGE ≈ owner-level
    control minus the owner role itself. `grants update catalog <id> --json {changes:[...]}`.
    Catalog must exist (created by `bundle deploy`, which runs before this post phase).
    """
    changes = {"changes": [{"principal": group, "add": ["ALL_PRIVILEGES", "MANAGE"]}]}
    cmd = [cli, "grants", "update", "catalog", catalog_id, "--json", json.dumps(changes)]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would grant ALL_PRIVILEGES + MANAGE on catalog '{catalog_id}' to group '{group}' (owner unchanged).")
        return
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"WARNING: could not grant privileges on catalog '{catalog_id}' to '{group}' "
            f"(exit {proc.returncode}). Grant it manually or re-run --phase post.")
    else:
        log(f"Granted ALL_PRIVILEGES + MANAGE on catalog '{catalog_id}' to group '{group}'.")


def grant_catalog_read(cli: str, catalog_id: str, principal: str, profile: str | None, dry_run: bool = False) -> None:
    """
    Grant a principal (e.g. a developer) USE_CATALOG + SELECT on a UC catalog — read +
    view tables, no manage/write-DDL. Used for the dev developer grant (dev target only).
    """
    changes = {"changes": [{"principal": principal, "add": ["USE_CATALOG", "SELECT"]}]}
    cmd = [cli, "grants", "update", "catalog", catalog_id, "--json", json.dumps(changes)]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would grant USE_CATALOG + SELECT on catalog '{catalog_id}' to '{principal}'.")
        return
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"WARNING: could not grant read on catalog '{catalog_id}' to '{principal}' "
            f"(exit {proc.returncode}). Grant it manually or re-run --phase post.")
    else:
        log(f"Granted USE_CATALOG + SELECT on catalog '{catalog_id}' to '{principal}'.")


def isolate_catalog_to_workspace(cli: str, catalog_id: str, workspace_id: str, profile: str | None, dry_run: bool = False) -> None:
    """
    Restrict a UC catalog to ONE workspace: bind the target workspace, then flip the
    catalog's isolation to ISOLATED. A catalog defaults to OPEN — accessible from EVERY
    workspace on the metastore. dev + prod share a metastore here, so an OPEN dev catalog
    is reachable from the prod workspace and vice-versa; ISOLATED + a single binding pins
    each env's catalog to just its own workspace.
    Order matters: bind FIRST, then isolate — so the (deploying) workspace is never left
    without access during the switch. If the bind fails we skip the isolate to avoid
    orphaning the catalog (ISOLATED with no bindings = reachable from nowhere).
    Catalog must exist (created by `bundle deploy`, which runs before this post phase).
    """
    bind = {"add": [{"workspace_id": int(workspace_id), "binding_type": "BINDING_TYPE_READ_WRITE"}]}
    bind_cmd = [cli, "workspace-bindings", "update-bindings", "catalog", catalog_id, "--json", json.dumps(bind)]
    iso_cmd = [cli, "catalogs", "update", catalog_id, "--isolation-mode", "ISOLATED"]
    if profile:
        bind_cmd += ["-p", profile]
        iso_cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would bind catalog '{catalog_id}' to workspace {workspace_id} (READ_WRITE), then set isolation ISOLATED.")
        return
    proc = run(bind_cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"WARNING: could not bind catalog '{catalog_id}' to workspace {workspace_id} "
            f"(exit {proc.returncode}); leaving isolation unchanged to avoid orphaning it.")
        return
    proc = run(iso_cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"WARNING: bound workspace {workspace_id} but could not set catalog '{catalog_id}' "
            f"to ISOLATED (exit {proc.returncode}). Set it manually or re-run --phase post.")
    else:
        log(f"Isolated catalog '{catalog_id}' to workspace {workspace_id} only (READ_WRITE binding).")


# Data-plane privilege policy: what each identity TYPE gets on the Postgres tables.
#   SERVICE_PRINCIPAL = runtime writer -> read+write
#   USER / GROUP      = human inspector -> read-only
# (Central so the policy lives in one place; change here to re-tune.)
def privileges_for(identity_type: str) -> str:
    return "SELECT,INSERT,UPDATE,DELETE" if identity_type == "SERVICE_PRINCIPAL" else "SELECT"


def current_identity(cli: str, profile: str | None) -> str:
    """
    Return the authenticated caller's identity (for an SP this is its application id).
    Used to SKIP data-plane grants for the deployer: the pre-hook runs AS the SP that
    created the instance, which is the project OWNER and already a databricks_superuser
    member — so granting it data access is redundant (and create_role on it can error).
    """
    cmd = [cli, "current-user", "me", "-o", "json"]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    try:
        return str(json.loads(proc.stdout).get("userName", "")).strip()
    except json.JSONDecodeError:
        return ""


def grant_data_role(
    cli: str, target: str, project_id: str, database: str,
    identity: str, identity_type: str, profile: str | None, dry_run: bool = False,
) -> bool:
    """
    Run the shared `setup_data_role` bundle job to grant ONE identity direct-connection
    Postgres access (role + table DML) on ONE database. This is DATA-PLANE work (psycopg
    over OAuth), so it must run as a bundle job in the workspace — the CLI/pre-hook cannot
    execute Postgres SQL itself. Privileges are derived from the identity type.

    Returns True on success, False on failure. Unlike the (best-effort) autoscale/admin
    grants, data-plane grant failures are treated as FATAL by the caller: a silently
    dropped grant means an identity has no access, which must not pass as green.
    """
    # NOTE: `bundle run --params` is COMMA-separated key=value pairs, so a value cannot
    # itself contain commas (e.g. "SELECT,INSERT,..." would be parsed as extra keys and
    # rejected: "INSERT must be formatted as key=value"). Join privileges with '+' here;
    # the notebook splits on either '+' or ','.
    privs = privileges_for(identity_type).replace(",", "+")
    params = (f"project_id={project_id},database={database},identity={identity},"
              f"identity_type={identity_type},privileges={privs}")
    cmd = [cli, "bundle", "run", "setup_data_role", "-t", target, "--params", params]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would grant {identity_type} '{identity}' [{privs}] on "
            f"{project_id}/{database} via setup_data_role.")
        return True
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"ERROR: setup_data_role failed for {identity_type} '{identity}' on "
            f"{project_id}/{database} (exit {proc.returncode}).")
        return False
    log(f"Granted {identity_type} '{identity}' [{privs}] on {project_id}/{database}.")
    return True


def _pg_cli_json(cli: str, args: list[str], profile: str | None) -> tuple[int, dict | list | None]:
    """Run a `databricks postgres ...` CLI subcommand and parse its JSON output."""
    cmd = [cli, "postgres", *args, "-o", "json"]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return proc.returncode, None


def grant_admin_superuser(
    cli: str, project_id: str, admin_group: str, profile: str | None, dry_run: bool = False,
) -> bool:
    """
    Grant the admin GROUP `databricks_superuser` via the CONTROL-PLANE role API — NOT raw
    SQL. `databricks_superuser` is NOLOGIN and no customer-connectable identity holds ADMIN
    OPTION on it, so `GRANT databricks_superuser TO ...` from a psql session (even as the
    owner) fails with InsufficientPrivilege. The postgres role API grants membership using
    an internally-privileged role, authorized by the SP's PROJECT ownership.

    Two steps: ensure the group's OAuth role exists (create-role; no-op if present), then
    update-role with membership_roles=["DATABRICKS_SUPERUSER"]. NOTE: membership_roles
    REPLACES the list (not merge) — we send the full intended set. This is a
    project/branch-level grant (not per-database). Returns True on success.
    """
    parent = f"projects/{project_id}/branches/production"
    if dry_run:
        log(f"DRY-RUN would grant databricks_superuser to group '{admin_group}' on "
            f"{project_id} via the postgres role API (create-role + update-role).")
        return True

    # Step 1: ensure the GROUP OAuth role exists. Reuse a deterministic role-id so we can
    # address it; "already exists" is a benign no-op (the SQL path or a prior run made it).
    role_id = "grp-" + re.sub(r"[^a-z0-9-]", "-", admin_group.lower())
    create_args = ["create-role", parent, "--role-id", role_id, "--json",
                   json.dumps({"spec": {"identity_type": "GROUP", "postgres_role": admin_group,
                                        "auth_method": "LAKEBASE_OAUTH_V1"}})]
    rc, _ = _pg_cli_json(cli, create_args, profile)
    # rc != 0 with "already exists" is fine; find the actual role resource by postgres_role
    # (its name may be this role_id OR a rol-xxxx from an earlier SQL create).
    rc_list, roles = _pg_cli_json(cli, ["list-roles", parent], profile)
    role_name = None
    for r in (roles or []):
        rn = r.get("name", "")
        rc_g, detail = _pg_cli_json(cli, ["get-role", rn], profile)
        if detail and detail.get("status", {}).get("postgres_role") == admin_group:
            role_name = rn
            break
    if not role_name:
        log(f"ERROR: could not locate the postgres role for group '{admin_group}' after create.")
        return False

    # Step 2: grant DATABRICKS_SUPERUSER membership AND set the role ATTRIBUTES. Role
    # attributes (createdb/createrole/bypassrls) are NOT inherited through membership in
    # Postgres — they must be set directly on the role. So the static SH admin group needs
    # both: superuser membership (for pg_read_all_data/pg_write_all_data etc.) + these
    # attributes (to actually CREATE DATABASE / CREATE ROLE / bypass RLS). One update-role
    # call with a combined mask. membership_roles REPLACES the list (send the full set).
    rc_u, updated = _pg_cli_json(
        cli, ["update-role", role_name, "spec.membership_roles,spec.attributes",
              "--json", json.dumps({"spec": {
                  "membership_roles": ["DATABRICKS_SUPERUSER"],
                  "attributes": {"createdb": True, "createrole": True, "bypassrls": True},
              }})],
        profile)
    if rc_u != 0 or not updated:
        log(f"ERROR: update-role failed granting databricks_superuser + attributes to '{admin_group}' on {project_id}.")
        return False
    log(f"Granted databricks_superuser + createdb/createrole/bypassrls to group '{admin_group}' on {project_id} (via role API).")
    return True


def grant_dataapi_role(
    cli: str, target: str, project_id: str, database: str,
    identity: str, profile: str | None, dry_run: bool = False,
) -> bool:
    """
    Run `setup_dataapi_role` to wire ONE service principal into the Data API path:
    GRANT <sp> TO authenticator + table/sequence DML across all schemas. The SP id is
    passed directly (from the permissions block) — no secret scope. PREREQ: "Enable Data
    API" must be clicked on the instance first (creates the `authenticator` role).
    Returns True on success. Only meaningful for SERVICE_PRINCIPAL identities.
    """
    params = f"project_id={project_id},database={database},identity={identity}"
    cmd = [cli, "bundle", "run", "setup_dataapi_role", "-t", target, "--params", params]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would wire SP '{identity}' into Data API (authenticator) on "
            f"{project_id}/{database} via setup_dataapi_role.")
        return True
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode != 0:
        log(f"ERROR: setup_dataapi_role failed for SP '{identity}' on {project_id}/{database} "
            f"(exit {proc.returncode}). Did you click 'Enable Data API' on the instance first?")
        return False
    log(f"Wired SP '{identity}' into Data API (authenticator) on {project_id}/{database}.")
    return True


# --------------------------------------------------------------------------- #
# Step 2 — bind
# --------------------------------------------------------------------------- #
def catalog_exists(cli: str, catalog_id: str, profile: str | None) -> bool:
    """Check whether a Lakebase UC catalog already exists in the workspace."""
    cmd = [cli, "postgres", "get-catalog", catalog_id]
    if profile:
        cmd += ["-p", profile]
    proc = run(cmd, check=False, capture=True)
    return proc.returncode == 0


def bind(cli: str, key: str, resource_path: str, target: str, profile: str | None, dry_run: bool = False) -> None:
    """Bind one bundle resource key to its backend path. Idempotent on 'already bound'."""
    cmd = [cli, "bundle", "deployment", "bind", key, resource_path, "--auto-approve", "-t", target]
    if profile:
        cmd += ["-p", profile]
    if dry_run:
        log(f"DRY-RUN would bind '{key}' -> {resource_path}: $ " + " ".join(cmd))
        return
    proc = run(cmd, check=False, capture=True)
    print(proc.stdout, end="")
    if proc.returncode == 0:
        return
    lowered = (proc.stdout or "").lower()
    if "already" in lowered and "bound" in lowered:
        log(f"'{key}' already bound — no-op.")
        return
    raise SystemExit(f"bind failed for '{key}' -> {resource_path} (exit {proc.returncode})")


def bind_item(cli: str, item: dict, target: str, profile: str | None, dry_run: bool = False) -> None:
    """
    Bind the autoscaling keys for one project, binding ONLY the resource types the
    bundle actually declares:
      • project              -> always
      • branches             -> only if postgres_branches declared for this project
      • endpoints            -> only if postgres_endpoints declared
      • catalogs             -> only if the catalog already EXISTS (restore/rerun case)
    """
    project_id = item["project_id"]

    # project (always)
    bind(cli, item["proj_key"], f"projects/{project_id}", target, profile, dry_run)

    # branches (only if declared)
    for bkey, bid in item["branches"]:
        bind(cli, bkey, f"projects/{project_id}/branches/{bid}", target, profile, dry_run)

    # endpoints (only if declared)
    for ekey, bid, eid in item["endpoints"]:
        bind(cli, ekey, f"projects/{project_id}/branches/{bid}/endpoints/{eid}", target, profile, dry_run)

    # catalogs: bind ONLY if they already exist (avoids CATALOG_ALREADY_EXISTS on
    # deploy after an undelete/restore). On the normal fresh path they don't exist
    # yet, so we skip and let `bundle deploy` create them.
    for ckey, cat_id in item["catalogs"]:
        if catalog_exists(cli, cat_id, profile):
            log(f"Catalog '{cat_id}' already exists — binding it so deploy adopts it.")
            bind(cli, ckey, f"catalogs/{cat_id}", target, profile, dry_run)
        else:
            log(f"Catalog '{cat_id}' does not exist yet — deploy will create it (no bind).")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Lakebase dual-network CI/CD pre-hook (multi-project create-legacy + bind).")
    parser.add_argument("--bundle-dir", default=".", help="Directory containing databricks.yml (default: cwd).")
    parser.add_argument("--target", default="dev", help="Bundle target (default: dev).")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile (omit in CI; use env auth).")
    parser.add_argument("--cli", default="databricks", help="Path to the databricks CLI (default: databricks on PATH).")
    parser.add_argument("--only", default=None,
                        help="Optional: restrict to a single project by project_id or postgres_projects key "
                             "(deploy one value stream at a time). Default: all projects in the bundle.")
    parser.add_argument("--phase", choices=["pre", "post"], default="pre",
                        help="pre  = create-legacy + bind (run BEFORE `bundle deploy`); "
                             "post = set the autoscale CU window (run AFTER `bundle deploy`). Default: pre.")
    parser.add_argument("--set-autoscale", action="store_true",
                        help="(--phase post) OPT-IN: set the endpoint autoscale window + scale-to-zero. "
                             "OFF by default and intentionally NOT run in CI/CD — autoscale is NOT managed "
                             "by DABs, and re-applying it on every deploy would CLOBBER a window a group/"
                             "developer set manually (e.g. bumping CU for a heavy workload). Autoscale is "
                             "the value stream's own operational knob; run this manually only when you "
                             "explicitly want to (re)set it.")
    parser.add_argument("--min-cu", type=float, default=0.5, help="Autoscale min CU (only with --set-autoscale; default 0.5).")
    parser.add_argument("--max-cu", type=float, default=2.0, help="Autoscale max CU (only with --set-autoscale; default 2.0).")
    parser.add_argument("--suspend-timeout", default="300s",
                        help="Scale-to-zero idle timeout (only with --set-autoscale), e.g. '300s' (5m), "
                             "'3600s' (1h), '604800s' (7d max). Pass '' to leave the endpoint's current "
                             "suspend setting untouched. Default 300s.")
    parser.add_argument("--admin-group", default="SH_ENTERPRISE_ADMIN",
                        help="Account GROUP granted CAN_MANAGE on each Lakebase project and "
                             "ALL_PRIVILEGES + MANAGE on each UC catalog (--phase post; owner NOT "
                             "transferred — SP/creator stays owner). Pass '' to skip. "
                             "Default SH_ENTERPRISE_ADMIN.")
    parser.add_argument("--developer", default="",
                        help="Developer identity (email) granted USE_CATALOG + SELECT on each UC "
                             "catalog (--phase post). Intended DEV-ONLY — the workflow passes this "
                             "only when target=dev. Empty = skip. (Postgres data access for devs "
                             "comes from the permissions block via --grant-data-access.)")
    parser.add_argument("--grant-data-access", action="store_true",
                        help="(--phase post) Derive Postgres data-plane grants from each project's "
                             "`permissions:` block and run the setup_data_role job per (principal, "
                             "database): SERVICE_PRINCIPAL -> read+write, USER/GROUP -> read-only. "
                             "Off by default (opt-in), since it opens a Postgres connection per grant.")
    parser.add_argument("--grant-dataapi", action="store_true",
                        help="(--phase post) Wire each project's SERVICE_PRINCIPAL(s) from the "
                             "`permissions:` block into the Data API (GRANT <sp> TO authenticator) "
                             "via setup_dataapi_role. PREREQ: 'Enable Data API' clicked on the "
                             "instance first. Combine with --only <project_id> to do just ONE "
                             "project (the normal path — Enable-Data-API is manual per-instance).")
    parser.add_argument("--isolate-catalogs", action="store_true",
                        help="(--phase post) Pin each project's UC catalog(s) to the CURRENT target's "
                             "workspace: bind that workspace then set isolation ISOLATED. Without this a "
                             "catalog is OPEN (usable from every workspace on the metastore); since dev + "
                             "prod share a metastore, that leaks each env's catalog cross-environment.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover + print the planned actions per project, but do NOT create, bind, "
                             "or update anything. Safe to run first.")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle_dir).resolve()
    if not (bundle_dir / "databricks.yml").exists():
        raise SystemExit(f"No databricks.yml in {bundle_dir}")
    os.chdir(bundle_dir)  # the CLI resolves the bundle from cwd

    log(f"CLI: {args.cli} | target: {args.target} | profile: {args.profile or '(env auth)'} | dir: {bundle_dir}")
    if args.dry_run:
        log("*** DRY-RUN: discovery only — no create / bind / update will be executed ***")
    run([args.cli, "--version"], check=False, capture=True)

    config = load_bundle_config(args.cli, args.target, args.profile)
    items = extract_projects(config)

    # Workspace id for catalog isolation — derived from the resolved target host
    # (adb-<workspace-id>.NN.azuredatabricks.net). Only needed for --isolate-catalogs.
    workspace_id = ""
    if args.isolate_catalogs:
        host = config.get("workspace", {}).get("host", "")
        m = re.search(r"adb-(\d+)\.", host)
        if not m:
            raise SystemExit(
                f"--isolate-catalogs needs the target workspace id, but could not derive it "
                f"from workspace.host '{host}'. Expected an adb-<id>.NN.azuredatabricks.net host."
            )
        workspace_id = m.group(1)
        log(f"Catalog isolation target: workspace {workspace_id} (from {host}).")

    if args.only:
        items = [it for it in items if args.only in (it["project_id"], it["proj_key"])]
        if not items:
            raise SystemExit(f"--only '{args.only}' matched no project_id or postgres_projects key in the bundle.")

    log(f"[phase={args.phase}] Found {len(items)} Lakebase project(s) to process: "
        + ", ".join(it["project_id"] for it in items))

    # ---------------- PHASE: post (autoscale + scale-to-zero + admin grants, AFTER deploy) ----
    if args.phase == "post":
        data_grant_failures: list[str] = []  # data-plane grant failures are FATAL (collected, raised at end)
        # The deployer identity (= instance owner / databricks_superuser) is skipped by the
        # data grants. Resolve it ONCE (a CLI call), only when a grant flag needs it.
        owner = current_identity(args.cli, args.profile) if (args.grant_data_access or args.grant_dataapi) else ""
        for item in items:
            project_id = item["project_id"]
            # Autoscale is OPT-IN (--set-autoscale) and NOT run in CI/CD. It is not managed
            # by DABs, so re-applying it on every deploy would clobber a window a group/
            # developer set manually. Set it only when explicitly asked.
            if args.set_autoscale:
                # Set the window on each declared endpoint; if none declared, default to
                # the production branch's primary (the endpoint the legacy create makes).
                targets = item["endpoints"] or [(None, "production", "primary")]
                for _ekey, bid, eid in targets:
                    set_autoscale_window(
                        args.cli, project_id, bid, eid, args.min_cu, args.max_cu, args.profile,
                        suspend_timeout=(args.suspend_timeout or None), dry_run=args.dry_run,
                    )
            # Admin group: CAN_MANAGE on the Lakebase project (closest to 'owner' — no
            # owner concept there), and ALL_PRIVILEGES + MANAGE on each of the project's
            # UC catalogs WITHOUT transferring ownership (SP/creator stays owner).
            if args.admin_group:
                # Control plane: CAN_MANAGE on the project.
                grant_project_admin(args.cli, project_id, args.admin_group, args.profile, args.dry_run)
                # UC plane: ALL_PRIVILEGES + MANAGE on each catalog.
                for _ckey, cat_id in item["catalogs"]:
                    grant_catalog_privileges(args.cli, cat_id, args.admin_group, args.profile, args.dry_run)
                # Data plane: databricks_superuser for the admin group (static SH admin role
                # gets full Postgres control). Project/branch-level (not per-DB), via the
                # role API. Fatal on failure like the other data grants.
                ok = grant_admin_superuser(args.cli, project_id, args.admin_group,
                                           args.profile, args.dry_run)
                if not ok:
                    data_grant_failures.append(f"superuser: {args.admin_group} on {project_id}")
            # Catalog isolation (opt-in): pin each of the project's UC catalogs to the
            # CURRENT target's workspace only. Independent of who is granted ON the catalog
            # (above) — this controls WHICH workspaces can see/use it at all.
            if args.isolate_catalogs:
                for _ckey, cat_id in item["catalogs"]:
                    isolate_catalog_to_workspace(args.cli, cat_id, workspace_id, args.profile, args.dry_run)
            # Developer (dev only — workflow passes --developer only when target=dev):
            # read + view tables on each catalog. Postgres data access for devs comes from
            # the permissions block via --grant-data-access, not here.
            if args.developer:
                for _ckey, cat_id in item["catalogs"]:
                    grant_catalog_read(args.cli, cat_id, args.developer, args.profile, args.dry_run)
            # Postgres DATA-PLANE grants derived from the project's permissions block
            # (opt-in via --grant-data-access). One setup_data_role run per (principal,
            # database): SP -> read+write, USER/GROUP -> read-only.
            if args.grant_data_access:
                # The deployer (pre-hook's own identity) OWNS the instance and is already
                # a databricks_superuser member — skip it (redundant + create_role errors).
                if not item["principals"]:
                    log(f"No permissions block on '{project_id}' — no data grants to derive.")
                elif not item["databases"]:
                    log(f"No postgres databases discovered for '{project_id}' — skipping data grants.")
                for identity, id_type in item["principals"]:
                    if owner and identity == owner:
                        log(f"Skipping data grant for '{identity}' — it is the deployer/owner "
                            f"(already databricks_superuser).")
                        continue
                    for db in item["databases"]:
                        ok = grant_data_role(args.cli, args.target, project_id, db,
                                             identity, id_type, args.profile, args.dry_run)
                        if not ok:
                            data_grant_failures.append(f"{identity} on {project_id}/{db}")

            # Data API wiring (opt-in): GRANT <sp> TO authenticator for each SERVICE_PRINCIPAL
            # in the permissions block. Use --only <project_id> to target the ONE project whose
            # Data API was just enabled. Only SPs (users don't get wired to authenticator);
            # deployer/owner skipped (its authenticator grant would 42501 anyway).
            if args.grant_dataapi:
                for identity, id_type in item["principals"]:
                    if id_type != "SERVICE_PRINCIPAL":
                        continue
                    if owner and identity == owner:
                        log(f"Skipping Data API wiring for '{identity}' — deployer/owner.")
                        continue
                    for db in item["databases"]:
                        ok = grant_dataapi_role(args.cli, args.target, project_id, db,
                                                identity, args.profile, args.dry_run)
                        if not ok:
                            data_grant_failures.append(f"authenticator: {identity} on {project_id}/{db}")
        # Data-plane grant failures are FATAL: a dropped grant means an identity silently
        # has no access, which must NOT pass as green (this masked a real bug once).
        if data_grant_failures:
            raise SystemExit(
                "ERROR: setup_data_role failed for: " + "; ".join(data_grant_failures)
                + ". Data-plane grants are required — fix the cause and re-run --phase post "
                  "--grant-data-access."
            )
        _did = []
        if args.set_autoscale: _did.append("autoscale")
        if args.admin_group: _did.append("admin grants")
        if args.grant_data_access: _did.append("data-plane grants")
        if args.grant_dataapi: _did.append("data-api wiring")
        log(f"Post-hook complete ({', '.join(_did) if _did else 'nothing requested'}).")
        return 0

    # ---------------- PHASE: pre (create-legacy + bind, BEFORE deploy) ----------
    for item in items:
        log(f"--- project: {item['project_id']} "
            f"(branches={len(item['branches'])}, endpoints={len(item['endpoints'])}, "
            f"catalogs={len(item['catalogs'])}) ---")

        # Create the instance ONLY if it doesn't exist (legacy-provisioned -> unlocks
        # Dual Networking). If it already exists, skip the create — but STILL bind below.
        if instance_exists(args.cli, item["project_id"], args.profile):
            log(f"Instance '{item['project_id']}' already exists — skipping create; ensuring bind.")
        else:
            cap_var = config.get("variables", {}).get("capacity", {})
            capacity = cap_var.get("value") or cap_var.get("default") or "CU_1"
            create_legacy_instance(args.cli, item["project_id"], capacity, args.profile,
                                   item.get("pg_version"), args.dry_run)

        # ALWAYS bind (idempotent — "already bound" is a safe no-op). This is the fix for
        # the "instance exists but is NOT bound in THIS bundle state" case (e.g. created
        # manually, on a different runner, or in a fresh CI state): without binding here,
        # `bundle deploy` would try to CREATE it and fail with "project slug already exists".
        log("Ensuring autoscaling resource keys are bound...")
        bind_item(args.cli, item, args.target, args.profile, args.dry_run)

    log("Pre-hook complete. The pipeline can now run `databricks bundle deploy`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
