#Requires -Version 5.1
<#
.SYNOPSIS
    Opens Claude Code in the SolidWorks-MCP work folder.

.DESCRIPTION
    Target of the desktop and Start menu shortcuts. It:
      1. repairs the installation if the server, Claude Code or the work folder is missing,
      2. once a day, checks GitHub for a new server version and installs it
         (only while no Claude Code window is using the server),
      3. opens Claude Code in the work folder, in Windows Terminal when available.
    Claude Code starts the SolidWorks MCP server itself from the folder's .mcp.json,
    and asks you to log in the first time.

.PARAMETER NoUpdate
    Skip the daily update check.
#>
[CmdletBinding()]
param([switch]$NoUpdate)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$PowerShellExe = (Get-Process -Id $PID).Path
$SetupScript = Join-Path $PSScriptRoot 'setup.ps1'
$DefaultRepo = 'MatteoBaisini/solidworks-mcp'

function Show-Message([string]$Text, [int]$Icon = 16) {
    try { [void](New-Object -ComObject WScript.Shell).Popup($Text, 0, $Script:AppName, $Icon) }
    catch { Write-Host $Text }
}

function Invoke-Setup([string[]]$Extra) {
    # Visible window, so the user sees what is being installed.
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (ConvertTo-Arg $SetupScript)) + $Extra
    $process = Start-Process -FilePath $PowerShellExe -ArgumentList $arguments -Wait -PassThru
    return $process.ExitCode
}

function Test-UpdateDue($State) {
    $last = Get-StateValue $State 'lastCheck'
    if (-not $last) { return $true }
    try {
        if ($last -is [DateTime]) { $when = $last.ToUniversalTime() }
        else {
            $when = [DateTime]::Parse("$last", [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        }
        return ((Get-Date).ToUniversalTime() - $when).TotalHours -ge 20
    } catch { return $true }
}

function Test-Ready($State) {
    if (-not $State) { return $false }
    $workspace = Get-StateValue $State 'workspace'
    return ((Test-File (Get-StateValue $State 'server')) -and (Find-Tool 'claude') -and
            $workspace -and (Test-File (Join-Path $workspace '.mcp.json')))
}

try {
    $state = Read-State

    if (-not (Test-Ready $state)) {
        $extra = @('-PauseOnError')
        $workspace = Get-StateValue $state 'workspace'
        if ($workspace) { $extra += @('-WorkspaceDir', (ConvertTo-Arg $workspace)) }
        $code = Invoke-Setup $extra
        $state = Read-State
        if ($code -ne 0 -or -not (Test-Ready $state)) {
            Show-Message ("SolidWorks-MCP is not ready. Check the log:`n" + (Join-Path $Script:LogDir 'setup.log'))
            exit 1
        }
    }
    elseif (-not $NoUpdate -and (Test-UpdateDue $state) -and -not (Get-Process -Name $Script:ToolName -ErrorAction SilentlyContinue)) {
        $source = Resolve-ServerSource (Get-StateValue $state 'repo' $DefaultRepo) (Get-StateValue $state 'channel' 'release') 5
        if ($source.Id -and $source.Id -ne (Get-StateValue $state 'installedId')) {
            [void](Invoke-Setup @('-UpdateOnly', '-PauseOnError'))
            $state = Read-State
        } else {
            Set-StateValue $state 'lastCheck' (Get-Date).ToUniversalTime().ToString('o')
            Save-State $state
        }
    }

    $claude = Find-Tool 'claude'
    $workspace = Get-StateValue $state 'workspace'
    $terminal = Get-Command 'wt.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($terminal) {
        Start-Process -FilePath $terminal.Source -ArgumentList @(
            '-d', (ConvertTo-Arg $workspace), '--title', $Script:AppName, (ConvertTo-Arg $claude))
    } else {
        Start-Process -FilePath $claude -WorkingDirectory $workspace
    }
}
catch {
    Show-Message "SolidWorks-MCP could not start:`n$($_.Exception.Message)"
    exit 1
}
