# Project context for Claude

This repo holds two things: a local MCP server (`fabric_mcp_server.py`) that gives Claude
read-only access to Microsoft Fabric lakehouse data, and the registration plus documentation
for Microsoft's Power BI Modeling MCP, which reads and edits semantic models over XMLA.

**This file is context, not a task.** Do not start installing anything because you read it.
Installation has its own runbook: [AGENT-INSTALL.md](AGENT-INSTALL.md). Follow it only when
someone asks you to install or troubleshoot the setup.

## Which doc owns what

| Topic | File |
|---|---|
| Orientation, which server answers which question | `README.md` |
| Installing fabric-mcp, identities, troubleshooting, env vars | `INSTALL.md` |
| The ten fabric-mcp tools, read-only guarantee, examples | `USAGE.md` |
| The Power BI Modeling MCP | `POWERBI-MCP.md` |
| Runbook for you to perform an install | `AGENT-INSTALL.md` |

Scripts: `setup.ps1`, `diagnose.ps1`, `Register-PowerBIMcp.ps1`.

## Constraints that apply to any work in this repo

**fabric-mcp is read-only by design, and that is not negotiable.** There is no write path in
the code, and `_assert_read_only` rejects DML, DDL and `EXEC` anywhere in a statement. If a
change would add a write path, say so and stop rather than implementing it. The guard's false
refusals (a `SELECT` containing the word `DELETE` in a string literal) are an accepted
trade, not a bug to fix by weakening it.

**Never write to `%APPDATA%\Claude\claude_desktop_config.json`.** On an MSIX install of
Claude Desktop the app reads a package-virtualized copy under
`%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`. Writing the
`%APPDATA%` path is a silent no-op. All three scripts resolve this; do not hand-edit either
file, and never bridge them with a junction.

**Identity is selected by subscription GUID, not tenant.** `-Subscription` picks which stored
`az` login to use. `-TenantId` keeps whichever account is currently active and requests
another tenant's token, which fails with `AADSTS90072` unless that user is a guest. The
Azure CLI refuses both selectors together. Never register an instance without a selector —
an unpinned entry silently follows whoever ran `az login` last.

**Never infer an identity from an instance name.** Only `identity.upn` from `fabric_whoami`,
decoded from the token itself, is evidence.

**PowerShell 5.1 is the target.** No ternary operator, `ConvertTo-Json` needs an explicit
`-Depth`, `Set-Content -Encoding UTF8` writes a BOM (use
`[System.IO.File]::WriteAllText`), and `ConvertFrom-Json` emits a parsed array as a single
object rather than enumerating it. The `.ps1` files intentionally carry a UTF-8 BOM and CRLF
endings, enforced by `.gitattributes`, because PowerShell 5.1 misreads BOM-less UTF-8 as
ANSI.

**Examples must use generic placeholders, never real client identifiers.** No client names,
tenant or subscription GUIDs, or account UPNs in any committed file. Use `fabric-clienta`,
`user@clientb.com`, `analytics_ws`, `<subscription-guid>`. This has been violated before by
adding examples to a doc after the initial scrub, so re-check the whole repo before
committing, not just the file you touched.

**Never commit `.venv/`.** It carries absolute paths from the machine that built it, and the
registered config points into it, so a copied venv yields a server that cannot start with no
error shown. `.gitignore` covers it; confirm with `git status` before committing.

## State of testing

Honest as of the last commit: `setup.ps1` has been exercised in dry-run, register, `-Auto`
(both the single-login and multi-login branches) and `-RemoveServer`.
`Register-PowerBIMcp.ps1` has been dry-run on both the existing-exe and npx paths, but never
applied. `diagnose.ps1` has not been run since its config-path resolution was fixed. The npm
package route has never been started on any machine. The whole install has only ever been
performed by its author, on the machine it was written for.
