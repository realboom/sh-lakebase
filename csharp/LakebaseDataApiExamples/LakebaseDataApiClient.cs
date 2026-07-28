using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace LakebaseDataApiExamples;

/// <summary>
/// The auth + transport half of the Lakebase Data API examples. It reproduces
/// what the Databricks SDK does under the hood in the Python notebook:
///
///   1. Mint a workspace OAuth token as the service principal (M2M
///      client-credentials against /oidc/v1/token).
///   2. Resolve the Lakebase endpoint host (list-endpoints) and the workspace id
///      (SCIM /Me header) unless supplied — the FEVM-proof bit.
///   3. Exchange the OAuth token for a short-lived Postgres credential
///      (POST /api/2.0/postgres/credentials) — this is the bearer the PostgREST
///      Data API actually accepts.
///   4. Expose thin GET/POST helpers against the Data API base URL.
/// </summary>
public sealed class LakebaseDataApiClient : IDisposable
{
    private readonly LakebaseConfig _cfg;
    private readonly HttpClient _http = new();

    private string _databaseToken = "";

    public LakebaseDataApiClient(LakebaseConfig cfg) => _cfg = cfg;

    /// <summary>Resolved Data API base, e.g. https://&lt;host&gt;/api/2.0/workspace/&lt;id&gt;/rest/apitest</summary>
    public string RestBase { get; private set; } = "";

    /// <summary>
    /// Run steps 1-3 so the client is ready to issue Data API calls. Prints the
    /// same "REST base:" line the notebook does.
    /// </summary>
    public async Task InitializeAsync()
    {
        var oauthToken = await GetOAuthTokenAsync();

        var endpointHost = _cfg.EndpointHost ?? await ResolveEndpointHostAsync(oauthToken);
        var workspaceId = _cfg.WorkspaceId ?? await ResolveWorkspaceIdAsync(oauthToken);

        _databaseToken = await MintDatabaseCredentialAsync(oauthToken, workspaceId);

        RestBase = $"https://{endpointHost}/api/2.0/workspace/{workspaceId}/rest/{_cfg.Database}";
        Console.WriteLine($"REST base: {RestBase}");
    }

    // ----- Step 1: workspace OAuth token (service principal, M2M) -----------

    private async Task<string> GetOAuthTokenAsync()
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{_cfg.WorkspaceHost}/oidc/v1/token");
        var basic = Convert.ToBase64String(Encoding.UTF8.GetBytes($"{_cfg.ClientId}:{_cfg.ClientSecret}"));
        req.Headers.Authorization = new AuthenticationHeaderValue("Basic", basic);
        req.Content = new FormUrlEncodedContent(new Dictionary<string, string>
        {
            ["grant_type"] = "client_credentials",
            ["scope"] = "all-apis",
        });

        using var resp = await _http.SendAsync(req);
        var body = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException($"OAuth token request failed ({(int)resp.StatusCode}): {body}");

        using var doc = JsonDocument.Parse(body);
        return doc.RootElement.GetProperty("access_token").GetString()
               ?? throw new InvalidOperationException("OAuth response had no access_token.");
    }

    // ======================================================================
    // ALTERNATIVE (Azure): authenticate an Entra ID service principal via
    // MSAL, with the client secret sourced from CyberArk.
    //
    // This is the likely production path at Select Health / Intermountain (an
    // Azure shop using an Entra ID SP rather than a Databricks-managed SP, with
    // secrets held in CyberArk). Azure Databricks accepts an Entra access token
    // as the bearer directly, so this simply REPLACES GetOAuthTokenAsync above:
    // wire it into InitializeAsync as
    //     var oauthToken = await GetEntraTokenViaMsalAsync();
    // and the rest of the flow (resolve host/id, mint Postgres credential,
    // PostgREST calls) is unchanged.
    //
    // Requires one NuGet package (add to the .csproj):
    //   <PackageReference Include="Microsoft.Identity.Client" Version="4.*" />
    // and config: AZURE_TENANT_ID. The SP secret comes from your existing
    // CyberArk integration (see the stub below) — or use a certificate and skip
    // the secret entirely (see .WithCertificate).
    // ======================================================================
    //
    // using Microsoft.Identity.Client;
    //
    // // The fixed programmatic id of the "AzureDatabricks" first-party app.
    // // Requesting "<id>/.default" yields a token carrying the SP's app roles.
    // private const string AzureDatabricksResourceId = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d";
    //
    // private async Task<string> GetEntraTokenViaMsalAsync()
    // {
    //     // 1) Fetch the SP's client secret from CyberArk. You already have this
    //     //    working — drop your call in RetrieveClientSecretFromCyberArkAsync
    //     //    below. Skip this line if you authenticate with a certificate.
    //     string clientSecret = await RetrieveClientSecretFromCyberArkAsync();
    //
    //     // 2) Acquire an Entra token for the Azure Databricks resource (M2M).
    //     var app = ConfidentialClientApplicationBuilder
    //         .Create(_cfg.ClientId)                       // Entra app (client) id
    //         .WithClientSecret(clientSecret)              // OR .WithCertificate(x509cert)
    //         .WithAuthority($"https://login.microsoftonline.com/{_cfg.TenantId}")
    //         .Build();
    //
    //     AuthenticationResult result = await app
    //         .AcquireTokenForClient(new[] { $"{AzureDatabricksResourceId}/.default" })
    //         .ExecuteAsync();
    //
    //     return result.AccessToken;   // use as the Databricks bearer, same as GetOAuthTokenAsync
    // }
    //
    // // Hand-off point for your existing CyberArk integration (CCP REST / AIM /
    // // Conjur). Return the SP client secret; nothing else in the flow changes.
    // private Task<string> RetrieveClientSecretFromCyberArkAsync() =>
    //     throw new NotImplementedException("Wire in your CyberArk secret retrieval here.");
    //
    // // Certificate variant (no stored secret at all — the SP has the public cert
    // // registered in Entra; load the cert however you already manage it):
    // //   var cert = new X509Certificate2(...);
    // //   ... .WithCertificate(cert) ...

    // ----- Step 2a: resolve the Lakebase Data API host ----------------------

    private async Task<string> ResolveEndpointHostAsync(string oauthToken)
    {
        var url = $"{_cfg.WorkspaceHost}/api/2.0/postgres/{_cfg.BranchResource}/endpoints";
        using var doc = await GetJsonAsync(url, oauthToken);

        if (!doc.RootElement.TryGetProperty("endpoints", out var endpoints) ||
            endpoints.GetArrayLength() == 0)
            throw new InvalidOperationException(
                $"No endpoints found for {_cfg.BranchResource}. Set LAKEBASE_ENDPOINT_HOST to override.");

        var host = endpoints[0]
            .GetProperty("status")
            .GetProperty("hosts")
            .GetProperty("host")
            .GetString();

        return host ?? throw new InvalidOperationException("Endpoint host was null in list-endpoints response.");
    }

    // ----- Step 2b: resolve the workspace id (FEVM-proof) -------------------

    private async Task<string> ResolveWorkspaceIdAsync(string oauthToken)
    {
        // The org id rides on a response header of the SCIM Me call, exactly like
        // the SDK's get_workspace_id(). The body itself is ignored.
        using var req = new HttpRequestMessage(HttpMethod.Get, $"{_cfg.WorkspaceHost}/api/2.0/preview/scim/v2/Me");
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", oauthToken);

        using var resp = await _http.SendAsync(req);
        if (!resp.IsSuccessStatusCode)
        {
            var body = await resp.Content.ReadAsStringAsync();
            throw new HttpRequestException($"SCIM /Me failed ({(int)resp.StatusCode}): {body}");
        }

        if (resp.Headers.TryGetValues("X-Databricks-Org-Id", out var values))
            return values.First();

        throw new InvalidOperationException(
            "X-Databricks-Org-Id header missing; set LAKEBASE_WORKSPACE_ID to override.");
    }

    // ----- Step 3: exchange OAuth token for a Postgres credential -----------

    private async Task<string> MintDatabaseCredentialAsync(string oauthToken, string workspaceId)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{_cfg.WorkspaceHost}/api/2.0/postgres/credentials");
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", oauthToken);
        req.Headers.Add("X-Databricks-Org-Id", workspaceId);
        var payload = JsonSerializer.Serialize(new { endpoint = _cfg.EndpointResource });
        req.Content = new StringContent(payload, Encoding.UTF8, "application/json");

        using var resp = await _http.SendAsync(req);
        var body = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException($"generate-credential failed ({(int)resp.StatusCode}): {body}");

        using var doc = JsonDocument.Parse(body);
        return doc.RootElement.GetProperty("token").GetString()
               ?? throw new InvalidOperationException("Credential response had no token.");
    }

    // ----- PostgREST Data API helpers ---------------------------------------

    /// <summary>GET a PostgREST resource. <paramref name="path"/> is relative to the DB, e.g. "public/epic_hospital_census".</summary>
    public async Task<HttpResponseMessage> GetAsync(
        string path,
        IReadOnlyDictionary<string, string> query,
        IReadOnlyDictionary<string, string>? extraHeaders = null)
    {
        var req = new HttpRequestMessage(HttpMethod.Get, BuildUri(path, query));
        ApplyDataApiHeaders(req, extraHeaders);
        return await _http.SendAsync(req);
    }

    /// <summary>POST a JSON body to a PostgREST resource.</summary>
    public async Task<HttpResponseMessage> PostAsync(
        string path,
        object body,
        IReadOnlyDictionary<string, string>? extraHeaders = null)
    {
        var req = new HttpRequestMessage(HttpMethod.Post, BuildUri(path, null));
        ApplyDataApiHeaders(req, extraHeaders);
        req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        return await _http.SendAsync(req);
    }

    private void ApplyDataApiHeaders(HttpRequestMessage req, IReadOnlyDictionary<string, string>? extraHeaders)
    {
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _databaseToken);
        if (extraHeaders is null) return;
        foreach (var (k, v) in extraHeaders)
        {
            // Prefer is a request header; Content-Type belongs on the content and
            // is set by StringContent, so skip it here to avoid a runtime throw.
            if (string.Equals(k, "Content-Type", StringComparison.OrdinalIgnoreCase)) continue;
            req.Headers.TryAddWithoutValidation(k, v);
        }
    }

    private string BuildUri(string path, IReadOnlyDictionary<string, string>? query)
    {
        var uri = $"{RestBase}/{path}";
        if (query is null || query.Count == 0) return uri;
        var qs = string.Join("&", query.Select(kv =>
            $"{Uri.EscapeDataString(kv.Key)}={Uri.EscapeDataString(kv.Value)}"));
        return $"{uri}?{qs}";
    }

    private async Task<JsonDocument> GetJsonAsync(string url, string bearer)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        using var resp = await _http.SendAsync(req);
        var body = await resp.Content.ReadAsStringAsync();
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException($"GET {url} failed ({(int)resp.StatusCode}): {body}");
        return JsonDocument.Parse(body);
    }

    public void Dispose() => _http.Dispose();
}
