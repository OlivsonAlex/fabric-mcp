# fabric-mcp — User Manual

Ten tools that let Claude read Microsoft Fabric as you. Nothing here can write.

---

## The mental model

Two surfaces, one sign-in:

| Surface | Token audience | What it gives you |
|---|---|---|
| **Fabric REST API** | `https://api.fabric.microsoft.com/.default` | Workspaces, every item type, lakehouse Delta table lists, SQL endpoint connection strings, item definitions |
| **SQL analytics endpoint / Warehouse over TDS** | `https://database.windows.net/.default` | Read-only T-SQL against lakehouse Delta tables and warehouses |

Some questions are answered from the REST side (*"what notebooks exist?"*), some from the
SQL side (*"how many rows landed yesterday?"*), and some need both. You do not choose the
surface; the tool you pick does.

## Start every session by checking who you are

If your team has more than one instance registered — `fabric-clienta`,
`fabric-clientb` — the tool list looks nearly identical and picking the wrong one means
querying the wrong client under the wrong audited identity.

```
fabric_whoami
```

Read `identity.upn` and `identity.tenant_id`. They come from the token's own claims, not
from configuration. **The instance name is a label someone chose; it is not evidence.**

---

## The tools

### Discovery — what exists?

| Tool | Use it for |
|---|---|
| `fabric_list_workspaces()` | Every workspace you can see: id, name, capacity, type |
| `fabric_list_items(workspace, item_type="")` | Every object in a workspace with a `count_by_type` summary. Filter with e.g. `Notebook`, `DataPipeline`, `Lakehouse`, `Warehouse`, `SemanticModel`, `Report` |
| `fabric_list_tables(workspace, lakehouse)` | Delta tables from the **REST** API: name, Managed/External, format, OneLake location |

### Schema — what does it look like?

| Tool | Use it for |
|---|---|
| `fabric_sql_catalog(workspace, item)` | Tables and views **as the SQL engine sees them**, with schema names and column counts |
| `fabric_describe_table(workspace, item, table, schema="dbo")` | One table: ordinal, name, type, length/precision, nullability |
| `fabric_sql_endpoint(workspace, item)` | Server and database for a lakehouse or warehouse, when you want the connection string itself |

**`fabric_list_tables` vs `fabric_sql_catalog`** — they disagree, on purpose. The first
asks the Fabric API what tables exist in the lakehouse. The second asks the SQL engine
what is queryable. A table in the first but not the second is either **not Delta** or the
endpoint's metadata sync has not caught up. Use `fabric_sql_catalog` before writing SQL.

### Query — what does the data say?

| Tool | Use it for |
|---|---|
| `fabric_query(workspace, item, sql, max_rows=0)` | Read-only T-SQL by workspace and item name. The normal one |
| `fabric_query_raw(server, database, sql, max_rows=0)` | Same guard, explicit server and database. Use when you already have the connection string, or to skip name resolution |

`max_rows=0` means the server default of 2000. Queries time out at 120 s.

### Definitions — what does the code say?

| Tool | Use it for |
|---|---|
| `fabric_get_item_definition(workspace, item, item_type, fmt="")` | An item's definition: notebook code, pipeline JSON. `fmt="ipynb"` for notebooks. Handles the 202 long-running path. Returns base64 parts to decode client-side |

Useful for *"what does the Bronze notebook actually do?"* without opening the portal.

---

## The read-only guarantee

Two independent layers:

1. **There is no write path in the code.** No tool constructs an `INSERT`, `UPDATE`,
   `DELETE` or any DDL. `fabric_get_item_definition` reads definitions; it never writes them.
2. **`_assert_read_only` inspects every statement.** After stripping comments it requires
   the statement to begin with `SELECT` or `WITH`, and rejects `INSERT`, `UPDATE`,
   `DELETE`, `MERGE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `REVOKE`, `DENY`,
   `BACKUP`, `RESTORE`, `EXEC`/`EXECUTE`, `RECONFIGURE`, `SHUTDOWN`, `sp_*`, `xp_*`,
   `OPENROWSET`, `OPENDATASOURCE` and `BULK` **anywhere** in the statement.

The guard is deliberately blunt, and you will meet a false refusal:

```sql
-- refused, even though it is a pure read
SELECT * FROM Silver.audit_log WHERE action = 'DELETE'
```

That is the intended trade: a cheap false refusal beats one write slipping through against
a client's production lakehouse.

When you hit one, **widen the query and filter the results**, e.g. select the rows for the
day and filter on `action` yourself. Do not try to smuggle the literal past the guard with
string concatenation or character codes — it works, and it trains everyone to route around
the one control standing between an agent and a client's warehouse. If the false refusals
become common, the fix is to refine the guard so it ignores string literals, not to evade it.

Note the two layers protect different things. The lakehouse SQL analytics endpoint is
read-only at the service level, so nothing could write through it regardless. **Warehouses
are writable** — there the guard is the only thing standing in the way.

---

## Two things that will bite you

**Only Delta tables appear on a lakehouse SQL analytics endpoint.** Parquet, CSV and
anything else under `Files/` is invisible to T-SQL. If `fabric_list_tables` shows
something `fabric_sql_catalog` does not, that is usually why.

**The lakehouse endpoint does not support the full warehouse T-SQL surface.** Some
warehouse-only constructs fail there. If a query works in a warehouse and not on a
lakehouse endpoint, this is the first thing to check.

---

## Worked examples

Ask in plain language; Claude picks the tools.

**Inventory a workspace**
> What's in the analytics_ws workspace? Break it down by object type.

`fabric_list_items` → `count_by_type` summary plus the full list.

**Understand a model before touching it**
> Show me the Silver schema in the lakehouse, then describe the orders table.

`fabric_sql_catalog` → `fabric_describe_table`.

**Check a load actually landed**
> How many rows are in Gold.load_log, and what's the newest timestamp?

`fabric_query` with a `SELECT COUNT(*)` and a `MAX(...)`.

**Read pipeline logic without the portal**
> What does the 01_Bronze notebook do?

`fabric_get_item_definition` with `item_type="Notebook"`, `fmt="ipynb"`.

**Compare two environments**
> Do the Dev and Production versions of the sales model have the same columns?

Two `fabric_describe_table` calls, one per workspace.

---

## Performance

Display names and GUIDs cost roughly the same now. Measured on an 18-workspace tenant,
`fabric_sql_catalog` by display name:

| | |
|---|---|
| First call in a fresh server process | ~4.9 s |
| Subsequent calls, name resolution cached | ~3.1 s |
| Pure TDS connect + trivial query | ~1.2 s |

Name resolution used to cost ~35 s of that. If you see numbers in that range, your copy
predates the shared-connection fix — see `FABRIC_MCP_RESOLVE_TTL` in INSTALL.md.

Name→GUID lookups are cached for 300 s. **Data listings are never cached**, so a stale
table list is never returned. A name that misses the cache triggers one uncached
re-check, so an item created seconds ago is not invisible.

---

## What this server does not do

| Not supported | Why / what to use instead |
|---|---|
| Any write, anywhere | By design. Use the portal or a pipeline |
| Service principals | The credential chain is Azure CLI → interactive browser. Every action is attributable to a person, which is the point |
| Search across all workspaces at once | Iterate `fabric_list_items`, or ask for Microsoft's Fabric Core MCP server |
| Workspace / item / permission management | Fabric Core MCP covers it — note it is **not** read-only and reports no identity |
| Non-Delta files under `Files/` | Invisible to T-SQL. Read them in a notebook |

---

## Reporting a problem

Include the `fabric_whoami` output (it names the instance, selector and identity) and the
output of `.\diagnose.ps1`. Those two together answer most questions without a
back-and-forth. Redact the tokens — `fabric_whoami` does not print them, but tracebacks can.
