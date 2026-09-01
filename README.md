# fabric-mcp

A local MCP server that gives Claude read access to Microsoft Fabric using **your own Entra
identity** — no service principal, no client secret, no stored credentials.

**Start here:** [INSTALL.md](INSTALL.md) to get it running · [USAGE.md](USAGE.md) for the tools.

Two surfaces, one sign-in:

| Surface | Audience | What it gives you |
|---|---|---|
| Fabric REST API | `https://api.fabric.microsoft.com/.default` | workspaces, every item type (notebooks, pipelines, lakehouses, semantic models, …), lakehouse Delta table lists, SQL endpoint connection strings, item definitions |
| SQL analytics endpoint / Warehouse over TDS | `https://database.windows.net/.default` | read-only T-SQL against lakehouse Delta tables and warehouses |

Auth chain: `AzureCliCredential` → `InteractiveBrowserCredential`. Run `az login` once; after
that it is silent. All access is bounded by your Fabric RBAC, and every operation is in Fabric's
audit log under your name.

## Prerequisites

1. Python 3.10+ on PATH
2. [ODBC Driver 18 for SQL Server](https://aka.ms/downloadmsodbcsql) (64-bit)
3. Azure CLI (`az`) — recommended; without it you get a browser popup on first token
4. Claude Desktop

## Install

```powershell
cd C:\Users\<you>\Claude\Projects\fabric-mcp
.\setup.ps1 -RegisterClaude
```

That checks prerequisites, builds `.venv`, installs dependencies, signs you in, runs a smoke
test (`fabric_whoami`), and writes a `fabric` entry into the config file Claude Desktop
actually reads (backing up the existing file first).

On an MSIX/Store install — which is what `Get-AppxPackage -Name Claude` reports here — that
file is **not** `%APPDATA%\Claude\claude_desktop_config.json`. The app reads a package-virtualized
copy at `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`. Writing the
`%APPDATA%` path is a silent no-op: the server never appears and Claude shows no error. `setup.ps1`
resolves this automatically and refuses to run while Claude Desktop is up. See
`Claude Desktop — MSIX shadows the config file you edit` in the vault.

Then **fully quit Claude Desktop from the tray and reopen it** — closing the window is not enough.

Run `.\setup.ps1` without `-RegisterClaude` to see the config snippet instead of writing it.

### Several tenants, several identities

Register one instance per identity and select it with `-Subscription <subscription-guid>`:

```powershell
.\setup.ps1 -RegisterClaude -Name fabric-clienta -Subscription <subscription-guid-a>
.\setup.ps1 -RegisterClaude -Name fabric-clientb -Subscription <subscription-guid-b>
```

`az login` once per identity first; `az account list --all` should show them all.
Each instance is its own process, so both work at the same time regardless of
which account `az` currently has selected.

**Use `-Subscription`, not `-TenantId`.** They are not two ways to do the same
thing, and `az account get-access-token` refuses both together:

| selector | what it does |
|---|---|
| `-Subscription` | picks **which stored `az` login** to use; token comes from that account in its home tenant |
| `-TenantId` | keeps the **currently active** account and asks it for a token in another tenant — `AADSTS90072` unless that user is a guest there |

`-TenantId` is for a genuine guest/B2B case only. Subscription names are no good as
selectors — `Azure Plan` and `Azure subscription 1` collide across tenants — so pass
the GUID.

Always confirm with `fabric_whoami`, which reports the UPN and tenant from the token's
own claims and names the selector in use. Do not trust the instance name.

## Tools

| Tool | Purpose |
|---|---|
| `fabric_whoami` | Auth/driver check. Run this first. |
| `fabric_list_workspaces` | Every workspace you can see. |
| `fabric_list_items` | All objects in a workspace, optionally filtered by type. Includes a `count_by_type` summary. |
| `fabric_list_tables` | Delta tables in a lakehouse, via the Fabric Tables REST API (name, Managed/External, format, OneLake location). |
| `fabric_sql_endpoint` | Resolve server + database for a lakehouse or warehouse. |
| `fabric_sql_catalog` | Tables and views **as the SQL engine sees them**, with schema names and column counts. |
| `fabric_describe_table` | Columns, types, precision, nullability for one table. |
| `fabric_query` | Read-only T-SQL by workspace + item name. |
| `fabric_query_raw` | Read-only T-SQL against an explicit server + database. |
| `fabric_get_item_definition` | An item's definition — notebook code, pipeline JSON. Handles the 202 long-running path. |

Workspaces and items are addressable by **display name or GUID**. Ambiguous names raise an
error listing the candidate GUIDs rather than silently picking one.

## Read-only guarantee

There is no write path in the code. Beyond that, `_assert_read_only` requires the statement to
begin with `SELECT` or `WITH` after comment stripping, and rejects `INSERT`, `UPDATE`, `DELETE`,
`MERGE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `REVOKE`, `DENY`, `BACKUP`, `RESTORE`,
`EXEC`/`EXECUTE`, `sp_*`, `xp_*`, `OPENROWSET`, `OPENDATASOURCE`, and `BULK` anywhere in the
statement. It is deliberately blunt: a legitimate query with `DELETE` inside a string literal
will be refused.

`fabric_get_item_definition` reads definitions; it never writes them.

## Performance

Resolving a workspace by **display name** costs a full `/workspaces` listing. Measured on
this tenant that was ~35s of a 38s call, against ~1.2s to open a TDS connection and ~2s to
run the metadata query itself — the SQL side was never the problem.

Two mitigations are built in: one shared `httpx` client so connections are reused across
requests and pages (a client per request meant a TLS handshake per request), and a
`FABRIC_MCP_RESOLVE_TTL` cache on name→GUID lookups. A name that misses the cache triggers
one uncached re-check, so a newly created workspace or item is never invisible for the
length of the TTL.

Passing GUIDs instead of display names skips the workspace listing entirely and remains the
fastest path.

## Two things that will bite you

**Only Delta tables appear on a lakehouse SQL analytics endpoint.** Parquet, CSV and anything
else under `Files/` is invisible to T-SQL. If `fabric_list_tables` shows a table that
`fabric_sql_catalog` does not, that is why — or the endpoint's metadata sync has not caught up.

**The lakehouse SQL analytics endpoint is read-only and does not support the full warehouse
T-SQL surface.** Some warehouse-only constructs will fail there.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `FABRIC_MCP_MAX_ROWS` | `2000` | Default row cap per query. |
| `FABRIC_MCP_SQL_TIMEOUT` | `120` | SQL connection/query timeout, seconds. |
| `FABRIC_MCP_TENANT_ID` | unset | Cross-tenant (guest) request for the active az account. Not an identity selector — see above. |
| `FABRIC_MCP_AZ_SUBSCRIPTION` | unset | Subscription GUID selecting **which stored `az` login** to use. This is the identity selector. |
| `FABRIC_MCP_INSTANCE` | `default` | Label reported by `fabric_whoami`. |
| `FABRIC_MCP_RESOLVE_TTL` | `300` | Seconds to cache name→GUID lookups. `0` disables. Data listings are never cached. |

## Troubleshooting

**`fabric_whoami` reports a token failure** — run `az login` (add `--tenant <guid>` if you are
in several tenants), then `az account show` to confirm.

**`Login failed for user '<token-identified principal>'` (SQL 18456)** — the token is valid but
that identity has no access to the item. Check your workspace role, and that the tenant setting
allowing SQL endpoint access is on.

**`No suitable ODBC driver found`** — install ODBC Driver 18, 64-bit, then restart Claude Desktop.

**Server does not appear in Claude** — quit Claude Desktop from the tray (not just the window),
reopen, and check the MCP log under `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs\`
on an MSIX install (`%APPDATA%\Claude\logs\` only on a non-packaged one). A server that crashes on import
simply never registers, and Claude shows no error — so if the tools are missing, run
`.\.venv\Scripts\python.exe fabric_mcp_server.py` directly and read the traceback. It should
sit silently waiting for stdio input; anything else is the bug.

**`No module named 'mcp.server.fastmcp'`** — you are on MCP SDK 2.x, where `FastMCP` was renamed
to `MCPServer`. The server handles both via a dual import; if you see this error, your
`fabric_mcp_server.py` predates that fix.

**A table is missing from `fabric_sql_catalog`** — either it is not Delta, or the SQL endpoint's
metadata sync is lagging. Refresh it from the Fabric portal, or via the Refresh SQL endpoint
metadata REST API.

## Companion: Fabric Core MCP Server (Microsoft-hosted)

For object management without running any code, Microsoft ships a remote MCP server at
`https://api.fabric.microsoft.com/v1/mcp/core` (preview) — browser OAuth, no secrets. It covers
catalog search, workspaces, items, permissions, folders and capacities, but **not** data
queries: "Operations that modify data within items (such as lakehouse tables or notebook code)
require direct Fabric access."

The two are complementary: Core MCP for the object graph and CRUD, this server for reading
actual data out of the lakehouse.
