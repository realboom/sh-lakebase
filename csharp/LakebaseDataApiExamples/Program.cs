using System.Text.Json;
using LakebaseDataApiExamples;

// C# port of ../../src/dataapi_examples.py — the Lakebase Data API (PostgREST)
// runbook driven over plain HTTPS. Config comes from environment variables
// (see README.md); nothing is hardcoded.

LakebaseConfig cfg;
try
{
    cfg = LakebaseConfig.FromEnvironment();
}
catch (InvalidOperationException ex)
{
    Console.Error.WriteLine(ex.Message);
    return 1;
}

using var client = new LakebaseDataApiClient(cfg);
await client.InitializeAsync();

const string census = "public/epic_hospital_census";

// 1) SELECT columns + filter: CensusId IN (...)
Console.WriteLine("\n== 1) SELECT + IN filter ==");
{
    using var resp = await client.GetAsync(census, new Dictionary<string, string>
    {
        ["select"] = "CensusId,FacilityName,LineOfBusiness4",
        ["CensusId"] = "in.(100001,100002,100003)",
    });
    Console.WriteLine($"status {(int)resp.StatusCode}");
    Console.WriteLine(await PrettyBodyAsync(resp));
}

// 2) Filter + sort: TotalCharges > 50000, highest first
Console.WriteLine("\n== 2) filter + order + limit ==");
{
    using var resp = await client.GetAsync(census, new Dictionary<string, string>
    {
        ["select"] = "CensusId,FacilityName,TotalCharges",
        ["TotalCharges"] = "gt.50000",
        ["order"] = "TotalCharges.desc",
        ["limit"] = "5",
    });
    Console.WriteLine($"status {(int)resp.StatusCode}");
    Console.WriteLine(await PrettyBodyAsync(resp));
}

// 3) Total count via the Content-Range header (Prefer: count=exact)
Console.WriteLine("\n== 3) exact count via Content-Range ==");
{
    using var resp = await client.GetAsync(
        census,
        new Dictionary<string, string> { ["select"] = "CensusId", ["limit"] = "1" },
        new Dictionary<string, string> { ["Prefer"] = "count=exact" });
    var contentRange = resp.Content.Headers.TryGetValues("Content-Range", out var cr)
        ? string.Join(",", cr)
        : resp.Headers.TryGetValues("Content-Range", out var cr2) ? string.Join(",", cr2) : null;
    Console.WriteLine($"Content-Range: {contentRange}"); // e.g. 0-0/100
}

// 4) OFFSET/LIMIT pagination LOOP — walk the WHOLE table page by page.
//    Simple and supports random access, but the DB re-scans and skips `offset`
//    rows each request, so cost grows the deeper you page — prefer keyset (#5)
//    for large tables. An ORDER BY is REQUIRED for stable paging.
Console.WriteLine("\n== 4) offset/limit pagination ==");
{
    const int page = 25;
    // Demo cap: this table has ~150k rows, so walk only the first few pages to
    // show the mechanics. Drop maxPages (or set it high) to walk the whole table.
    const int maxPages = 5;
    int offset = 0, pages = 0, total = 0;
    var ids = new List<long>();
    while (true)
    {
        using var resp = await client.GetAsync(census, new Dictionary<string, string>
        {
            ["select"] = "CensusId",
            ["order"] = "CensusId.asc",
            ["limit"] = page.ToString(),
            ["offset"] = offset.ToString(),
        });
        var rows = await ReadRowsAsync(resp); // surfaces PostgREST errors instead of crashing on a dict
        if (rows.Count == 0) break;            // empty page -> past the end, done
        foreach (var row in rows) ids.Add(row.GetProperty("CensusId").GetInt64());
        total += rows.Count;
        pages++;
        Console.WriteLine($"  page {pages}: offset={offset} got {rows.Count} -> " +
                          $"CensusId {rows[0].GetProperty("CensusId").GetInt64()}.." +
                          $"{rows[^1].GetProperty("CensusId").GetInt64()}");
        if (rows.Count < page) break;          // short page -> last page
        if (pages >= maxPages) { Console.WriteLine($"  (demo cap: stopping after {maxPages} pages)"); break; }
        offset += page;
    }
    Console.WriteLine($"offset paging: walked {total} rows in {pages} pages of {page} (unique={ids.Distinct().Count()})");
}

// 5) KEYSET pagination (recommended): page forward by the last CensusId seen —
//    flat cost, no count.
Console.WriteLine("\n== 5) keyset pagination ==");
{
    long last = 0;
    int pages = 0, total = 0;
    const int maxPages = 5; // demo cap (see #4); remove to walk the whole ~150k-row table
    while (true)
    {
        using var resp = await client.GetAsync(census, new Dictionary<string, string>
        {
            ["select"] = "CensusId",
            ["order"] = "CensusId.asc",
            ["CensusId"] = $"gt.{last}",
            ["limit"] = "40",
        });
        var rows = await ReadRowsAsync(resp);
        if (rows.Count == 0) break;
        last = rows[^1].GetProperty("CensusId").GetInt64();
        total += rows.Count;
        pages++;
        if (pages >= maxPages) { Console.WriteLine($"  (demo cap: stopping after {maxPages} pages)"); break; }
    }
    Console.WriteLine($"keyset: walked {total} rows in {pages} pages (last CensusId={last})");
}

// 6) INSERT a row (POST). Prefer: return=representation echoes the inserted row.
Console.WriteLine("\n== 6) INSERT (POST) ==");
{
    using var resp = await client.PostAsync(
        "public/jiva_episode_type",
        new
        {
            AccountClassMap = "Demo",
            PatientClass = "Inpatient",
            BaseClass = "Acute",
            MemberClassCode = "DEMO",
            LengthOfStay = 1,
            JivaEpisodeTypeName = "API Demo Insert",
        },
        new Dictionary<string, string> { ["Prefer"] = "return=representation" });
    Console.WriteLine($"status {(int)resp.StatusCode}");
    Console.WriteLine(await resp.Content.ReadAsStringAsync());
}

return 0;

// ----- local helpers --------------------------------------------------------

static async Task<string> PrettyBodyAsync(HttpResponseMessage resp)
{
    var body = await resp.Content.ReadAsStringAsync();
    try
    {
        using var doc = JsonDocument.Parse(body);
        return JsonSerializer.Serialize(doc.RootElement, new JsonSerializerOptions { WriteIndented = true });
    }
    catch (JsonException)
    {
        return body; // not JSON (e.g. an error page) -> print raw
    }
}

// Parse a PostgREST array response into rows, throwing on a non-2xx so errors
// surface instead of a NullReference deep in the loop (mirrors raise_for_status).
static async Task<List<JsonElement>> ReadRowsAsync(HttpResponseMessage resp)
{
    var body = await resp.Content.ReadAsStringAsync();
    if (!resp.IsSuccessStatusCode)
        throw new HttpRequestException($"Data API returned {(int)resp.StatusCode}: {body}");

    using var doc = JsonDocument.Parse(body);
    if (doc.RootElement.ValueKind != JsonValueKind.Array)
        throw new HttpRequestException($"Expected a JSON array, got: {body}");

    // Clone so the elements stay valid after the JsonDocument is disposed.
    return doc.RootElement.EnumerateArray().Select(e => e.Clone()).ToList();
}
