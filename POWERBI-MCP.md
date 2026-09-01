# Power BI Modeling MCP — install and use

The companion to [fabric-mcp](README.md). Where fabric-mcp reads **lakehouse data**
over T-SQL, this server reads and edits the **semantic model** over XMLA: measures,
tables, relationships, roles, and DAX.

This is Microsoft's server, not ours: [microsoft/powerbi-modeling-mcp](https://github.com/microsoft/powerbi-modeling-mcp).
We only wrap its registration.

---

## Which server answers which question

| Question | Server |
|---|---|
| What objects exist in a workspace? | fabric-mcp `fabric_list_items` |
| What does the lakehouse look like to SQL? | fabric-mcp `fabric_sql_catalog` |
| How many rows landed? What's the max date? | fabric-mcp `fabric_query` |
| What does this notebook or pipeline do? | fabric-mcp `fabric_get_item_definition` |
| What measures exist and what's their DAX? | Power BI MCP `measure_operations` |
| What does this measure return? Why is it slow? | Power BI MCP `dax_query_operations` |
| What changed between dev and prod? | Power BI MCP, two connections at once |

Rule of thumb for DirectLake models: when a number looks wrong, decide first whether the
fault is in the **data** or the **DAX**. Querying the lakehouse through fabric-mcp answers
the first half faster than going through the model.

---

## Install

Two routes. The npm route needs no download and is the easier one for a new machine.

### npm (recommended)

Needs Node.js on PATH. Nothing to download by hand.

```powershell
# quit Claude Desktop from the tray first
.\Register-PowerBIMcp.ps1 -DryRun -UseNpx
.\Register-PowerBIMcp.ps1 -UseNpx
# reopen Claude Desktop
```

The script pins a version rather than using `@latest`. The package is published as a
**pre-release** (`0.5.0-beta.13` at the time of writing) and its CLI has changed between
builds, so pinning is what keeps a teammate's behaviour the same as yours. Bump it
deliberately with `-PackageVersion`.

### Existing executable

If you already extracted the VSIX, or the marketplace build is preferred:

```powershell
.\Register-PowerBIMcp.ps1 -ExePath "C:\MCPServers\PowerBIModelingMCP\extension\server\powerbi-modeling-mcp.exe"
```

Run with no switches at all and the script auto-detects: it reuses the exe already
registered, falls back to the conventional path
`C:\MCPServers\PowerBIModelingMCP\extension\server\`, and only then tries npx.

`Register-PowerBIMcp.ps1` resolves the MSIX-virtualized config path, refuses to run while
Claude Desktop is alive, and writes a timestamped backup. The same rules as
[INSTALL.md](INSTALL.md) apply: never edit `%APPDATA%\Claude` by hand.

## Two entries, split by capability

The script registers both:

| Entry | Flag | Use |
|---|---|---|
| `powerbi-mcp-server` | `--readonly` | asking questions, reading DAX, validating |
| `powerbi-mcp-write` | `--readwrite` | deliberate model edits |

**The server defaults to read-write.** Launched with only `--start`, an entry named like a
reader can create, update and delete measures, tables, relationships and security roles on
*published* models. Splitting it means writing to a client's model requires choosing a
different tool, not just phrasing a request differently.

Know the limit: **`--readonly` does not remove the write operations from the tool schema the
server advertises.** The refusal happens at runtime. This has not been verified against a
live model here, and it should not be verified against a client's production model. If you
want it proven, use `ConnectFolder` against a local PBIP folder and attempt an edit there.

The two entries hold **separate** connections, so switching to the writer means connecting
again in that process. That friction is the point.

Flag spelling: use `--readonly` and `--readwrite`. Both the VSIX and npm builds accept
these. The hyphenated `--read-only` / `--read-write` exist on some builds only.

---

## Connecting to a model

There is **no way to pin a model in config**. Not in the entry, not in a CLI flag. Every
connection is a runtime call, and every Claude Desktop restart drops all of them.

Ask in plain language and name the workspace, because model display names repeat across
workspaces:

> Connect to the Profitability Dashboard in the analytics-dev workspace and call it prof-dev

Under that, three steps happen:

1. `connection_operations ConnectFabric` with `workspaceName` and `semanticModelName`,
   matched **exactly**, including non-Latin names
2. `RenameConnection` to something short, because auto-generated names are URL-encoded
   (`Fabric-My%20Workspace-My%20Model`)
3. every later call takes `connectionName`, or falls back to last-used

### What to expect the first time

**An Entra account picker appears** for a tenant you have not used for XMLA on this
machine. This is a **separate credential path from the `az` logins** fabric-mcp uses, so
signing in there does not help here. Pick the identity that has a workspace role in the
target tenant.

**The tool call may time out at 60 seconds** while it waits for you. The bridge gives up;
**the server does not.** The connect completes once you finish signing in. Retrying
produces a duplicate connection. After any timeout, run `ListConnections` before retrying.

**Credentials are held per connection, not per process.** Verified: after a
`clearCredential: true` connect against a second tenant, the first tenant's connection kept
answering. So one instance serves several clients at once, and you do **not** need one
instance per identity the way fabric-mcp does.

Pass `clearCredential: false` to reuse a cached sign-in; `true` forces the picker, which is
how you connect as a different identity.

### After a restart

> Reconnect prof-prod and prof-dev

Sign-ins stay cached, so this is a few seconds with no browser prompt.

---

## Reading a model without drowning in output

`measure_operations List` returns `name`, `description` and `displayFolder` only. No DAX.
That is deliberate: a 214-measure model costs roughly 87 KB *without* expressions.

`List` also **truncates at `maxResults`** (default 200) and reports the real total only
inside a `warnings` array. A model with 214 measures returns 200 and a warning that is easy
to skim past. Read the warnings.

For actual DAX, go per measure with `Get`, or `ExportTMDL` which takes
`maxReturnCharacters` (default 10000) — the size control that fabric-mcp's
`fabric_get_item_definition` lacks.

**Description quality decides the strategy.** A model where every measure documents its
aggregation and grain can be queried from metadata alone, quickly. A model without
descriptions needs a DAX pull per measure: slower, heavier, same answers. Worth knowing
which kind you are pointed at before promising someone a fast answer.

---

## Coverage

Beyond measures: tables, columns, relationships, calculation groups, perspectives,
cultures, object translations, partitions, hierarchies, security roles, named expressions,
query groups, traces, and transactions. `dax_query_operations` covers `Execute`,
`Validate` (no execution) and `ClearCache`, with optional execution metrics for
performance work.

Connection targets: `ConnectFabric` for a published Fabric model, `Connect` for Power BI
Desktop or Analysis Services via a connection string, `ConnectFolder` for a local PBIP
`.SemanticModel\definition` folder.

## Notes on provenance

Microsoft's README documents an `--authmode` flag (`interactive` or `serviceprincipal`)
that is absent from the `--help` of the VSIX build tested here, so builds differ in more
than flag spelling. npm reports the package licence as `Microsoft`; the repository README
has been described as MIT. Check the package you actually install rather than trusting
either summary.
