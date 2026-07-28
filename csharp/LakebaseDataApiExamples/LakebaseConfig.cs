namespace LakebaseDataApiExamples;

/// <summary>
/// All the knobs the examples need, read from environment variables so nothing
/// (least of all the service-principal secret) is ever hardcoded — the same
/// posture as the Python notebook, which reads creds from a Databricks secret
/// scope. Blank <see cref="EndpointHost"/> / <see cref="WorkspaceId"/> are
/// auto-derived at runtime, which keeps this FEVM-proof: those two values change
/// every time the workspace is rebuilt.
/// </summary>
public sealed record LakebaseConfig
{
    /// <summary>Workspace URL, e.g. https://adb-XXXXXXXXXXXXXXXX.NN.azuredatabricks.net</summary>
    public required string WorkspaceHost { get; init; }

    /// <summary>Service-principal OAuth client id (M2M).</summary>
    public required string ClientId { get; init; }

    /// <summary>Service-principal OAuth client secret (M2M).</summary>
    public required string ClientSecret { get; init; }

    public string Project { get; init; } = "sh-lakebase";
    public string Branch { get; init; } = "production";
    public string Endpoint { get; init; } = "primary";
    public string Database { get; init; } = "apitest";

    /// <summary>Lakebase Data API host. Blank => derived via list-endpoints.</summary>
    public string? EndpointHost { get; init; }

    /// <summary>Workspace id for the REST path. Blank => derived via SCIM /Me.</summary>
    public string? WorkspaceId { get; init; }

    // --- Optional: only used by the commented-out Azure Entra ID / MSAL auth
    //     path in LakebaseDataApiClient. Harmless when unset.
    /// <summary>Entra ID (Azure AD) tenant id for MSAL.</summary>
    public string? TenantId { get; init; }

    /// <summary>The branch resource path used by the Postgres APIs.</summary>
    public string BranchResource => $"projects/{Project}/branches/{Branch}";

    /// <summary>The endpoint resource path passed to generate-credential.</summary>
    public string EndpointResource => $"{BranchResource}/endpoints/{Endpoint}";

    /// <summary>
    /// Build config from environment variables. Throws with a readable message
    /// listing anything required that is missing.
    /// </summary>
    public static LakebaseConfig FromEnvironment()
    {
        static string? Env(string name)
        {
            var v = Environment.GetEnvironmentVariable(name);
            return string.IsNullOrWhiteSpace(v) ? null : v.Trim();
        }

        var host = Env("DATABRICKS_HOST");
        var clientId = Env("DATABRICKS_CLIENT_ID");
        var clientSecret = Env("DATABRICKS_CLIENT_SECRET");

        var missing = new List<string>();
        if (host is null) missing.Add("DATABRICKS_HOST");
        if (clientId is null) missing.Add("DATABRICKS_CLIENT_ID");
        if (clientSecret is null) missing.Add("DATABRICKS_CLIENT_SECRET");
        if (missing.Count > 0)
            throw new InvalidOperationException(
                "Missing required environment variable(s): " + string.Join(", ", missing) +
                ". See README.md for the full list.");

        // Trim a trailing slash so we can concatenate paths cleanly.
        host = host!.TrimEnd('/');

        return new LakebaseConfig
        {
            WorkspaceHost = host,
            ClientId = clientId!,
            ClientSecret = clientSecret!,
            Project = Env("LAKEBASE_PROJECT") ?? "sh-lakebase",
            Branch = Env("LAKEBASE_BRANCH") ?? "production",
            Endpoint = Env("LAKEBASE_ENDPOINT") ?? "primary",
            Database = Env("LAKEBASE_DATABASE") ?? "apitest",
            EndpointHost = Env("LAKEBASE_ENDPOINT_HOST"),
            WorkspaceId = Env("LAKEBASE_WORKSPACE_ID"),
            TenantId = Env("AZURE_TENANT_ID"),
        };
    }
}
