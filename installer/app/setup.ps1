#Requires -Version 5.1
<#
.SYNOPSIS
    Installs, updates, repairs or removes SolidWorks-MCP for the current Windows user.

.DESCRIPTION
    Run by the SolidWorks-MCP installer, by the "Repair" Start menu shortcut and by
    launch.ps1. It never needs administrator rights. It:
      1. installs uv (https://docs.astral.sh/uv/) if it is missing,
      2. installs the solidworks-mcp server as a uv tool, pinned to the latest GitHub
         release (or to the latest commit of main when there is no release yet),
      3. installs Claude Code if it is missing,
      4. prepares the work folder: .mcp.json, CLAUDE.md, .claude\settings.json,
         solidworks-mcp.toml and projects\.
    Everything is logged to logs\setup.log next to this script.

.PARAMETER WorkspaceDir
    Work folder Claude Code opens. Default: Documents\SolidWorks-MCP.
.PARAMETER Channel
    'release' (default) follows GitHub releases; 'main' follows every commit.
.PARAMETER UpdateOnly
    Reinstall the server only if a newer version exists.
.PARAMETER Uninstall
    Remove the server tool. The work folder and your parts are kept.
.PARAMETER SkipClaude
    Do not install Claude Code.
.PARAMETER Pause
    Wait for Enter before closing the window.
.PARAMETER PauseOnError
    Wait for Enter only if something went wrong.
#>
[CmdletBinding()]
param(
    [string]$WorkspaceDir,
    [ValidateSet('release', 'main')][string]$Channel,
    [string]$Repo = 'MatteoBaisini/solidworks-mcp',
    [switch]$UpdateOnly,
    [switch]$Uninstall,
    [switch]$SkipClaude,
    [switch]$Pause,
    [switch]$PauseOnError
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$env:NO_COLOR = '1'          # plain uv output in the installer log
$env:UV_NO_PROGRESS = '1'
$PowerShellExe = (Get-Process -Id $PID).Path

New-Item -ItemType Directory -Force -Path $Script:LogDir | Out-Null
$transcribing = $false
try { Start-Transcript -Path (Join-Path $Script:LogDir 'setup.log') -Append | Out-Null; $transcribing = $true } catch { }

function Write-Step([string]$Message) { Write-Host "==> $Message" }
function Write-Note([string]$Message) { Write-Host "    $Message" }

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Native tools write progress to stderr; never let that become a terminating error.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" } }
    finally { $ErrorActionPreference = $saved }
    return $LASTEXITCODE
}

function Invoke-WebInstaller([string]$Url) {
    # The official one-line installers, each in its own PowerShell as their docs show.
    $command = "irm $Url | iex"
    return (Invoke-Native $PowerShellExe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command))
}

function Install-Uv {
    $uv = Find-Tool 'uv'
    if ($uv) { Write-Note "uv found: $uv"; return $uv }
    Write-Step 'Installing uv (Python package manager)'
    $code = Invoke-WebInstaller 'https://astral.sh/uv/install.ps1'
    Update-PathFromRegistry
    $uv = Find-Tool 'uv'
    if (-not $uv) { throw "uv could not be installed (exit code $code). Check the internet connection and run Repair." }
    Write-Note "uv installed: $uv"
    return $uv
}

function Install-Claude {
    $claude = Find-Tool 'claude'
    if ($claude) { Write-Note "Claude Code found: $claude"; return $claude }
    Write-Step 'Installing Claude Code'
    $code = Invoke-WebInstaller 'https://claude.ai/install.ps1'
    Update-PathFromRegistry
    $claude = Find-Tool 'claude'
    if (-not $claude) {
        Write-Note "Claude Code could not be installed (exit code $code)."
        Write-Note 'Install it from https://code.claude.com/docs/en/setup and run Repair.'
        return $null
    }
    Write-Note "Claude Code installed: $claude"
    return $claude
}

function Get-ServerExe([string]$Uv) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $bin = (& $Uv tool dir --bin 2>$null | Select-Object -Last 1) } finally { $ErrorActionPreference = $saved }
    if (-not $bin) { $bin = $Script:UserBin }
    return (Join-Path "$bin".Trim() "$($Script:ToolName).exe")
}

function Test-ServerRunning {
    return [bool](Get-Process -Name $Script:ToolName -ErrorAction SilentlyContinue)
}

function Install-Server([string]$Uv, $Source) {
    Write-Step "Installing the SolidWorks MCP server ($($Source.Label))"
    $spec = "$($Script:ToolName) @ $($Source.Url)"
    $code = Invoke-Native $Uv @('tool', 'install', '--force', '--python', '3.12', $spec)
    if ($code -ne 0) {
        if (Test-ServerRunning) { throw 'The server is in use: close Claude Code and run Repair.' }
        throw "uv could not install the server (exit code $code)."
    }
}

function Copy-IfMissing([string]$From, [string]$To) {
    if (Test-Path -LiteralPath $To) { Write-Note "kept      $To"; return }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $To) | Out-Null
    Copy-Item -LiteralPath $From -Destination $To
    Write-Note "created   $To"
}

function Initialize-Workspace([string]$Workspace, [string]$ServerExe) {
    Write-Step "Preparing the work folder $Workspace"
    $template = Join-Path $PSScriptRoot 'workspace'
    New-Item -ItemType Directory -Force -Path $Workspace, (Join-Path $Workspace 'projects') | Out-Null

    # Files the user may edit: written once, never overwritten.
    Copy-IfMissing (Join-Path $template 'CLAUDE.md') (Join-Path $Workspace 'CLAUDE.md')
    Copy-IfMissing (Join-Path $template 'README.md') (Join-Path $Workspace 'README.md')
    Copy-IfMissing (Join-Path $template '.claude\settings.json') (Join-Path $Workspace '.claude\settings.json')
    $toml = Join-Path $Workspace 'solidworks-mcp.toml'
    Copy-IfMissing (Join-Path $template 'solidworks-mcp.toml.in') $toml

    # .mcp.json is generated: absolute paths to this PC's server and settings.
    # Other servers the user added to it are preserved.
    $mcpPath = Join-Path $Workspace '.mcp.json'
    $doc = $null
    if (Test-Path -LiteralPath $mcpPath) {
        try { $doc = Read-Utf8 $mcpPath | ConvertFrom-Json }
        catch {
            Copy-Item -LiteralPath $mcpPath -Destination "$mcpPath.invalid" -Force
            Write-Note ".mcp.json was not valid JSON: saved as .mcp.json.invalid"
        }
    }
    if (-not $doc) { $doc = New-Object PSObject }
    if (-not ($doc.PSObject.Properties.Name -contains 'mcpServers') -or -not $doc.mcpServers) {
        $doc | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject) -Force
    }
    $entry = [pscustomobject][ordered]@{
        type    = 'stdio'
        command = $ServerExe
        args    = @('--config', $toml)
    }
    $doc.mcpServers | Add-Member -NotePropertyName 'solidworks' -NotePropertyValue $entry -Force
    Write-Utf8NoBom $mcpPath ($doc | ConvertTo-Json -Depth 10)
    Write-Note "updated   $mcpPath"
}

# --------------------------------------------------------------------------- #
$exitCode = 0
try {
    $state = Read-State
    if (-not $state) { $state = New-Object PSObject }
    if (-not $WorkspaceDir) { $WorkspaceDir = Get-StateValue $state 'workspace' }
    if (-not $WorkspaceDir) {
        $documents = [Environment]::GetFolderPath('MyDocuments')
        if (-not $documents) { $documents = Join-Path $env:USERPROFILE 'Documents' }
        $WorkspaceDir = Join-Path $documents $Script:AppName
    }
    $WorkspaceDir = [IO.Path]::GetFullPath($WorkspaceDir)
    if ($WorkspaceDir.Length -gt 3) { $WorkspaceDir = $WorkspaceDir.TrimEnd('\') }
    if (-not $Channel) { $Channel = Get-StateValue $state 'channel' 'release' }

    Write-Step "$($Script:AppName) setup - $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    Update-PathFromRegistry

    if ($Uninstall) {
        $uv = Find-Tool 'uv'
        if ($uv) {
            Write-Step 'Removing the SolidWorks MCP server'
            [void](Invoke-Native $uv @('tool', 'uninstall', $Script:ToolName))
        }
        Write-Note "Work folder kept: $WorkspaceDir"
    }
    elseif ($UpdateOnly) {
        $uv = Find-Tool 'uv'
        if (-not $uv) { throw 'uv is missing: run Repair.' }
        Write-Step 'Checking for a new version of the server'
        $source = Resolve-ServerSource $Repo $Channel
        $installed = Get-StateValue $state 'installedId'
        if (-not $source.Id) {
            Write-Note 'GitHub not reachable: keeping the installed version.'
        } elseif ($source.Id -eq $installed -and (Test-File (Get-StateValue $state 'server'))) {
            Write-Note "Up to date ($($source.Label))."
        } elseif (Test-ServerRunning) {
            Write-Note 'The server is in use by an open Claude Code window: update postponed.'
        } else {
            Install-Server $uv $source
            Set-StateValue $state 'installedId' $source.Id
            Set-StateValue $state 'installedLabel' $source.Label
            Write-Note "Updated to $($source.Label)."
        }
        Set-StateValue $state 'lastCheck' (Get-Date).ToUniversalTime().ToString('o')
        Save-State $state
    }
    else {
        $uv = Install-Uv
        $source = Resolve-ServerSource $Repo $Channel
        $serverExe = Get-ServerExe $uv
        if ((Test-ServerRunning) -and (Test-File $serverExe)) {
            Write-Note 'The server is in use by an open Claude Code window: keeping the installed version.'
        } else {
            Install-Server $uv $source
            Set-StateValue $state 'installedId' $source.Id
            Set-StateValue $state 'installedLabel' $source.Label
        }
        if (-not (Test-File $serverExe)) { throw "Server executable not found: $serverExe" }

        $claude = $null
        if (-not $SkipClaude) { $claude = Install-Claude }
        Initialize-Workspace $WorkspaceDir $serverExe

        Set-StateValue $state 'workspace' $WorkspaceDir
        Set-StateValue $state 'channel' $Channel
        Set-StateValue $state 'repo' $Repo
        Set-StateValue $state 'uv' $uv
        Set-StateValue $state 'server' $serverExe
        Set-StateValue $state 'claude' $claude
        Set-StateValue $state 'lastCheck' (Get-Date).ToUniversalTime().ToString('o')
        Save-State $state

        if (-not (Find-Tool 'git')) {
            Write-Note 'Optional: Git for Windows (https://git-scm.com/downloads/win) lets Claude Code use Bash.'
        }
        Write-Step 'Checking SolidWorks'
        [void](Invoke-Native $serverExe @('doctor'))
        Write-Step "Done. Open SolidWorks-MCP from the desktop or the Start menu."
    }
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)"
    Write-Host "Log: $(Join-Path $Script:LogDir 'setup.log')"
    $exitCode = 1
}
finally {
    if ($transcribing) { try { Stop-Transcript | Out-Null } catch { } }
}

if ($Pause -or ($PauseOnError -and $exitCode -ne 0)) { [void](Read-Host 'Press Enter to close') }
exit $exitCode
