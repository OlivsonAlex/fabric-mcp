<#
    Register-PowerBIMcp.ps1

    Registers Microsoft's Power BI Modeling MCP server with Claude Desktop as TWO
    entries, split by capability:

        powerbi-mcp-server   --readonly     asking questions, reading DAX, validating
        powerbi-mcp-write    --readwrite    deliberate model edits

    Why two: the server defaults to read-write. An entry innocently named like a
    reader can create, update and delete measures, tables, relationships and
    security roles on PUBLISHED semantic models. Two entries make writing to a
    client's model a choice of tool rather than a choice of phrasing.

    Known limit: --readonly does NOT remove the write operations from the tool
    schema the server advertises. The refusal is at runtime. Treat the split as a
    speed bump, not a capability boundary.

    RUN WITH CLAUDE DESKTOP FULLY QUIT (no tray icon).

        .\Register-PowerBIMcp.ps1 -DryRun
        .\Register-PowerBIMcp.ps1                        # auto-detect exe, else npx
        .\Register-PowerBIMcp.ps1 -UseNpx                # force the npm route
        .\Register-PowerBIMcp.ps1 -ExePath "C:\...\powerbi-modeling-mcp.exe"
        .\Register-PowerBIMcp.ps1 -NoWriter              # reader only
#>
param(
  [string]$ExePath,
  [switch]$UseNpx,
  [string]$PackageVersion = "0.5.0-beta.13",
  [string]$ReaderName = "powerbi-mcp-server",
  [string]$WriterName = "powerbi-mcp-write",
  [switch]$NoWriter,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red }

# On an MSIX/Store install the app reads a package-virtualized copy, NOT %APPDATA%.
function Resolve-ClaudeConfigPath {
    $virt  = Join-Path $env:LOCALAPPDATA "Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"
    $plain = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
    $isMsix = $false
    try { $isMsix = [bool](Get-AppxPackage -Name Claude -ErrorAction Stop) } catch { }
    if ($isMsix -or (Test-Path $virt)) { return $virt }
    return $plain
}

$cfgPath = Resolve-ClaudeConfigPath
Step "Claude Desktop config"
Write-Host "  path: $cfgPath"
if (-not (Test-Path $cfgPath)) { Bad "config not found - start Claude Desktop once, then re-run."; return }

$claudeUp = [bool](Get-Process -Name Claude -ErrorAction SilentlyContinue)
Write-Host "  Claude Desktop running: $claudeUp"
if ($claudeUp -and -not $DryRun) {
    Bad "quit Claude Desktop from the tray first - it can overwrite this write."
    return
}

$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
if (-not $cfg.PSObject.Properties['mcpServers']) {
    $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
}

# ------------------------------------------------------------------ how to launch
# Flag spelling: the alias forms --readonly / --readwrite are accepted by both the
# VSIX build and the npm package. The hyphenated --read-only / --read-write exist
# on some builds only, so prefer the aliases for portability.
Step "Launch method"

$resolvedExe = $null
if ($ExePath) {
    if (-not (Test-Path $ExePath)) { Bad "-ExePath not found: $ExePath"; return }
    $resolvedExe = $ExePath
}
elseif (-not $UseNpx) {
    # reuse whatever an existing entry points at, then the conventional location
    $existing = $cfg.mcpServers.PSObject.Properties[$ReaderName]
    if ($existing -and $existing.Value.command -and (Test-Path $existing.Value.command)) {
        $resolvedExe = $existing.Value.command
        Ok "reusing the exe already registered"
    }
    else {
        $conventional = "C:\MCPServers\PowerBIModelingMCP\extension\server\powerbi-modeling-mcp.exe"
        if (Test-Path $conventional) { $resolvedExe = $conventional; Ok "found the exe at the conventional path" }
    }
}

if ($resolvedExe) {
    Ok "exe: $resolvedExe"
    $readerCmd = $resolvedExe; $readerArgs = @("--readonly")
    $writerCmd = $resolvedExe; $writerArgs = @("--readwrite")
}
else {
    # npm route - no download, but needs Node and network on first run
    $npxCmd = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if (-not $npxCmd) { $npxCmd = Get-Command npx -ErrorAction SilentlyContinue }
    if (-not $npxCmd) {
        Bad "no exe found and npx is not on PATH. Install Node.js, or pass -ExePath."
        return
    }
    # A bare "npx" is not found when spawned without a shell on Windows; use the full path.
    $pkg = "@microsoft/powerbi-modeling-mcp@$PackageVersion"
    Ok "npx: $($npxCmd.Source)"
    Ok "package: $pkg  (pinned - a pre-release, so do not use @latest)"
    $readerCmd = $npxCmd.Source; $readerArgs = @("-y", $pkg, "--readonly")
    $writerCmd = $npxCmd.Source; $writerArgs = @("-y", $pkg, "--readwrite")
}

# ------------------------------------------------------------------ write entries
Step "Registering"
$cfg.mcpServers | Add-Member -NotePropertyName $ReaderName `
    -NotePropertyValue ([pscustomobject]@{ command = $readerCmd; args = $readerArgs }) -Force
Ok "$ReaderName  ->  $($readerArgs -join ' ')"

if (-not $NoWriter) {
    $cfg.mcpServers | Add-Member -NotePropertyName $WriterName `
        -NotePropertyValue ([pscustomobject]@{ command = $writerCmd; args = $writerArgs }) -Force
    Ok "$WriterName  ->  $($writerArgs -join ' ')"
} else {
    Warn "-NoWriter: no read-write entry registered"
}

Write-Host "`n  servers: $(($cfg.mcpServers.PSObject.Properties.Name) -join ', ')"

if ($DryRun) { Write-Host "`nDryRun: nothing written." -ForegroundColor Cyan; return }

$stamp = Get-Date -Format yyyyMMdd-HHmmss
Copy-Item $cfgPath "$cfgPath.bak-$stamp"
[System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 100))
Write-Host "`nwritten.  backup: $cfgPath.bak-$stamp"
Write-Host "Start Claude Desktop, then connect to a model - see POWERBI-MCP.md." -ForegroundColor Yellow
