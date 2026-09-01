# fabric-mcp — Installation

A local MCP server that gives Claude **read-only** access to Microsoft Fabric under
**your own Entra identity**. No service principal, no client secret, nothing stored.

Everything you can reach is bounded by your Fabric RBAC, and every operation lands in
Fabric's audit log under your name.

Written for Windows with Claude Desktop, which is what the team runs.

---

## Before you start

| Requirement | Notes |
|---|---|
| Python 3.10+ on PATH | `python --version` |
| ODBC Driver 18 for SQL Server, **64-bit** | https://aka.ms/downloadmsodbcsql — Driver 17 works but 18 is expected |
| Azure CLI | `az version`. Without it you get a browser popup on every server start |
| Claude Desktop | Quit-from-tray access needed, see step 5 |
| Fabric access | A workspace role in the tenant you want to reach, and the tenant setting allowing SQL endpoint access |

You need one more thing that is easy to overlook: **the subscription GUID for the
identity you will use.** Step 3 covers it.

---

## Step 1 — Get the folder

This server has no upstream repository — it is internal code. Get it from whoever sent
you this document. You need these five files in one directory:

```
fabric_mcp_server.py    the server
setup.ps1               prerequisites, venv, registration
diagnose.ps1            troubleshooting
requirements.txt        pinned dependencies
README.md               design notes
```

The examples below assume:

```
C:\Users\<you>\Claude\Projects\fabric-mcp
```

**Do not copy `.venv` or `__pycache__`** if someone hands you a zipped folder. A virtual
environment contains absolute paths from the machine that built it, and the registered
config points at `.venv\Scripts\python.exe` — a copied venv gives you a server that
cannot start, with no error shown. Delete both and let `setup.ps1` rebuild. Skip any
`*.bak-*` files too; they are one-off config backups from someone else's machine.

Nothing is installed globally.

## Step 2 — Sign in, once per identity

Client work usually means a client-issued account. Sign in as each identity you need:

```powershell
az login                                     # your own account
az login --tenant <client-tenant-guid>       # a client account
```

Logins **accumulate** — a second `az login` does not remove the first. Add
`--allow-no-subscriptions` if a tenant grants you no Azure subscription.

## Step 3 — Find your subscription GUID

```powershell
az account list --all --query "[].{user:user.name, tenant:tenantId, subscription:name, id:id}" -o json
```

Note the `id` for each identity. **Use the GUID, never the subscription name** —
names like `Azure Plan` and `Azure subscription 1` are generic and collide across
tenants, and a name-based selector will silently resolve to the wrong one.

## Step 4 — Register

One server instance per identity. Name it after the client so the tool list is readable:

```powershell
cd C:\Users\<you>\Claude\Projects\fabric-mcp
.\setup.ps1                                                    # dry run: checks, venv, smoke test, prints the config target
.\setup.ps1 -RegisterClaude -Name fabric-<client> -Subscription <subscription-guid>
```

Run the dry form first. It verifies prerequisites, builds the venv, runs
`fabric_whoami`, and prints which config file it would write — without writing.

The smoke test runs under the same environment as the instance you are registering, so
**read the UPN it prints.** If it is not the identity you expect, stop; do not register.

`-RegisterClaude` refuses to run while Claude Desktop is alive, and writes a timestamped
`.bak` before touching the config.

## Step 5 — Restart Claude Desktop properly

Quit from the **system tray**, not by closing the window. The app keeps running in the
tray and reads its config only at process start. Then reopen.

## Step 6 — Verify

In Claude, run `fabric_whoami` on the instance. Check three things:

```json
{
  "instance": "fabric-<client>",
  "cli_selector": "subscription=<guid> (picks the stored az login)",
  "identity": { "upn": "<the account you expected>", "tenant_id": "<the tenant you expected>" },
  "fabric_api_token": "ok",
  "sql_token": "ok",
  "workspaces_visible": 18
}
```

**Read `identity.upn`, not the instance name.** The name is a label you chose; the UPN is
decoded from the token the server actually got. Trusting the label is how a whole session
runs under the wrong account without anyone noticing.

---

## The config trap you will hit if you edit by hand

Claude Desktop on Windows is an MSIX/Store package, so `claude_desktop_config.json`
exists in **two** places:

| | |
|---|---|
| What the app reads and writes | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| What "Edit Config", the docs, and every blog post point at | `%APPDATA%\Claude\claude_desktop_config.json` |

Editing `%APPDATA%` is a **silent no-op**: the server never appears and Claude shows no
error. `setup.ps1` and `diagnose.ps1` resolve this automatically, so prefer them over
hand-editing.

Two things not to do:

- **Do not bridge the paths with a junction.** It is widely recommended and it converts a
  harmless shadow copy into real data loss if anything writes a partial config.
- **Do not hand-edit the virtualized file** while the app is running. It writes its own
  state there and can overwrite you.

Confirm which install you have:

```powershell
Get-AppxPackage -Name Claude | Select-Object Name, Version, PackageFullName
```

---

## Several identities at once

Each instance is its own process, so they run simultaneously and independently of which
`az` account is currently active:

```powershell
.\setup.ps1 -RegisterClaude -Name fabric-clienta -Subscription <guid-a>
.\setup.ps1 -RegisterClaude -Name fabric-clientb -Subscription <guid-b>
```

**Use `-Subscription`, not `-TenantId`.** They are not two ways to do the same thing:

| selector | what it actually does |
|---|---|
| `-Subscription` | picks **which stored `az` login** to use; the token comes from that account in its home tenant |
| `-TenantId` | keeps the **currently active** account and asks it for a token in another tenant — fails with `AADSTS90072` unless that user is a guest there |

The Azure CLI refuses both at once. `-TenantId` is for a genuine guest/B2B case only.

**Never leave an instance unpinned.** An entry with no `-Subscription` follows whichever
`az` account is active, so it silently repoints the next time someone runs `az login`
elsewhere — same name in the tool list, different tenant, no warning.

To rename or remove an entry, use the script rather than editing the file:

```powershell
.\setup.ps1 -RegisterClaude -Name fabric-new -Subscription <guid> -RemoveServer fabric-old
```

---

## Troubleshooting

Run `.\diagnose.ps1` first and read its output. It resolves the correct config path,
lists every `fabric*` entry, warns about unpinned instances, checks the interpreter, and
import-tests the server.

| Symptom | Cause | Fix |
|---|---|---|
| Tools never appear, no error anywhere | The server crashed on import. Claude Desktop shows nothing for a server that dies before registering | Run `.\.venv\Scripts\python.exe fabric_mcp_server.py` directly and read the traceback. It should sit silently waiting on stdin; anything else is the bug |
| Tools never appear, config looks right | You edited `%APPDATA%` | Re-run `setup.ps1 -RegisterClaude`; it targets the virtualized path |
| `AADSTS90072: ... does not exist in tenant ...` | Wrong selector — the active account was asked for another tenant's token | Register with `-Subscription`, not `-TenantId` |
| `ERROR: Please specify only one of subscription and tenant, not both` | Both selectors passed | Drop `-TenantId` |
| `Login failed for user '<token-identified principal>'` (SQL 18456) | Token is valid, that identity has no access to the item | Check your workspace role, and that the tenant setting allowing SQL endpoint access is on |
| `No suitable ODBC driver found` | Driver 18 missing or 32-bit | Install 64-bit ODBC Driver 18, restart Claude Desktop |
| `No module named 'mcp.server.fastmcp'` | MCP SDK 2.x renamed `FastMCP` to `MCPServer` | The server handles both; your copy predates that fix |
| A table is missing from `fabric_sql_catalog` | Not a Delta table, or endpoint metadata sync lagging | Refresh the SQL endpoint metadata from the portal |
| First call after launch is slow | Cold name resolution | Expected, roughly 2 s. If it is ~35 s your copy predates the connection-reuse fix |

Logs live next to the config the app actually uses:

```powershell
$logDir = Join-Path (Split-Path (& { $v = "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"; if (Test-Path $v) { $v } else { "$env:APPDATA\Claude\claude_desktop_config.json" } })) "logs"
Get-ChildItem $logDir | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

---

## Environment variables

Set on the server entry's `env` block by `setup.ps1`; override by editing the entry.

| Variable | Default | Effect |
|---|---|---|
| `FABRIC_MCP_AZ_SUBSCRIPTION` | unset | Subscription GUID selecting **which stored `az` login** to use. The identity selector |
| `FABRIC_MCP_TENANT_ID` | unset | Cross-tenant (guest) request for the active account. **Not** an identity selector |
| `FABRIC_MCP_INSTANCE` | `default` | Label reported by `fabric_whoami` |
| `FABRIC_MCP_MAX_ROWS` | `2000` | Default row cap per query |
| `FABRIC_MCP_SQL_TIMEOUT` | `120` | SQL connection/query timeout, seconds |
| `FABRIC_MCP_RESOLVE_TTL` | `300` | Seconds to cache name→GUID lookups. `0` disables. Data listings are never cached |
