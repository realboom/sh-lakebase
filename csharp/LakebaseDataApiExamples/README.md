# Lakebase Data API — C# examples

A C# (.NET 8) console port of [`src/dataapi_examples.py`](../../src/dataapi_examples.py).
It talks to the Lakebase **Data API (PostgREST)** over plain HTTPS — no Postgres
driver, no port 5432 — which is exactly what a .NET client at Select Health /
Intermountain would use, given the [in-workspace / no-5432 constraints](../../README.md).

It reproduces, with only the BCL (`System.Net.Http` + `System.Text.Json`), what
the Databricks SDK does for you in the notebook:

1. **OAuth (M2M):** mint a workspace token as the service principal via
   `POST /oidc/v1/token` (client-credentials, `scope=all-apis`).
2. **Resolve endpoint host + workspace id** (unless overridden) via
   `list-endpoints` and the SCIM `/Me` `X-Databricks-Org-Id` header — so it
   survives FEVM rebuilds with no config edits.
3. **Mint a Postgres credential:** exchange the OAuth token at
   `POST /api/2.0/postgres/credentials` for the short-lived bearer the Data API
   accepts.
4. **Run the six examples:** filtered SELECT, filter+sort, exact count, offset
   pagination, keyset pagination, and an INSERT.

## Prerequisites

- **.NET 8 SDK** — https://dotnet.microsoft.com/download/dotnet/8.0
  (`global.json` pins the build to the 8.0 line). Verify with `dotnet --version`.
- **Outbound HTTPS** to the workspace host and the Lakebase endpoint host.
- **NuGet access** (nuget.org) is only needed if you enable the MSAL auth path,
  which adds one package. The default path is BCL-only — no restore required.

## Configuration (environment variables)

The app reads process **environment variables** — it does not parse a `.env`
file automatically. Copy `.env.example`, fill it in, and either export the values
(`set -a; . ./.env; set +a` on bash/zsh) or set them in your CI/runtime secret
store. Never commit a filled-in `.env`.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DATABRICKS_HOST` | ✅ | — | Workspace URL, e.g. `https://adb-XXXXXXXXXXXXXXXX.NN.azuredatabricks.net` |
| `DATABRICKS_CLIENT_ID` | ✅ | — | Service-principal OAuth client id (M2M) |
| `DATABRICKS_CLIENT_SECRET` | ✅ | — | Service-principal OAuth client secret |
| `LAKEBASE_PROJECT` | | `sh-lakebase` | |
| `LAKEBASE_BRANCH` | | `production` | |
| `LAKEBASE_ENDPOINT` | | `primary` | |
| `LAKEBASE_DATABASE` | | `apitest` | |
| `LAKEBASE_ENDPOINT_HOST` | | *(derived)* | e.g. `ep-XXXXXXXX.database.REGION.azuredatabricks.net` |
| `LAKEBASE_WORKSPACE_ID` | | *(derived)* | e.g. `XXXXXXXXXXXXXXXX` |

The service principal must already have its Postgres role granted (see
`sql/dataapi_role_setup.sql`), or the Data API returns `PGRST301
invalid token permissions`.

## Run

```bash
export DATABRICKS_HOST="https://adb-XXXXXXXXXXXXXXXX.NN.azuredatabricks.net"
export DATABRICKS_CLIENT_ID="<sp-client-id>"
export DATABRICKS_CLIENT_SECRET="<sp-client-secret>"

dotnet run --project csharp/LakebaseDataApiExamples
```

Read the SP creds from the Databricks secret scope instead of pasting them:

```bash
p=central-lakebase   # profile whose host is this workspace
export DATABRICKS_CLIENT_ID=$(databricks secrets get-secret sh-lakebase sp_client_id \
  --profile $p -o json | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["value"]).decode())')
export DATABRICKS_CLIENT_SECRET=$(databricks secrets get-secret sh-lakebase sp_client_secret \
  --profile $p -o json | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["value"]).decode())')
```

## Alternative auth: Azure Entra ID (MSAL), secret from CyberArk

The default path mints a Databricks OAuth token from a Databricks-managed service
principal. Select Health / Intermountain (an Azure shop) will use an **Entra ID
service principal** instead, with its secret held in **CyberArk**. A
ready-to-uncomment `GetEntraTokenViaMsalAsync` lives in `LakebaseDataApiClient.cs`:
it acquires a token for the Azure Databricks resource
(`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default`) via **MSAL**. Azure Databricks
accepts that Entra token as the bearer directly, so it's a drop-in replacement for
`GetOAuthTokenAsync` — the rest of the flow is unchanged.

The SP secret is sourced through a `RetrieveClientSecretFromCyberArkAsync` stub —
plug your existing CyberArk retrieval (CCP REST / AIM / Conjur) in there; it just
returns the secret string. To enable: uncomment the block, add the one NuGet package
it needs (`Microsoft.Identity.Client`), set `AZURE_TENANT_ID`, and call it from
`InitializeAsync`. A certificate variant (no stored secret at all) is noted inline.
This aligns with the Entra-SP direction in the repo's DABs setup.

## Files

- `Program.cs` — entry point and the six examples.
- `LakebaseDataApiClient.cs` — auth, credential minting, and PostgREST GET/POST helpers.
- `LakebaseConfig.cs` — environment-variable config.
