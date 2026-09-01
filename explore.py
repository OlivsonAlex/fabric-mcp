#!/usr/bin/env python3
"""
One-shot exploration + verification for a Fabric lakehouse, using the
fabric-mcp server module directly (no MCP host needed).

    .\.venv\Scripts\python.exe explore.py "analytics_ws" "lakehouse"

What it does, in order:
  1. resolve the workspace and lakehouse, print the SQL endpoint
  2. list Delta tables via the Fabric REST Tables API
  3. list tables/views as the SQL engine sees them (INFORMATION_SCHEMA)
  4. reconcile 2 against 3 and name any discrepancy
  5. row-count every SQL-visible table over a single reused connection
  6. SELECT TOP 5 from the widest table, to prove data actually comes back

Read-only throughout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def load_server():
    spec = importlib.util.spec_from_file_location(
        "fabric_mcp_server", os.path.join(HERE, "fabric_mcp_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def head(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    workspace, lakehouse = sys.argv[1], sys.argv[2]

    # Quiet the azure/httpx INFO chatter so the output stays readable.
    import logging

    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    m = load_server()

    # ---------------------------------------------------------------- 1
    head(f"1. Resolving  workspace={workspace!r}  lakehouse={lakehouse!r}")
    try:
        server, database, meta = m._sql_target(workspace, lakehouse)
    except Exception:
        traceback.print_exc()
        print("\nCould not resolve. Listing what you do have access to:")
        try:
            print(m.fabric_list_workspaces())
        except Exception:
            traceback.print_exc()
        return 1
    print(json.dumps({"server": server, "database": database, **meta}, indent=2, ensure_ascii=False))

    # ---------------------------------------------------------------- 2
    head("2. Delta tables via Fabric REST (fabric_list_tables)")
    rest_names: set[str] = set()
    try:
        rest = json.loads(m.fabric_list_tables(workspace, lakehouse))
        print(f"count: {rest['count']}")
        for t in rest["tables"]:
            nm = t.get("name")
            rest_names.add(nm)
            print(f"  - {nm:<40} type={t.get('type','?'):<10} format={t.get('format','?')}")
    except Exception:
        traceback.print_exc()

    # ---------------------------------------------------------------- 3
    head("3. Tables/views via the SQL analytics endpoint (INFORMATION_SCHEMA)")
    sql_rows: list[list] = []
    try:
        cat = json.loads(m.fabric_sql_catalog(workspace, lakehouse))
        if "rows" in cat:
            sql_rows = cat["rows"]
            print(f"count: {cat['row_count']}   elapsed: {cat.get('elapsed_ms')}ms")
            print(f"  {'schema':<12} {'name':<40} {'type':<12} columns")
            for r in sql_rows:
                print(f"  {str(r[0]):<12} {str(r[1]):<40} {str(r[2]):<12} {r[3]}")
        else:
            print(json.dumps(cat, indent=2)[:2000])
    except Exception:
        traceback.print_exc()
        print("\n>>> This is the step that exercises the ODBC access-token path.")
        print(">>> If it failed here but section 1 succeeded, the token struct or")
        print(">>> the endpoint permission is the problem, not name resolution.")
        return 1

    # ---------------------------------------------------------------- 4
    head("4. Reconciliation: REST vs SQL")
    sql_names = {str(r[1]) for r in sql_rows}
    only_rest = sorted(rest_names - sql_names)
    only_sql = sorted(sql_names - rest_names)
    print(f"REST tables: {len(rest_names)}   SQL-visible: {len(sql_names)}")
    if only_rest:
        print(f"  in REST but NOT queryable in T-SQL ({len(only_rest)}): {only_rest}")
        print("    -> not Delta format, or the SQL endpoint metadata sync is lagging")
    if only_sql:
        print(f"  in SQL but not in the REST table list ({len(only_sql)}): {only_sql}")
        print("    -> views, or shortcut-backed tables")
    if not only_rest and not only_sql:
        print("  exact match")

    # ---------------------------------------------------------------- 5
    head("5. Row counts (single reused connection)")
    counts: list[tuple[str, str, int | str]] = []
    conn = None
    try:
        conn = m._connect(server, database)
        cur = conn.cursor()
        for r in sql_rows:
            schema, name = str(r[0]), str(r[1])
            q = f"SELECT COUNT_BIG(*) FROM {bracket(schema)}.{bracket(name)}"
            started = time.time()
            try:
                cur.execute(q)
                n = cur.fetchone()[0]
                counts.append((schema, name, int(n)))
                print(f"  {schema}.{name:<40} {int(n):>14,}   ({int((time.time()-started)*1000)}ms)")
            except Exception as e:  # noqa: BLE001
                counts.append((schema, name, f"ERROR: {e}"))
                print(f"  {schema}.{name:<40} {'ERROR':>14}   {str(e)[:120]}")
    except Exception:
        traceback.print_exc()
    finally:
        if conn is not None:
            conn.close()

    # ---------------------------------------------------------------- 6
    head("6. Sample data (proves rows actually come back)")
    widest = None
    if sql_rows:
        widest = max(sql_rows, key=lambda r: (r[3] or 0))
    if widest:
        schema, name = str(widest[0]), str(widest[1])
        q = f"SELECT TOP 5 * FROM {bracket(schema)}.{bracket(name)}"
        print(f"query: {q}\n")
        try:
            res = json.loads(m.fabric_query_raw(server, database, q, 5))
            print("columns: " + ", ".join(res["columns"]))
            for row in res["rows"]:
                print("  " + " | ".join("NULL" if v is None else str(v)[:28] for v in row))
            print(f"\nrows returned: {res['row_count']}   elapsed: {res['elapsed_ms']}ms")
        except Exception:
            traceback.print_exc()
    else:
        print("  no tables to sample")

    head("SUMMARY")
    ok = [c for c in counts if isinstance(c[2], int)]
    print(f"workspace           : {meta.get('workspace')}  ({meta.get('workspace_id')})")
    print(f"lakehouse           : {meta.get('item')}  ({meta.get('item_id')})")
    print(f"sql endpoint        : {server}")
    print(f"database            : {database}")
    print(f"default schema      : {meta.get('default_schema') or '(non-schema-enabled)'}")
    print(f"REST tables         : {len(rest_names)}")
    print(f"SQL-visible objects : {len(sql_names)}")
    print(f"counted cleanly     : {len(ok)} / {len(counts)}")
    if ok:
        print(f"total rows          : {sum(c[2] for c in ok):,}")
        biggest = max(ok, key=lambda c: c[2])
        print(f"largest table       : {biggest[0]}.{biggest[1]}  {biggest[2]:,} rows")
    print("\nPaste this back to Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
