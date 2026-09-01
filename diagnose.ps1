<#
    fabric-mcp diagnostics — answers the three questions that matter when the
    server does not show up in Claude:

        1. Is a "fabric" entry actually in claude_desktop_config.json?
        2. Does the entry point at a python.exe that exists?
        3. Does the server import and register its tools without crashing?

    Run from this folder:   .\diagnose.ps1
    Paste the whole output back to Claude.
#>
[CmdletBinding()]
param()

$root = $PSScriptRoot
$py   = Join-Path $root ".venv\Scripts\python.exe"
$srv  = Join-Path $root "fabric_mcp_server.py"
# On an MSIX/Store install the app reads a package-virtualized copy, NOT %APPDATA%.
# Checking the wrong file reports "entry NOT FOUND" for a correct installation.
function Resolve-ClaudeConfigPath {
    $virt  = Join-Path $env:LOCALAPPDATA "Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"
    $plain = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
    $isMsix = $false
    try { $isMsix = [bool](Get-AppxPackage -Name Claude -ErrorAction Stop) } catch { }
    if ($isMsix -or (Test-Path $virt)) { return $virt }
    return $plain
}
$cfg     = Resolve-ClaudeConfigPath
$isMsix  = $cfg -like "*\Packages\Claude_*"
$logDir  = Join-Path (Split-Path $cfg) "logs"

function Head($t) { Write-Host "`n===== $t" -ForegroundColor Cyan }

Head "1. Claude Desktop config"
Write-Host "path: $cfg"
Write-Host "install type: $(if ($isMsix) { 'MSIX/Store (virtualized path)' } else { 'non-packaged (%APPDATA%)' })"
if (-not (Test-Path $cfg)) {
    Write-Host "  MISSING - Claude Desktop has never written a config here." -ForegroundColor Red
} else {
    $raw = Get-Content $cfg -Raw
    Write-Host "  size: $($raw.Length) bytes, modified $((Get-Item $cfg).LastWriteTime)"
    try {
        $j = $raw | ConvertFrom-Json
        $names = @()
        if ($j.PSObject.Properties.Name -contains "mcpServers") {
            $names = $j.mcpServers.PSObject.Properties.Name
        }
        Write-Host "  mcpServers present: $($names -join ', ')"
        # Instances are commonly named per identity (fabric-clienta, fabric-clientb, ...)
        $fabricNames = @($names | Where-Object { $_ -like "fabric*" })
        if ($fabricNames.Count -gt 0) {
            Write-Host "  fabric entries FOUND: $($fabricNames -join ', ')" -ForegroundColor Green
            foreach ($n in $fabricNames) {
                Write-Host "  --- $n"
                $j.mcpServers.$n | ConvertTo-Json -Depth 8 | Write-Host
                $sel = $j.mcpServers.$n.env.FABRIC_MCP_AZ_SUBSCRIPTION
                if (-not $sel) {
                    Write-Host "      WARNING: no FABRIC_MCP_AZ_SUBSCRIPTION - this instance follows whichever az account is active" -ForegroundColor Yellow
                }
            }
        } else {
            Write-Host "  no fabric* entry found  <-- this is very likely the problem" -ForegroundColor Red
            Write-Host "  Fix:  .\setup.ps1 -RegisterClaude -Name fabric-<client> -Subscription <subscription-guid>"
        }
    } catch {
        Write-Host "  config is not valid JSON: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Head "2. Backups left by setup.ps1 -RegisterClaude"
$baks = Get-ChildItem (Split-Path $cfg) -Filter "claude_desktop_config.json.bak-*" -ErrorAction SilentlyContinue
if ($baks) { $baks | Select-Object Name, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host }
else { Write-Host "  none - so -RegisterClaude has never run successfully" -ForegroundColor Yellow }

Head "3. Interpreter and script"
Write-Host "python: $py  exists=$(Test-Path $py)"
Write-Host "script: $srv  exists=$(Test-Path $srv)"
if (Test-Path $py) { & $py -c "import sys;print('  version:',sys.version)" }

Head "4. Import + tool registration test"
$probe = @'
import asyncio, importlib.util, importlib.metadata as md, sys, traceback
print("  mcp SDK:", md.version("mcp"))
try:
    spec = importlib.util.spec_from_file_location("fms", sys.argv[1])
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    tools = asyncio.run(m.app.list_tools())
    print("  IMPORT OK -", len(tools), "tools:", ", ".join(t.name for t in tools))
except Exception:
    print("  IMPORT FAILED:")
    traceback.print_exc()
    sys.exit(1)
'@
$probeFile = Join-Path $env:TEMP "fabric_mcp_probe.py"
Set-Content -Path $probeFile -Value $probe -Encoding UTF8
if (Test-Path $py) { & $py $probeFile $srv }
Remove-Item $probeFile -ErrorAction SilentlyContinue

Head "5. Auth + ODBC (fabric_whoami)"
$who = @'
import importlib.util, sys, traceback
try:
    spec = importlib.util.spec_from_file_location("fms", sys.argv[1])
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    print(m.fabric_whoami())
except Exception:
    traceback.print_exc()
'@
$whoFile = Join-Path $env:TEMP "fabric_mcp_who.py"
Set-Content -Path $whoFile -Value $who -Encoding UTF8
if (Test-Path $py) { & $py $whoFile $srv }
Remove-Item $whoFile -ErrorAction SilentlyContinue

Head "6. Newest Claude MCP logs"
# $logDir was resolved alongside $cfg so it follows the MSIX path too.
if (Test-Path $logDir) {
    Get-ChildItem $logDir -Filter "*mcp*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 4 |
        ForEach-Object {
            Write-Host "`n--- $($_.Name)  ($($_.LastWriteTime))" -ForegroundColor DarkGray
            Get-Content $_.FullName -Tail 25
        }
} else {
    Write-Host "  no log folder at $logDir"
}

Write-Host "`n===== done. Paste everything above back to Claude." -ForegroundColor Cyan
