#!/usr/bin/env python3
"""
fabric-mcp — a local MCP server for Microsoft Fabric.

Two surfaces, one identity (your own Entra user via Azure CLI):

  Fabric REST API  (audience https://api.fabric.microsoft.com/.default)
      -> workspaces, every item type (notebooks, pipelines, lakehouses, ...),
         lakehouse Delta table lists, SQL endpoint connection strings.

  SQL analytics endpoint / Warehouse over TDS  (audience https://database.windows.net/.default)
      -> read-only T-SQL against lakehouse Delta tables and warehouses.

Auth chain: AzureCliCredential -> InteractiveBrowserCredential.
Run `az login` once and everything is silent afterwards.

Multiple tenants / multiple users: register one instance per identity, each with
its own env block, and select the identity with FABRIC_MCP_AZ_SUBSCRIPTION -- the
subscription id of that account. That is the ONLY selector that picks a stored
`az login`; the token then comes from that account in its own home tenant.

FABRIC_MCP_TENANT_ID does NOT do this. It keeps whichever az account is currently
active and asks it for a token in another tenant, which fails with AADSTS90072
unless that user is a guest there. Use it only for a genuine guest/B2B case. The
two are mutually exclusive -- `az account get-access-token` refuses both at once --
and subscription wins when both are set.

FABRIC_MCP_INSTANCE labels the instance. fabric_whoami reports the actual UPN and
tenant from the token claims plus which selector was used: do not assume the
identity from the instance name.

Every query tool is read-only. There is no write path in this file at all:
no INSERT/UPDATE/DELETE/DDL is ever sent, and the guard rejects them.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import struct
import threading
import time
from typing import Any

import httpx
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    InteractiveBrowserCredential,
)
# The MCP Python SDK renamed FastMCP -> MCPServer in 2.x. Support both.
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
except ImportError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _MCPServer

# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

FABRIC_API = "https://api.fabric.microsoft.com/v1"
SCOPE_FABRIC = "https://api.fabric.microsoft.com/.default"
SCOPE_SQL = "https://database.windows.net/.default"

# SQL_COPT_SS_ACCESS_TOKEN — the ODBC connection attribute used to hand a
# pre-acquired Entra bearer token to the SQL Server driver.
SQL_COPT_SS_ACCESS_TOKEN = 1256

DEFAULT_MAX_ROWS = int(os.getenv("FABRIC_MCP_MAX_ROWS", "2000"))
SQL_TIMEOUT_SECONDS = int(os.getenv("FABRIC_MCP_SQL_TIMEOUT", "120"))
# Seconds to cache NAME -> GUID resolution lookups. 0 disables the cache.
RESOLVE_TTL_SECONDS = int(os.getenv("FABRIC_MCP_RESOLVE_TTL", "300"))
TENANT_ID = os.getenv("FABRIC_MCP_TENANT_ID") or None
# Label for this server instance, so fabric_whoami can say which one answered
# when several are registered against different tenants or identities.
INSTANCE = os.getenv("FABRIC_MCP_INSTANCE") or "default"
# Azure CLI subscription name or id. Per the azure-identity docs, this is how you
# acquire tokens for an az account OTHER than the CLI's current one -- which is
# what lets one instance per tenant/user work without `az account set`.
AZ_SUBSCRIPTION = os.getenv("FABRIC_MCP_AZ_SUBSCRIPTION") or None

app = _MCPServer("fabric-mcp")

# ----------------------------------------------------------------------------
# tokens
# ----------------------------------------------------------------------------

_cred_lock = threading.Lock()
_cred: ChainedTokenCredential | None = None
_token_cache: dict[str, tuple[str, int]] = {}


def _construct(cls, **kwargs):
    """Instantiate cls with only the keyword arguments its signature accepts.

    A TypeError here would crash the server on import, and Claude Desktop shows
    no error for a server that dies before registering -- the tools simply never
    appear. Dropping unsupported kwargs fails soft instead.
    """
    try:
        allowed = set(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):
        return cls(**kwargs)
    if "kwargs" in allowed:
        return cls(**kwargs)
    return cls(**{k: v for k, v in kwargs.items() if k in allowed})


def _token_identity(token: str) -> dict[str, Any]:
    """Display claims from a JWT payload.

    No signature verification: this exists so the operator can see WHICH identity
    answered, not to make an authorization decision.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not decode token claims: {e}"}
    return {
        "upn": claims.get("upn")
        or claims.get("preferred_username")
        or claims.get("unique_name"),
        "name": claims.get("name"),
        "object_id": claims.get("oid"),
        "tenant_id": claims.get("tid"),
        "audience": claims.get("aud"),
    }


def _credential() -> ChainedTokenCredential:
    global _cred
    with _cred_lock:
        if _cred is None:
            # `az account get-access-token` refuses --subscription and --tenant
            # together ("Please specify only one of subscription and tenant"), so
            # these two selectors are mutually exclusive.
            #
            # They also do different things, and only one of them does what we want:
            #
            #   subscription -> picks WHICH STORED az LOGIN to use. The token comes
            #                   from that account in its own home tenant. This is the
            #                   correct selector for "one instance per client identity".
            #   tenant_id    -> keeps the CURRENTLY ACTIVE account and asks it for a
            #                   token in another tenant. That only succeeds if the
            #                   active user is a guest there; otherwise AADSTS90072.
            #
            # So subscription wins whenever it is set, and tenant_id is only used as a
            # cross-tenant (guest) request for the active account.
            cli_kwargs: dict[str, Any] = {}
            browser_kwargs: dict[str, Any] = {}

            if AZ_SUBSCRIPTION:
                cli_kwargs["subscription"] = AZ_SUBSCRIPTION
            elif TENANT_ID:
                cli_kwargs["tenant_id"] = TENANT_ID
                # Without this, a token request for a tenant other than the
                # credential's default is refused rather than attempted.
                cli_kwargs["additionally_allowed_tenants"] = ["*"]

            # The browser fallback can genuinely sign in as a different user, so it
            # always gets the tenant when we know it.
            if TENANT_ID:
                browser_kwargs["tenant_id"] = TENANT_ID

            _cred = ChainedTokenCredential(
                _construct(AzureCliCredential, **cli_kwargs),
                _construct(InteractiveBrowserCredential, **browser_kwargs),
            )
        return _cred


def _token(scope: str) -> str:
    """Cached bearer token for a scope, refreshed 5 minutes before expiry."""
    cached = _token_cache.get(scope)
    if cached and cached[1] - 300 > time.time():
        return cached[0]
    at = _credential().get_token(scope)
    _token_cache[scope] = (at.token, at.expires_on)
    return at.token


# ----------------------------------------------------------------------------
# Fabric REST
# ----------------------------------------------------------------------------


_http_lock = threading.Lock()
_http: httpx.Client | None = None


def _http_client() -> httpx.Client:
    """One shared client for the process, so connections are reused.

    A fresh httpx.Client per request means a fresh TCP + TLS handshake per request.
    With continuationUri pagination that cost is paid per page, which is where a
    workspace listing was spending ~35s.
    """
    global _http
    with _http_lock:
        if _http is None:
            _http = httpx.Client(
                timeout=60.0,
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            )
        return _http


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{FABRIC_API}{path}"
    headers = {"Authorization": f"Bearer {_token(SCOPE_FABRIC)}"}

    # A pooled connection can be closed by the far end while idle. That surfaces as a
    # transport error on first use, so retry once with a rebuilt client before failing.
    for attempt in (0, 1):
        try:
            r = _http_client().get(url, headers=headers, params=params)
            break
        except httpx.TransportError:
            if attempt == 1:
                raise
            global _http
            with _http_lock:
                if _http is not None:
                    try:
                        _http.close()
                    except Exception:  # noqa: BLE001
                        pass
                    _http = None

    if r.status_code >= 400:
        raise RuntimeError(f"Fabric API {r.status_code} on {url}: {r.text[:800]}")
    return r.json() if r.content else {}


def _get_all(path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Follow continuationUri pagination and return the concatenated `value` list."""
    out: list[dict[str, Any]] = []
    body = _get(path, params)
    out.extend(body.get("value", []))
    guard = 0
    while body.get("continuationUri") and guard < 200:
        guard += 1
        body = _get(body["continuationUri"])
        out.extend(body.get("value", []))
    return out


_resolve_lock = threading.Lock()
_resolve_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _get_all_cached(path: str, refresh: bool = False) -> list[dict[str, Any]]:
    """_get_all with a short TTL, for NAME -> GUID resolution lookups only.

    Data listings stay uncached on purpose: a stale table list is a wrong answer,
    whereas a stale workspace id is either still correct or fails cleanly with a 404.
    Callers pass refresh=True to bypass the cache when a name was NOT found, so a
    newly created workspace or item is not invisible for the length of the TTL.
    """
    if RESOLVE_TTL_SECONDS <= 0:
        return _get_all(path)
    now = time.time()
    if not refresh:
        with _resolve_lock:
            hit = _resolve_cache.get(path)
            if hit and now - hit[0] < RESOLVE_TTL_SECONDS:
                return hit[1]
    value = _get_all(path)
    with _resolve_lock:
        _resolve_cache[path] = (time.time(), value)
    return value


def _is_guid(s: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            s.strip(),
        )
    )


def _resolve_workspace(workspace: str) -> tuple[str, str]:
    """Accept a workspace GUID or display name. Returns (id, displayName)."""
    if _is_guid(workspace):
        ws = _get(f"/workspaces/{workspace}")
        return ws["id"], ws.get("displayName", workspace)
    wanted = workspace.strip().casefold()
    all_ws = _get_all_cached("/workspaces")
    hits = [w for w in all_ws if (w.get("displayName") or "").casefold() == wanted]
    if not hits:
        # Could be a workspace created since the cache was filled.
        all_ws = _get_all_cached("/workspaces", refresh=True)
        hits = [w for w in all_ws if (w.get("displayName") or "").casefold() == wanted]
    if not hits:
        near = [w.get("displayName") for w in all_ws if wanted in (w.get("displayName") or "").casefold()]
        raise RuntimeError(
            f"No workspace named {workspace!r}. "
            + (f"Close matches: {near}. " if near else "")
            + f"You have access to {len(all_ws)} workspaces; call fabric_list_workspaces."
        )
    if len(hits) > 1:
        raise RuntimeError(
            f"{len(hits)} workspaces named {workspace!r}; pass the GUID instead: "
            f"{[h['id'] for h in hits]}"
        )
    return hits[0]["id"], hits[0].get("displayName", workspace)


def _resolve_item(ws_id: str, item: str, item_types: tuple[str, ...]) -> dict[str, Any]:
    """Accept an item GUID or display name, restricted to the given item types."""
    def _load(refresh: bool = False) -> list[dict[str, Any]]:
        return [
            i
            for i in _get_all_cached(f"/workspaces/{ws_id}/items", refresh=refresh)
            if i.get("type") in item_types
        ]

    typed = _load()
    def _match(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if _is_guid(item):
            return [i for i in pool if i.get("id", "").casefold() == item.strip().casefold()]
        wanted = item.strip().casefold()
        return [i for i in pool if (i.get("displayName") or "").casefold() == wanted]

    hits = _match(typed)
    if not hits:
        # Could be an item created since the cache was filled.
        typed = _load(refresh=True)
        hits = _match(typed)
    if not hits:
        raise RuntimeError(
            f"No {'/'.join(item_types)} named {item!r} in this workspace. "
            f"Available: {[i.get('displayName') for i in typed]}"
        )
    if len(hits) > 1:
        raise RuntimeError(f"Ambiguous item {item!r}; pass the GUID: {[h['id'] for h in hits]}")
    return hits[0]


# ----------------------------------------------------------------------------
# SQL
# ----------------------------------------------------------------------------

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_ALLOWED_FIRST_KEYWORD = ("select", "with")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|deny"
    r"|backup|restore|exec|execute|reconfigure|shutdown|sp_\w+|xp_\w+"
    r"|openrowset|opendatasource|bulk)\b",
    re.I,
)


def _assert_read_only(sql: str) -> str:
    stripped = _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql)).strip()
    if not stripped:
        raise ValueError("Empty statement.")
    first = re.match(r"\s*([A-Za-z_]+)", stripped)
    if not first or first.group(1).lower() not in _ALLOWED_FIRST_KEYWORD:
        raise ValueError(
            f"Read-only server: statements must start with {' or '.join(_ALLOWED_FIRST_KEYWORD).upper()}. "
            f"Got {(first.group(1) if first else '?')!r}."
        )
    bad = _FORBIDDEN.search(stripped)
    if bad:
        raise ValueError(
            f"Read-only server: rejected keyword {bad.group(0)!r}. "
            "If it appears inside a string literal, rewrite the query."
        )
    return sql


def _odbc_driver() -> str:
    import pyodbc

    installed = pyodbc.drivers()
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in installed:
            return candidate
    raise RuntimeError(
        "No suitable ODBC driver found. Install 'ODBC Driver 18 for SQL Server'. "
        f"Drivers present: {installed}"
    )


def _connect(server: str, database: str):
    import pyodbc

    token = _token(SCOPE_SQL).encode("utf-16-le")
    token_struct = struct.pack("<I", len(token)) + token
    conn_str = (
        f"DRIVER={{{_odbc_driver()}}};"
        f"SERVER={server},1433;"
        f"DATABASE={database};"
        "Encrypt=yes;TrustServerCertificate=no;"
        f"Connection Timeout={SQL_TIMEOUT_SECONDS};"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def _run_sql(server: str, database: str, sql: str, max_rows: int) -> dict[str, Any]:
    started = time.time()
    conn = _connect(server, database)
    try:
        conn.timeout = SQL_TIMEOUT_SECONDS
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description is None:
            return {"columns": [], "rows": [], "row_count": 0, "note": "Statement returned no result set."}
        columns = [d[0] for d in cur.description]
        raw = cur.fetchmany(max_rows + 1)
        truncated = len(raw) > max_rows
        rows = [[_jsonable(v) for v in row] for row in raw[:max_rows]]
    finally:
        conn.close()
    return {
        "server": server,
        "database": database,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _jsonable(v: Any) -> Any:
    import datetime
    import decimal
    import uuid

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _sql_target(workspace: str, item: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve a lakehouse or warehouse to (server, database, metadata)."""
    ws_id, ws_name = _resolve_workspace(workspace)
    found = _resolve_item(ws_id, item, ("Lakehouse", "Warehouse", "MirroredDatabase", "SQLDatabase"))
    itype, iid, iname = found["type"], found["id"], found.get("displayName", item)

    if itype == "Lakehouse":
        lh = _get(f"/workspaces/{ws_id}/lakehouses/{iid}")
        ep = (lh.get("properties") or {}).get("sqlEndpointProperties") or {}
        server = ep.get("connectionString")
        status = ep.get("provisioningStatus")
        if not server:
            raise RuntimeError(f"Lakehouse {iname!r} has no SQL endpoint yet (status={status}).")
        if status and status != "Success":
            raise RuntimeError(f"Lakehouse {iname!r} SQL endpoint provisioningStatus={status}.")
        meta = {
            "workspace": ws_name,
            "workspace_id": ws_id,
            "item": iname,
            "item_id": iid,
            "item_type": itype,
            "sql_endpoint_id": ep.get("id"),
            "default_schema": (lh.get("properties") or {}).get("defaultSchema"),
            "read_only": True,
        }
        return server, iname, meta

    if itype == "Warehouse":
        wh = _get(f"/workspaces/{ws_id}/warehouses/{iid}")
        server = (wh.get("properties") or {}).get("connectionString")
        if not server:
            raise RuntimeError(f"Warehouse {iname!r} returned no connectionString.")
        meta = {
            "workspace": ws_name,
            "workspace_id": ws_id,
            "item": iname,
            "item_id": iid,
            "item_type": itype,
            "collation": (wh.get("properties") or {}).get("collationType"),
            "read_only": True,
        }
        return server, iname, meta

    raise RuntimeError(
        f"{iname!r} is a {itype}. This server resolves SQL endpoints for Lakehouse and "
        "Warehouse items only; use fabric_sql_endpoint_raw with an explicit server name."
    )


# ----------------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------------


@app.tool()
def fabric_whoami() -> str:
    """Check that authentication works. Returns the signed-in identity and token audiences acquired."""
    out: dict[str, Any] = {}
    out["instance"] = INSTANCE
    out["configured_tenant_id"] = TENANT_ID or "(azure CLI default)"
    out["configured_az_subscription"] = AZ_SUBSCRIPTION or "(azure CLI current account)"
    out["resolve_cache_ttl_seconds"] = RESOLVE_TTL_SECONDS
    out["cli_selector"] = (
        f"subscription={AZ_SUBSCRIPTION} (picks the stored az login)"
        if AZ_SUBSCRIPTION
        else f"tenant={TENANT_ID} (active az account, cross-tenant)"
        if TENANT_ID
        else "none (active az account, home tenant)"
    )
    try:
        _tok = _token(SCOPE_FABRIC)
        out["fabric_api_token"] = "ok"
        out["identity"] = _token_identity(_tok)
    except Exception as e:  # noqa: BLE001
        out["fabric_api_token"] = f"FAILED: {e}"
    try:
        _token(SCOPE_SQL)
        out["sql_token"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["sql_token"] = f"FAILED: {e}"
    try:
        ws = _get_all("/workspaces")
        out["workspaces_visible"] = len(ws)
    except Exception as e:  # noqa: BLE001
        out["workspaces_visible"] = f"FAILED: {e}"
    try:
        import pyodbc

        out["odbc_drivers"] = pyodbc.drivers()
    except Exception as e:  # noqa: BLE001
        out["odbc_drivers"] = f"pyodbc unavailable: {e}"
    return json.dumps(out, indent=2, ensure_ascii=False)


@app.tool()
def fabric_list_workspaces() -> str:
    """List every Fabric workspace the signed-in user can see (id, name, capacity, type)."""
    ws = _get_all("/workspaces")
    slim = [
        {
            "id": w.get("id"),
            "displayName": w.get("displayName"),
            "type": w.get("type"),
            "capacityId": w.get("capacityId"),
            "description": w.get("description"),
        }
        for w in ws
    ]
    slim.sort(key=lambda w: (w["displayName"] or "").casefold())
    return json.dumps({"count": len(slim), "workspaces": slim}, indent=2, ensure_ascii=False)


@app.tool()
def fabric_list_items(workspace: str, item_type: str = "") -> str:
    """List Fabric objects in a workspace: notebooks, data pipelines, lakehouses, warehouses,
    semantic models, reports, dataflows, environments, eventstreams, and every other item type.

    workspace: workspace display name or GUID.
    item_type: optional filter, e.g. Notebook, DataPipeline, Lakehouse, Warehouse,
               SemanticModel, Report, Dataflow, Environment, SparkJobDefinition,
               Eventhouse, KQLDatabase, Eventstream, CopyJob, VariableLibrary,
               MirroredDatabase, SQLDatabase, GraphQLApi, DataAgent, MLModel,
               MLExperiment, Reflex. Empty = all types.
    """
    ws_id, ws_name = _resolve_workspace(workspace)
    params = {"type": item_type} if item_type.strip() else None
    items = _get_all(f"/workspaces/{ws_id}/items", params)
    slim = [
        {
            "id": i.get("id"),
            "type": i.get("type"),
            "displayName": i.get("displayName"),
            "description": i.get("description"),
            "folderId": i.get("folderId"),
        }
        for i in items
    ]
    by_type: dict[str, int] = {}
    for i in slim:
        by_type[i["type"] or "?"] = by_type.get(i["type"] or "?", 0) + 1
    slim.sort(key=lambda i: ((i["type"] or ""), (i["displayName"] or "").casefold()))
    return json.dumps(
        {
            "workspace": ws_name,
            "workspace_id": ws_id,
            "count": len(slim),
            "count_by_type": dict(sorted(by_type.items())),
            "items": slim,
        },
        indent=2,
        ensure_ascii=False,
    )


@app.tool()
def fabric_list_tables(workspace: str, lakehouse: str) -> str:
    """List the Delta tables in a lakehouse via the Fabric Tables REST API.
    Returns name, type (Managed/External), format, and OneLake location for each table.
    """
    ws_id, ws_name = _resolve_workspace(workspace)
    lh = _resolve_item(ws_id, lakehouse, ("Lakehouse",))
    tables = _get_all(f"/workspaces/{ws_id}/lakehouses/{lh['id']}/tables")
    return json.dumps(
        {
            "workspace": ws_name,
            "lakehouse": lh.get("displayName"),
            "lakehouse_id": lh["id"],
            "count": len(tables),
            "tables": tables,
        },
        indent=2,
        ensure_ascii=False,
    )


@app.tool()
def fabric_sql_endpoint(workspace: str, item: str) -> str:
    """Resolve the T-SQL connection details for a lakehouse or warehouse:
    server (SQL analytics endpoint connection string), database, and item metadata.
    """
    server, database, meta = _sql_target(workspace, item)
    return json.dumps({"server": server, "database": database, **meta}, indent=2, ensure_ascii=False)


@app.tool()
def fabric_sql_catalog(workspace: str, item: str) -> str:
    """List tables and views as the SQL engine sees them, with column counts,
    by querying INFORMATION_SCHEMA on the lakehouse SQL analytics endpoint or warehouse.

    Use this rather than fabric_list_tables when you care about what is actually
    queryable in T-SQL: only Delta tables surface here, and schema names are included.
    """
    server, database, meta = _sql_target(workspace, item)
    sql = """
        SELECT t.TABLE_SCHEMA  AS [schema],
               t.TABLE_NAME    AS [name],
               t.TABLE_TYPE    AS [type],
               COUNT(c.COLUMN_NAME) AS [columns]
        FROM INFORMATION_SCHEMA.TABLES AS t
        LEFT JOIN INFORMATION_SCHEMA.COLUMNS AS c
               ON c.TABLE_SCHEMA = t.TABLE_SCHEMA
              AND c.TABLE_NAME   = t.TABLE_NAME
        GROUP BY t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE
        ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
    """
    result = _run_sql(server, database, sql, DEFAULT_MAX_ROWS)
    return json.dumps({**meta, **result}, indent=2, ensure_ascii=False)


@app.tool()
def fabric_describe_table(workspace: str, item: str, table: str, schema: str = "dbo") -> str:
    """Column metadata for one table: ordinal, name, data type, length/precision, nullability."""
    server, database, meta = _sql_target(workspace, item)
    safe_schema = schema.replace("'", "''")
    safe_table = table.replace("'", "''")
    sql = f"""
        SELECT ORDINAL_POSITION      AS [pos],
               COLUMN_NAME           AS [column],
               DATA_TYPE             AS [type],
               CHARACTER_MAXIMUM_LENGTH AS [max_len],
               NUMERIC_PRECISION     AS [precision],
               NUMERIC_SCALE         AS [scale],
               IS_NULLABLE           AS [nullable]
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = '{safe_schema}' AND TABLE_NAME = '{safe_table}'
        ORDER BY ORDINAL_POSITION
    """
    result = _run_sql(server, database, sql, DEFAULT_MAX_ROWS)
    if result["row_count"] == 0:
        result["note"] = (
            f"No columns found for [{schema}].[{table}]. Check the name with fabric_sql_catalog. "
            "Only Delta tables appear on a lakehouse SQL analytics endpoint."
        )
    return json.dumps({**meta, "table": f"[{schema}].[{table}]", **result}, indent=2, ensure_ascii=False)


@app.tool()
def fabric_query(workspace: str, item: str, sql: str, max_rows: int = 0) -> str:
    """Run a read-only T-SQL query against a lakehouse SQL analytics endpoint or a warehouse.

    workspace: workspace display name or GUID.
    item:      lakehouse or warehouse display name or GUID.
    sql:       must start with SELECT or WITH. DML/DDL is rejected.
    max_rows:  row cap; 0 uses the server default.

    Notes on the lakehouse SQL analytics endpoint: it is read-only, it exposes only
    Delta tables (Parquet/CSV files in Files/ are not queryable here), and it does not
    support the full warehouse T-SQL surface.
    """
    _assert_read_only(sql)
    cap = max_rows if max_rows and max_rows > 0 else DEFAULT_MAX_ROWS
    server, database, meta = _sql_target(workspace, item)
    result = _run_sql(server, database, sql, cap)
    return json.dumps({**meta, **result}, indent=2, ensure_ascii=False)


@app.tool()
def fabric_query_raw(server: str, database: str, sql: str, max_rows: int = 0) -> str:
    """Escape hatch: read-only T-SQL against an explicit Fabric SQL server name and database.
    Use when you already have the connection string (e.g. copied from the Fabric portal)
    and do not want name resolution. Same read-only guard as fabric_query.
    """
    _assert_read_only(sql)
    cap = max_rows if max_rows and max_rows > 0 else DEFAULT_MAX_ROWS
    return json.dumps(_run_sql(server, database, sql, cap), indent=2, ensure_ascii=False)


@app.tool()
def fabric_get_item_definition(workspace: str, item: str, item_type: str, fmt: str = "") -> str:
    """Fetch an item's definition — e.g. a notebook's code, a data pipeline's JSON.

    workspace: workspace display name or GUID.
    item:      item display name or GUID.
    item_type: the item's type, e.g. Notebook or DataPipeline (used to disambiguate names).
    fmt:       optional format, e.g. 'ipynb' for notebooks. Empty = service default.

    Returns base64 payload parts as the API provides them; decode client-side.
    """
    ws_id, ws_name = _resolve_workspace(workspace)
    found = _resolve_item(ws_id, item, (item_type,))
    url = f"{FABRIC_API}/workspaces/{ws_id}/items/{found['id']}/getDefinition"
    if fmt.strip():
        url += f"?format={fmt.strip()}"
    headers = {"Authorization": f"Bearer {_token(SCOPE_FABRIC)}"}
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        r = client.post(url, headers=headers)
        # getDefinition can run as a long-running operation: 202 + Location/Retry-After.
        waited = 0
        while r.status_code == 202 and waited < 300:
            delay = int(r.headers.get("Retry-After", "3") or 3)
            loc = r.headers.get("Location")
            if not loc:
                break
            time.sleep(delay)
            waited += delay
            r = client.get(loc, headers=headers)
        if r.status_code == 200 and "/operations/" in str(r.request.url):
            state = r.json()
            if state.get("status") in ("Succeeded", "Completed"):
                r = client.get(str(r.request.url).rstrip("/") + "/result", headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"Fabric API {r.status_code} on getDefinition: {r.text[:800]}")
    body = r.json() if r.content else {}
    return json.dumps(
        {"workspace": ws_name, "item": found.get("displayName"), "item_type": item_type, **body},
        indent=2,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    app.run(transport="stdio")
