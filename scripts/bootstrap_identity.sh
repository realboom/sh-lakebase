#!/usr/bin/env bash
# Bootstrap the workspace-level identity this bundle needs.
#
# FEVM rebuilds the workspace ~every 2 weeks with brand-new (empty) identity, so this
# script is RE-RUNNABLE: run it once against each freshly minted workspace, BEFORE
# `databricks bundle deploy`. It is idempotent — re-running reuses anything that already
# exists instead of erroring.
#
# It creates ONE service principal (used for both the prod `run_as` AND the Data API),
# two groups (admins -> CAN_MANAGE, operators -> CAN_RUN), and the secret scope the
# Data API notebook reads. At the end it prints the values to paste into databricks.yml.
#
# Usage:
#   databricks auth login --host https://<new-workspace-host>   # or set DATABRICKS_CONFIG_PROFILE
#   ./scripts/bootstrap_identity.sh
#
# Requires: databricks CLI (authenticated to the TARGET workspace) and jq.

set -euo pipefail

SP_NAME="${SP_NAME:-lakebase-deploy}"
ADMIN_GROUP="${ADMIN_GROUP:-lakebase-admins}"
OPERATOR_GROUP="${OPERATOR_GROUP:-lakebase-operators}"
SECRET_SCOPE="${SECRET_SCOPE:-sh-lakebase}"

command -v jq >/dev/null || { echo "jq is required"; exit 1; }

# Run all CLI calls OUTSIDE the bundle dir. Inside the repo the CLI auto-loads
# databricks.yml and rejects a profile whose host != the default target's host. Pick the
# target workspace explicitly via DATABRICKS_CONFIG_PROFILE=<profile> (recommended) or
# DATABRICKS_HOST=<host> before running — your default profile is likely NOT the lakebase
# workspace, and bootstrap provisions whatever the CLI auth points to.
cd "${TMPDIR:-/tmp}"

# `databricks auth env` is empty when auth comes from a named profile (DATABRICKS_CONFIG_PROFILE),
# and `grep` returning no match exits 1 -> under `set -euo pipefail` that silently kills the
# script here (before the banner). Make it non-fatal and fall back to the CLI's resolved host.
WS_HOST=$(databricks auth env 2>/dev/null | grep -oE 'https://[^"]+' | head -1 || true)
[[ -z "$WS_HOST" ]] && WS_HOST=$(databricks current-user me -o json 2>/dev/null | jq -r '._links.self // empty' 2>/dev/null || true)
[[ -z "$WS_HOST" ]] && WS_HOST="${DATABRICKS_HOST:-(profile: ${DATABRICKS_CONFIG_PROFILE:-default})}"
echo "============================================================"
echo " Bootstrapping workspace: ${WS_HOST:-UNKNOWN — set DATABRICKS_CONFIG_PROFILE/HOST}"
echo " as user:                 $(databricks current-user me -o json | jq -r '.userName')"
echo " Ctrl-C NOW if that is not the workspace you intend to set up."
echo "============================================================"

# --- Service principal --------------------------------------------------------
SP_NUM_ID=$(databricks service-principals list -o json \
  | jq -r --arg n "$SP_NAME" '.[] | select(.displayName==$n) | .id' | head -n1)
if [[ -z "$SP_NUM_ID" ]]; then
  echo "Creating service principal '$SP_NAME'..."
  SP=$(databricks service-principals create --json "{\"displayName\":\"$SP_NAME\",\"active\":true}")
  SP_NUM_ID=$(echo "$SP" | jq -r '.id')
else
  echo "Service principal '$SP_NAME' already exists (id=$SP_NUM_ID)."
fi
SP_APP_ID=$(databricks service-principals get "$SP_NUM_ID" -o json | jq -r '.applicationId')

# Mint a fresh OAuth secret (you can't read an existing one back — always create a new one).
echo "Minting OAuth secret for the SP..."
SP_SECRET=$(databricks service-principal-secrets-proxy create "$SP_NUM_ID" -o json | jq -r '.secret')

# --- Groups -------------------------------------------------------------------
ensure_group() {  # $1 = display name -> echoes group id
  local name="$1" gid
  gid=$(databricks groups list -o json | jq -r --arg n "$name" '.[] | select(.displayName==$n) | .id' | head -n1)
  if [[ -z "$gid" ]]; then
    gid=$(databricks groups create --json "{\"displayName\":\"$name\"}" | jq -r '.id')
    echo "  created group '$name' (id=$gid)" >&2
  else
    echo "  group '$name' already exists (id=$gid)" >&2
  fi
  echo "$gid"
}
echo "Ensuring groups..."
ADMIN_ID=$(ensure_group "$ADMIN_GROUP")
OPERATOR_ID=$(ensure_group "$OPERATOR_GROUP")

# Add the SP to the admin group so it can deploy/manage (no-op if already a member).
echo "Adding SP to '$ADMIN_GROUP'..."
databricks groups patch "$ADMIN_ID" --json "{\"schemas\":[\"urn:ietf:params:scim:api:messages:2.0:PatchOp\"],\"Operations\":[{\"op\":\"add\",\"path\":\"members\",\"value\":[{\"value\":\"$SP_NUM_ID\"}]}]}" >/dev/null || true

# --- Secret scope (for the Data API notebook) ---------------------------------
if ! databricks secrets list-scopes -o json | jq -e --arg s "$SECRET_SCOPE" '.[]? | select(.name==$s)' >/dev/null; then
  echo "Creating secret scope '$SECRET_SCOPE'..."
  databricks secrets create-scope "$SECRET_SCOPE"
fi
echo "Storing SP creds in scope '$SECRET_SCOPE'..."
databricks secrets put-secret "$SECRET_SCOPE" sp_client_id     --string-value "$SP_APP_ID"
databricks secrets put-secret "$SECRET_SCOPE" sp_client_secret --string-value "$SP_SECRET"

# --- Summary ------------------------------------------------------------------
cat <<EOF

============================================================
 Bootstrap complete. Put these into databricks.yml variables:
============================================================
  prod_deploy_sp:      $SP_APP_ID
  prod_admin_group:    $ADMIN_GROUP
  prod_operator_group: $OPERATOR_GROUP

Next:
  1. databricks bundle deploy -t <target>
  2. databricks bundle run setup_schema -t <target>
  3. Enable the Data API in the UI (Compute -> Database instances -> Data API -> Enable)
  4. Apply Postgres role grants: psql ... -f sql/dataapi_role_setup.sql
     (the SP's Postgres role; see README "One-time setup")
============================================================
EOF
