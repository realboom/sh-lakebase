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

        # catalogs whose branch references THIS project
        my_catalogs = []
        for ckey, c in catalogs.items():
            if _project_key_for_ref(str(c.get("branch", ""))) == proj_key:
                cat_id = resolve_vars(str(c["catalog_id"]), config)
                my_catalogs.append((ckey, cat_id))

        items.append({
            "proj_key": proj_key,
            "project_id": project_id,
            "pg_version": pg_version,
            "branches": my_branches,
            "endpoints": my_endpoints,
            "catalogs": my_catalogs,
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
    parser.add_argument("--min-cu", type=float, default=0.5, help="Autoscale min CU for --phase post (default 0.5).")
    parser.add_argument("--max-cu", type=float, default=2.0, help="Autoscale max CU for --phase post (default 2.0).")
    parser.add_argument("--suspend-timeout", default="300s",
                        help="Scale-to-zero idle timeout for --phase post, e.g. '300s' (5m), '3600s' (1h), "
                             "'604800s' (7d max). Enables scale-to-zero (NOT on by default here). "
                             "Pass '' to leave the endpoint's current suspend setting untouched. Default 300s.")
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

    if args.only:
        items = [it for it in items if args.only in (it["project_id"], it["proj_key"])]
        if not items:
            raise SystemExit(f"--only '{args.only}' matched no project_id or postgres_projects key in the bundle.")

    log(f"[phase={args.phase}] Found {len(items)} Lakebase project(s) to process: "
        + ", ".join(it["project_id"] for it in items))

    # ---------------- PHASE: post (set autoscale window, AFTER deploy) ----------
    if args.phase == "post":
        for item in items:
            project_id = item["project_id"]
            # Set the window on each declared endpoint; if none declared, default to
            # the production branch's primary (the endpoint the legacy create makes).
            targets = item["endpoints"] or [(None, "production", "primary")]
            for _ekey, bid, eid in targets:
                set_autoscale_window(
                    args.cli, project_id, bid, eid, args.min_cu, args.max_cu, args.profile,
                    suspend_timeout=(args.suspend_timeout or None), dry_run=args.dry_run,
                )
        log("Post-hook complete (autoscale windows + scale-to-zero set).")
        return 0

    # ---------------- PHASE: pre (create-legacy + bind, BEFORE deploy) ----------
    for item in items:
        log(f"--- project: {item['project_id']} "
            f"(branches={len(item['branches'])}, endpoints={len(item['endpoints'])}, "
            f"catalogs={len(item['catalogs'])}) ---")

        # The pre-hook is a ONE-TIME cutover per instance. If the instance already
        # exists it is created + bound (DAB-managed) — nothing to do; deploy reconciles.
        if instance_exists(args.cli, item["project_id"], args.profile):
            log(f"Instance '{item['project_id']}' already exists — bound & DAB-managed. Pre-hook no-op.")
            continue

        # First-time cutover: create legacy-provisioned (unlocks Dual Networking), then bind.
        cap_var = config.get("variables", {}).get("capacity", {})
        capacity = cap_var.get("value") or cap_var.get("default") or "CU_1"
        create_legacy_instance(args.cli, item["project_id"], capacity, args.profile,
                               item.get("pg_version"), args.dry_run)

        log("Binding autoscaling resource keys to the newly-created instance...")
        bind_item(args.cli, item, args.target, args.profile, args.dry_run)

    log("Pre-hook complete. The pipeline can now run `databricks bundle deploy`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
