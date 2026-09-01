# AGENT-INSTALL.md — install runbook for Claude

**This file is written for Claude, not for a person.** A human hands it over by saying
something like *"read AGENT-INSTALL.md and install this for me."* Nothing in this repo runs
on its own.

Human-facing instructions live in [INSTALL.md](INSTALL.md). If you are a person reading
this, that is the file you want.

---

## What cannot be automated

Three steps require the human, no matter which Claude is running this. Do not try to work
around them, and do not pretend they succeeded.

1. **`az login`** opens a browser and needs credentials plus MFA.
2. **Choosing which client identity to register** is a judgement call with audit
   consequences — every query runs under that account's name in that client's Fabric audit
   log. Never pick for them when there is more than one option.
3. **Restarting Claude Desktop from the system tray.** You cannot restart the application
   you may be running inside, and closing the window is not a restart.

Everything else — prerequisite checks, path resolution, running the installer, reading its
output, verifying the result, diagnosing failures — you can do.

## Rules that override convenience

- **Never invent or guess a subscription GUID, tenant GUID, or account name.** Read them
  from `az account list`. If the value you need is not there, stop and ask.
- **Never pass both `-Subscription` and `-TenantId`.** The Azure CLI refuses them together,
  and `-TenantId` is not an identity selector — it keeps the *active* account and asks it
  for another tenant's token, failing with `AADSTS90072`.
- **Never register an instance without `-Subscription`.** An unpinned entry silently
  follows whichever `az` account is active and will repoint to a different client later.
- **Never write to `%APPDATA%\Claude\claude_desktop_config.json`.** On an MSIX install the
  app reads a virtualized copy; writing the other path is a silent no-op. `setup.ps1`
  resolves this — do not hand-edit either file.
- **Never trust an instance name as evidence of identity.** Only `identity.upn` from
  `fabric_whoami`, which is decoded from the token itself, is evidence.
- **Report the UPN back to the human before they rely on it.** If it is not what they
  expected, treat the install as failed even though every command succeeded.
- **Stop rather than guess.** A wrong identity here means querying a client's production
  data under someone else's name.

---

# Part A — Claude Code in a Windows terminal

This is the surface where the install genuinely automates: you can execute PowerShell,
read its output, and act on it.

## A0. Invoke PowerShell explicitly

Your shell on Windows may be git-bash, cmd or PowerShell — do not assume. Call the script
through PowerShell every time:

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 <args>
```

`-ExecutionPolicy Bypass` matters: on a managed corporate machine an unsigned local script
is blocked by default, and the failure looks like a script error rather than a policy one.
If you see `cannot be loaded because running scripts is disabled`, that is policy — report
it and stop; the human may need IT to allow it.

## A1. Confirm you are in the right folder

```
powershell -NoProfile -Command "Test-Path .\setup.ps1, .\fabric_mcp_server.py"
```

Both must be `True`. If not, find the repo root before doing anything else.

## A2. Check for a copied virtual environment

```
powershell -NoProfile -Command "Test-Path .\.venv"
```

If `.venv` exists **and** the human obtained this folder as a zip or copy rather than a
`git clone`, delete it before continuing — a venv carries absolute paths from the machine
that built it, and the registered config points into it, so a copied one produces a server
that cannot start with no error shown anywhere. Ask before deleting; do not delete a venv
that `setup.ps1` built on this machine.

## A3. Dry run

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Read the output rather than skimming it. Stop and report if you see:

| Output | Meaning | Action |
|---|---|---|
| `[FAIL] python not on PATH` | Missing prerequisite | Stop. Human installs Python 3.10+ |
| `[FAIL] No Microsoft ODBC driver` | Missing prerequisite | Stop. Human installs 64-bit ODBC Driver 18 |
| `[warn] Azure CLI not on PATH` | No `az` | Stop. `-Auto` needs it, and without it every server start opens a browser |
| Smoke test shows a token `FAILED` | Auth problem, not an install problem | Diagnose before registering — see the failure playbook |

The dry run also prints the config file it *would* write. Confirm it is the
`...\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\...` path on an MSIX install.
If it prints `%APPDATA%`, the resolution logic misfired — stop and report.

## A4. Discover the identities

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Auto
```

Three outcomes:

- **No logins.** `-Auto` stops and tells them to run `az login`. Relay that, wait, retry.
  Do not attempt `az login` yourself — it needs their browser and MFA.
- **Exactly one login.** `-Auto` picks that subscription and derives an instance name from
  the account's domain (`abrams@huliot.com` → `fabric-huliot`). Show the human which
  identity it chose and get a yes before registering.
- **Several logins.** `-Auto` prints a numbered list with a ready-to-run command per
  identity and writes nothing. **Ask which client they want.** Register one at a time.

The `-Name` in those suggested commands is derived from the account's domain
(`baram-group.com` → `fabric-baram-group`) and is only a suggestion. Before running one,
check whether an instance for that identity already exists under a different name — with
`.\diagnose.ps1`, which lists every `fabric*` entry. If it does, reuse the existing name,
or you will create a second instance for the same account rather than updating the first.

## A5. Register

Claude Desktop must be fully quit first — the script refuses otherwise, deliberately. You
cannot quit it for them if you are running inside it; from a terminal, ask them to quit
from the tray and confirm.

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -RegisterClaude -Name fabric-<client> -Subscription <guid>
```

Then read the smoke test's `identity.upn` in the output. **Report it to the human before
they restart.** If it is not the account they named, the install is wrong even though the
command succeeded — say so plainly.

Repeat this step per client identity. Each is an independent instance.

## A6. Hand back for the restart

Tell them: quit Claude Desktop from the system tray, not the window, then reopen. Say why —
the app reads its config only at process start.

## A7. Verify after the restart

You cannot do this until they confirm the restart. Then ask them to run `fabric_whoami` on
each new instance, or run it yourself if you have the tools. Check all four:

- `identity.upn` — the expected account
- `identity.tenant_id` — the expected tenant
- `cli_selector` — should read `subscription=<guid> (picks the stored az login)`
- `fabric_api_token` and `sql_token` — both `ok`

Only after that is the install done. Say so, and say which identity each instance carries.

---

# Part B — Cowork / Claude Desktop with the folder connected

**Read this before starting: you cannot install from here.** The connected-folder bridge is
a Linux VM. It can read and write files in the folder, but it cannot run PowerShell, `az`,
`python.exe`, or any other Windows executable, and it cannot see
`%LOCALAPPDATA%\Packages\...` because that path is outside the connected folder.

Do not attempt `powershell`, `az`, or `.\setup.ps1` through the bridge. They will fail, and
retrying wastes the human's time. Say what you can and cannot do, up front.

## What you can genuinely do from here

**Pre-flight the repo.** Confirm the files are present and intact, read `README.md` for the
design constraints, and check for a copied `.venv` or stray `*.bak-*` files that need
removing. This catches the most common day-one failure before they start.

**Prepare exact commands.** You know the folder's real Windows path, so you can produce
commands with no placeholders left for them to fill in. Give them one block at a time and
wait for the output — do not dump the whole install and hope.

**Read their output and decide the next step.** This is where you add the most value. They
paste terminal output; you interpret it. That covers the whole failure playbook below.

**Inspect the result.** Once instances are registered and Claude Desktop has restarted, the
`fabric_*` tools appear in your own tool list, proxied from their machine. Call
`fabric_whoami` on each and check the identity yourself. You cannot read their config file,
but you can confirm the outcome that config produces — which is the thing that matters.

## The flow from here

1. Pre-flight the folder. Report anything that would block them.
2. Give them the identity-discovery command and ask for the output:
   ```powershell
   cd <the repo path>
   .\setup.ps1 -Auto
   ```
3. Read the output. If several identities, ask which client. If one, confirm it.
4. Give them the register command for the chosen identity, and tell them in the same
   message to quit Claude Desktop from the tray first. Ask them to paste the output —
   specifically the `identity.upn` line.
5. Check the UPN against what they expected. Say so explicitly.
6. Tell them to reopen Claude Desktop. **Your connection to their machine drops while it is
   closed** — say that, so it does not look like a fault.
7. When they are back, call `fabric_whoami` yourself on each instance and confirm all four
   fields from step A7.

Realistic human involvement: three commands pasted, one browser sign-in, one restart.

---

# Failure playbook

Applies to both parts. Ask for `.\diagnose.ps1` output when the cause is not obvious — it
resolves the correct config path, lists every `fabric*` entry, flags unpinned instances,
and import-tests the server.

| Symptom | Cause | Response |
|---|---|---|
| Tools never appear, nothing in any log | Server crashed on import; a server that dies before registering shows no error at all | Have them run `.\.venv\Scripts\python.exe fabric_mcp_server.py` directly. It should sit silently on stdin — any output is the traceback you need |
| Tools never appear, config looks correct | Wrong config file was written | Re-run `-RegisterClaude`; never hand-edit |
| `AADSTS90072` | `-TenantId` used as an identity selector | Re-register with `-Subscription` |
| `only one of subscription and tenant` | Both selectors passed | Drop `-TenantId` |
| `identity.upn` is the wrong account | Wrong subscription GUID, or the instance is unpinned | Re-register with the correct GUID. Treat as a failed install and say so |
| SQL 18456 `token-identified principal` | Token valid, no access to that item | Their workspace role, or the tenant setting allowing SQL endpoint access. Not an install fault |
| `No suitable ODBC driver found` | Driver 18 missing or 32-bit | Human installs it, then restarts Claude Desktop |
| `running scripts is disabled` | PowerShell execution policy | Use `-ExecutionPolicy Bypass`; if still blocked it is machine policy — stop |
| First call takes ~35 s | Copy predates the connection-reuse fix | Have them pull latest |

## When you are finished

Report, in this order: which instances exist, the UPN and tenant each one actually carries,
and anything you could not verify. If you skipped a verification step, say which — an
install reported as complete but unverified is worse than one reported as incomplete.
