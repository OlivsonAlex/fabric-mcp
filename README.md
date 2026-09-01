# fabric-mcp

Two MCP servers that let Claude read Microsoft Fabric **as you**, under your own Entra
identity. No service principal, no client secret, nothing stored.

One is ours and reads lakehouse data. The other is Microsoft's and reads semantic models.
This repo holds the first, and the registration and documentation for the second.

## Which server answers which question

| Question | Server |
|---|---|
| What workspaces and objects exist? | **fabric-mcp** |
| What does the lakehouse look like to SQL? | **fabric-mcp** |
| How many rows landed? What is the newest date? | **fabric-mcp** |
| What does this notebook or pipeline do? | **fabric-mcp** |
| What measures exist, and what is their DAX? | **Power BI Modeling MCP** |
| What does this measure return? Why is it slow? | **Power BI Modeling MCP** |
| What differs between dev and prod? | **Power BI Modeling MCP**, two connections at once |

Rule of thumb for DirectLake models: when a number looks wrong, first decide whether the
fault is in the **data** or the **DAX**. Querying the lakehouse through fabric-mcp answers
the first half faster than going through the model.

## Documentation

| File | For |
|---|---|
| [INSTALL.md](INSTALL.md) | installing fabric-mcp, per-client identities, troubleshooting, env vars |
| [USAGE.md](USAGE.md) | the ten fabric-mcp tools, read-only guarantee, worked examples |
| [POWERBI-MCP.md](POWERBI-MCP.md) | installing and using Microsoft's Power BI Modeling MCP |
| [AGENT-INSTALL.md](AGENT-INSTALL.md) | a runbook written for Claude, so it can do the install |

Scripts: `setup.ps1` and `diagnose.ps1` for fabric-mcp, `Register-PowerBIMcp.ps1` for the
Power BI server.

---

## fabric-mcp — lakehouse data, read-only

Two surfaces, one sign-in:

| Surface | Audience | What it gives you |
|---|---|---|
| Fabric REST API | `https://api.fabric.microsoft.com/.default` | workspaces, every item type, lakehouse Delta table lists, SQL endpoint connection strings, item definitions |
| SQL analytics endpoint / Warehouse over TDS | `https://database.windows.net/.default` | read-only T-SQL against lakehouse Delta tables and warehouses |

Auth chain: `AzureCliCredential` → `InteractiveBrowserCredential`. Run `az login` once; after
that it is silent. All access is bounded by your Fabric RBAC, and every operation appears in
Fabric's audit log under your name.

Prerequisites: Python 3.10+, [ODBC Driver 18 for SQL Server](https://aka.ms/downloadmsodbcsql)
64-bit, Azure CLI, Claude Desktop.

```powershell
git clone https://github.com/OlivsonAlex/fabric-mcp.git
cd fabric-mcp
.\setup.ps1                                    # dry run: checks, venv, smoke test
.\setup.ps1 -Auto                              # list your az logins and what to run
# quit Claude Desktop from the tray, then:
.\setup.ps1 -RegisterClaude -Name fabric-<client> -Subscription <subscription-guid>
```

Ten tools: `fabric_whoami`, `fabric_list_workspaces`, `fabric_list_items`,
`fabric_list_tables`, `fabric_sql_endpoint`, `fabric_sql_catalog`, `fabric_describe_table`,
`fabric_query`, `fabric_query_raw`, `fabric_get_item_definition`. Workspaces and items are
addressable by display name or GUID; ambiguous names raise an error listing the candidates
rather than silently picking one. See [USAGE.md](USAGE.md).

**Identity is pinned in config, one instance per client.** Select it with `-Subscription`
(the subscription GUID), never `-TenantId` — the latter keeps whichever `az` account is
active and asks it for another tenant's token, failing with `AADSTS90072`. Details and the
full comparison in [INSTALL.md](INSTALL.md).

**Read-only by construction.** No write path exists in the code, and `_assert_read_only`
additionally requires every statement to begin with `SELECT` or `WITH` and rejects DML, DDL
and `EXEC` anywhere in it. Deliberately blunt: a `SELECT` with the word `DELETE` inside a
string literal is refused.

---

## Power BI Modeling MCP — semantic models

Microsoft's server: [microsoft/powerbi-modeling-mcp](https://github.com/microsoft/powerbi-modeling-mcp).
Reaches the model itself over XMLA, covering measures, tables, columns, relationships,
calculation groups, roles, and DAX execution.

```powershell
# quit Claude Desktop from the tray first
.\Register-PowerBIMcp.ps1 -DryRun
.\Register-PowerBIMcp.ps1
```

Registers two entries, split by capability, because **the server defaults to read-write**:

| Entry | Flag | Use |
|---|---|---|
| `powerbi-mcp-server` | `--readonly` | asking questions, reading DAX, validating |
| `powerbi-mcp-write` | `--readwrite` | deliberate model edits |

Know the limit: `--readonly` does **not** remove the write operations from the tool schema
the server advertises. The refusal happens at runtime, and it has not been verified here.
Treat the split as a speed bump, not a capability boundary.

**Connections are runtime state, not config.** There is no way to pin a model in advance,
and every Claude Desktop restart drops them. Full behaviour, including the credential model
and the 60-second timeout trap, in [POWERBI-MCP.md](POWERBI-MCP.md).

---

## The config trap, common to both

Claude Desktop on Windows ships as an MSIX package, so `claude_desktop_config.json` exists
in two places:

| | |
|---|---|
| What the app reads and writes | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| What "Edit Config" and every blog post point at | `%APPDATA%\Claude\claude_desktop_config.json` |

Editing `%APPDATA%` is a silent no-op: the server never appears and Claude reports no error.
Every script here resolves the correct path, refuses to run while Claude Desktop is alive,
and writes a timestamped backup. Do not hand-edit either file, and do not bridge them with a
junction — that converts a harmless shadow copy into real data loss.

Restarting means **quit from the system tray**, not closing the window. Config is read only
at process start.

## Companion: Fabric Core MCP (Microsoft-hosted, remote)

For object management without running code, Microsoft ships a remote server at
`https://api.fabric.microsoft.com/v1/mcp/core` (preview). It covers catalog search,
workspaces, items, permissions, folders and capacities, but not data queries.

Two things to weigh before adding it: it is **not read-only** — it can create, update and
delete workspaces and items and change permissions — and it reports no identity, so you
cannot check which account it is acting as. Neither is true of the two servers above.
