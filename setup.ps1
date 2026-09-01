<#
    fabric-mcp setup (Windows PowerShell)

    Run from this folder:
        .\setup.ps1                    # check prerequisites, create venv, install deps
        .\setup.ps1 -RegisterClaude    # ...and register the server in Claude Desktop

    -RegisterClaude backs up claude_desktop_config.json before touching it.
#>
[CmdletBinding()]
param(
    [switch]$RegisterClaude,
    [string]$TenantId = "",
    [string]$Subscription = "",
    [string]$Name = "fabric",
    [switch]$RegisterFabricCore,
    [switch]$CoreOnly,
    [switch]$Auto,
    [string[]]$RemoveServer = @()
)

# -CoreOnly registers ONLY the remote fabric-core bridge and leaves the local server
# entries untouched. Without it, a -RegisterFabricCore run also rewrites the $Name
# entry -- and if -Subscription is not repeated on that same command line, the entry
# loses its identity selector and silently follows whichever az account is active.
if ($CoreOnly -and -not $RegisterFabricCore) {
    Write-Host "ABORT: -CoreOnly requires -RegisterFabricCore." -ForegroundColor Red
    return
}

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$py   = Join-Path $venv "Scripts\python.exe"

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [ok]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- prerequisites
Step "Prerequisites"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { Bad "python not on PATH. Install Python 3.10+ and re-run."; exit 1 }
$pyver = (& python -c "import sys;print('.'.join(map(str,sys.version_info[:3])))")
Ok "python $pyver at $($pythonCmd.Source)"

$az = Get-Command az -ErrorAction SilentlyContinue
if ($az) { Ok "Azure CLI found" } else { Warn "Azure CLI not on PATH. Install it, or the server will fall back to an interactive browser sign-in." }

# ODBC Driver 18 for SQL Server
$drivers = @()
try {
    $drivers = Get-OdbcDriver -Platform 64-bit -ErrorAction Stop | Select-Object -ExpandProperty Name
} catch {
    Warn "Could not enumerate ODBC drivers ($($_.Exception.Message))."
}
if ($drivers -contains "ODBC Driver 18 for SQL Server") {
    Ok "ODBC Driver 18 for SQL Server present"
} elseif ($drivers -contains "ODBC Driver 17 for SQL Server") {
    Warn "Only ODBC Driver 17 present. 18 is recommended: https://aka.ms/downloadmsodbcsql"
} else {
    Bad "No Microsoft ODBC driver for SQL Server found. Install ODBC Driver 18: https://aka.ms/downloadmsodbcsql"
}

# --------------------------------------------------------------------- venv
Step "Virtual environment"
if (-not (Test-Path $py)) {
    & python -m venv $venv
    Ok "created $venv"
} else {
    Ok "reusing $venv"
}
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $root "requirements.txt") --quiet
Ok "dependencies installed"

# --------------------------------------------------------------------- sign in
Step "Azure sign-in"
if ($az) {
    $acct = (& az account show 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Warn "Not signed in. Running 'az login'..."
        if ($TenantId) { & az login --tenant $TenantId | Out-Null } else { & az login | Out-Null }
    }
    $acct = (& az account show 2>$null | ConvertFrom-Json)
    if ($acct) { Ok "signed in as $($acct.user.name) (tenant $($acct.tenantId))" }
} else {
    Warn "Skipped - no Azure CLI."
}

# ------------------------------------------------------------------ -Auto
# Removes the GUID-hunting step. Enumerates the stored az logins and, when there is
# exactly one, registers against it without asking. With several, it prints the
# ready-to-run command for each and stops -- picking the client identity is a human
# decision, not a default worth guessing.
if ($Auto) {
    Step "Auto-detecting identity"

    if (-not $az) { Bad "-Auto needs the Azure CLI. Install it, or pass -Subscription yourself."; exit 1 }

    # A native command returns its output as an ARRAY OF LINES, and PowerShell 5.1's
    # ConvertFrom-Json emits a parsed JSON array as a SINGLE object instead of
    # enumerating it. Piping straight into @( ... ) therefore yields Count=1 holding an
    # array of every account, and member enumeration then joins their values together --
    # producing a "subscription id" made of two GUIDs. Join the lines, convert by
    # argument rather than by pipeline, and flatten one level defensively.
    $accountsRaw = (& az account list --all --query "[].{user:user.name, tenant:tenantId, sub:name, id:id}" -o json 2>$null) -join "`n"
    $accounts = @()
    if ($accountsRaw) {
        try {
            $parsed = ConvertFrom-Json $accountsRaw
            if ($null -ne $parsed) { $accounts = @($parsed) }
            if ($accounts.Count -eq 1 -and $accounts[0] -is [System.Collections.IEnumerable] -and $accounts[0] -isnot [string]) {
                $accounts = @($accounts[0])
            }
        } catch {
            Bad "could not parse 'az account list' output: $($_.Exception.Message)"
            exit 1
        }
    }

    if ($accounts.Count -eq 0) {
        Bad "No az logins found. Run 'az login' (add --tenant <guid> for a client tenant, and --allow-no-subscriptions if it grants none), then re-run."
        exit 1
    }

    function Get-DefaultName($upn) {
        # abrams@huliot.com -> fabric-huliot
        $domain = ($upn -split "@")[-1]
        $label  = ($domain -split "\.")[0]
        $label  = ($label -replace "[^a-zA-Z0-9-]", "").ToLower()
        if ($label) { return "fabric-$label" } else { return "fabric" }
    }

    if ($Subscription) {
        Warn "-Subscription was given explicitly; -Auto will not override it."
    }
    elseif ($accounts.Count -eq 1) {
        $only = $accounts[0]
        $Subscription = $only.id
        if (-not $PSBoundParameters.ContainsKey("Name")) { $Name = Get-DefaultName $only.user }
        Ok "one login found: $($only.user)"
        Ok "using subscription $($only.id) ($($only.sub)) as instance '$Name'"
    }
    else {
        Write-Host "`n  $($accounts.Count) logins found. Pick the identity you want and run one of these:`n" -ForegroundColor Yellow
        $i = 0
        foreach ($acct in $accounts) {
            $i++
            $suggested = Get-DefaultName $acct.user
            Write-Host ("  [{0}] {1}" -f $i, $acct.user)
            Write-Host ("      tenant {0}  subscription {1}" -f $acct.tenant, $acct.sub) -ForegroundColor DarkGray
            Write-Host ("      .\setup.ps1 -RegisterClaude -Name {0} -Subscription {1}" -f $suggested, $acct.id) -ForegroundColor Cyan
            Write-Host ""
        }
        Write-Host "  Register one per client you need. Nothing was written." -ForegroundColor Yellow
        exit 0
    }
}

# One malformed selector reaches Fabric as an unreadable "Subscription '...' not found",
# so fail here with a message that names the actual problem.
if ($Subscription -and $Subscription -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
    Bad "-Subscription must be a single subscription GUID. Got: '$Subscription'"
    Write-Host "  List them with:  az account list --all --query ""[].{user:user.name, id:id}"" -o table" -ForegroundColor Yellow
    exit 1
}

if ($TenantId -and $Subscription) {
    Warn "-TenantId and -Subscription are mutually exclusive to the Azure CLI. -Subscription wins for token acquisition (it selects WHICH stored az login to use); -TenantId is kept only for the browser fallback and for reporting."
}
if ($TenantId) {
    $matching = (& az account list --all --query "[?tenantId=='$TenantId'].user.name" -o tsv 2>$null)
    if ($matching) { Ok "az has a login in tenant $TenantId : $($matching -join ', ')" }
    else { Warn "no az login found for tenant $TenantId - the server will fall back to an interactive browser sign-in. Run: az login --tenant $TenantId" }
}

# --------------------------------------------------------------- smoke test
if (-not $CoreOnly) {
Step "Smoke test (fabric_whoami)"
$smoke = @"
import importlib.util, sys
spec = importlib.util.spec_from_file_location('fms', r'$($root)\fabric_mcp_server.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.fabric_whoami())
"@
# Clear first: these persist for the life of the PowerShell session, so a previous
# run in the same shell would otherwise leak its identity into this smoke test.
Remove-Item Env:\FABRIC_MCP_TENANT_ID       -ErrorAction SilentlyContinue
Remove-Item Env:\FABRIC_MCP_AZ_SUBSCRIPTION -ErrorAction SilentlyContinue
$env:FABRIC_MCP_INSTANCE = $Name
if ($TenantId)     { $env:FABRIC_MCP_TENANT_ID       = $TenantId }
if ($Subscription) { $env:FABRIC_MCP_AZ_SUBSCRIPTION = $Subscription }
$smokeFile = Join-Path $env:TEMP "fabric_mcp_smoke.py"
Set-Content -Path $smokeFile -Value $smoke -Encoding UTF8
& $py $smokeFile
Remove-Item $smokeFile -ErrorAction SilentlyContinue
} else {
    Step "Smoke test skipped (-CoreOnly: no local server involved)"
}

# ------------------------------------------------------------ Claude Desktop
$envBlock = [ordered]@{ FABRIC_MCP_INSTANCE = $Name }
if ($TenantId)     { $envBlock.FABRIC_MCP_TENANT_ID       = $TenantId }
if ($Subscription) { $envBlock.FABRIC_MCP_AZ_SUBSCRIPTION = $Subscription }

$serverEntry = [ordered]@{
    command = $py
    args    = @((Join-Path $root "fabric_mcp_server.py"))
    env     = $envBlock
}

# Fabric Core MCP is a remote HTTP server. It is registered through the mcp-remote
# stdio bridge rather than as a `url` entry: Claude Desktop has an open bug where a
# url-valued server entry can destroy claude_desktop_config.json, and this is the
# file that just cost a day to repair. mcp-remote caches its OAuth tokens per server
# under ~/.mcp-auth, so each tenant sign-in is kept separately.
# Resolve npx to a full path. On Windows the launcher is npx.cmd, and a bare "npx"
# spawned without a shell is not found -- the server would silently never register.
$npxCmd  = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $npxCmd) { $npxCmd = Get-Command npx -ErrorAction SilentlyContinue }
$npxPath = if ($npxCmd) { $npxCmd.Source } else { $null }

$coreEntry = [ordered]@{
    command = $npxPath
    args    = @("-y", "mcp-remote", "https://api.fabric.microsoft.com/v1/mcp/core",
                "--auth-timeout", "120")
}

# --- config path resolution -------------------------------------------------
# On an MSIX/Store install of Claude Desktop, %APPDATA%\Claude is a decoy: the app
# reads and writes a package-virtualized copy under LocalCache. Writing the decoy is
# a silent no-op -- the server never appears and Claude reports no error.
function Resolve-ClaudeConfigPath {
    $virt  = Join-Path $env:LOCALAPPDATA "Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"
    $plain = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"

    $isMsix = $false
    try { $isMsix = [bool](Get-AppxPackage -Name Claude -ErrorAction Stop) } catch { }

    if ($isMsix -or (Test-Path $virt)) {
        return [pscustomobject]@{ Path = $virt; Seed = $plain; Msix = $true }
    }
    return [pscustomobject]@{ Path = $plain; Seed = $null; Msix = $false }
}

$resolved = Resolve-ClaudeConfigPath
$cfgPath  = $resolved.Path

if ($RegisterClaude) {
    Step "Registering with Claude Desktop"

    if ($resolved.Msix) { Ok "MSIX install detected - targeting the virtualized config" }
    else                { Ok "non-packaged install - targeting %APPDATA%" }

    if (Get-Process -Name Claude -ErrorAction SilentlyContinue) {
        Bad "Claude Desktop is running. Quit it from the tray (not just the window) and re-run - the app can overwrite this write."
        exit 1
    }

    $cfgDir = Split-Path $cfgPath
    if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null }

    if (Test-Path $cfgPath) {
        $backup = "$cfgPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        Copy-Item $cfgPath $backup
        Ok "backed up to $backup"
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    } elseif ($resolved.Seed -and (Test-Path $resolved.Seed)) {
        # First write into the virtualized store. Seed from the real file, otherwise the
        # new stub would shadow it and every existing server and preference would vanish.
        Warn "no virtualized config yet - seeding from $($resolved.Seed) so nothing is lost"
        $cfg = Get-Content $resolved.Seed -Raw | ConvertFrom-Json
    } else {
        $cfg = [pscustomobject]@{}
    }

    if (-not $cfg.PSObject.Properties.Name.Contains("mcpServers")) {
        $cfg | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{})
    }
    foreach ($rm in $RemoveServer) {
        if ($cfg.mcpServers.PSObject.Properties[$rm]) {
            $cfg.mcpServers.PSObject.Properties.Remove($rm)
            Ok "removed server entry '$rm'"
        } else {
            Warn "no server entry named '$rm' to remove"
        }
    }
    if (-not $CoreOnly) {
        $cfg.mcpServers | Add-Member -MemberType NoteProperty -Name $Name -Value ([pscustomobject]$serverEntry) -Force
    }
    if ($RegisterFabricCore) {
        if (-not $npxPath) {
            Bad "npx not found on PATH. Install Node.js, or drop -RegisterFabricCore. Nothing was written."
            exit 1
        }
        $cfg.mcpServers | Add-Member -MemberType NoteProperty -Name "fabric-core" -Value ([pscustomobject]$coreEntry) -Force
        Ok "added 'fabric-core' via $npxPath (remote, mcp-remote bridge)"
    }

    # WriteAllText, not Set-Content: PS 5.1's -Encoding UTF8 prepends a BOM, which a
    # strict JSON parser rejects. Depth 100 because the preferences tree nests deeply.
    [System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 100))
    if (-not $CoreOnly) { Ok "wrote '$Name' server into $cfgPath" }
    Ok "servers now present: $((($cfg.mcpServers.PSObject.Properties.Name) -join ', '))"
    Write-Host "`nNow start Claude Desktop." -ForegroundColor Yellow
} else {
    Step "Claude Desktop config snippet"
    Write-Host "Target config for this install:`n  $cfgPath`n"
    Write-Host "Add this under `"mcpServers`":`n"
    $snippet = [ordered]@{ $Name = [pscustomobject]$serverEntry }
    if ($RegisterFabricCore) { $snippet["fabric-core"] = [pscustomobject]$coreEntry }
    ([pscustomobject]$snippet | ConvertTo-Json -Depth 100)
    Write-Host "`nOr re-run:  .\setup.ps1 -RegisterClaude" -ForegroundColor Yellow
}
