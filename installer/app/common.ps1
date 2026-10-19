# Shared helpers for setup.ps1 and launch.ps1 (dot-sourced, never run on its own).
# Windows PowerShell 5.1 compatible. ASCII only, so no byte-order mark is needed.

$Script:AppName   = 'SolidWorks-MCP'
$Script:ToolName  = 'solidworks-mcp'
$Script:UserBin   = Join-Path $env:USERPROFILE '.local\bin'
$Script:StateFile = Join-Path $PSScriptRoot 'state.json'
$Script:LogDir    = Join-Path $PSScriptRoot 'logs'

# TLS 1.2 for GitHub on older Windows 10 builds; faster downloads without the progress bar.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    # Claude Code (JSON) and Python's tomllib both reject a byte-order mark.
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Read-Utf8([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Read-State {
    if (-not (Test-Path -LiteralPath $Script:StateFile)) { return $null }
    try { return (Read-Utf8 $Script:StateFile | ConvertFrom-Json) } catch { return $null }
}

function Save-State($State) {
    Write-Utf8NoBom $Script:StateFile ($State | ConvertTo-Json -Depth 5)
}

function Get-StateValue($State, [string]$Name, $Default = $null) {
    if ($State -and ($State.PSObject.Properties.Name -contains $Name) -and $null -ne $State.$Name -and "$($State.$Name)" -ne '') {
        return $State.$Name
    }
    return $Default
}

function Set-StateValue($State, [string]$Name, $Value) {
    $State | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Update-PathFromRegistry {
    # Installers launched as child processes add to the user PATH in the registry only.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($Script:UserBin, $user, $machine) | Where-Object { $_ }) -join ';'
}

function Test-File([string]$Path) {
    return [bool]($Path -and (Test-Path -LiteralPath $Path))
}

function Find-Tool([string]$Name) {
    $local = Join-Path $Script:UserBin "$Name.exe"
    if (Test-Path -LiteralPath $local) { return $local }
    $cmd = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function ConvertTo-Arg([string]$Value) {
    # Quote one command-line argument for Start-Process (Windows PowerShell joins without quoting).
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Resolve-ServerSource([string]$Repo, [string]$Channel, [int]$TimeoutSec = 15) {
    # Returns Id (what is installed, used to detect updates), Url (archive uv installs) and Label.
    # Archives need no Git on the PC, and a pinned URL lets uv cache it correctly.
    $headers = @{ 'User-Agent' = 'SolidWorks-MCP-installer'; 'Accept' = 'application/vnd.github+json' }
    if ($Channel -ne 'main') {
        try {
            $rel = Invoke-RestMethod -UseBasicParsing -TimeoutSec $TimeoutSec -Headers $headers `
                -Uri "https://api.github.com/repos/$Repo/releases/latest"
            if ($rel.tag_name) {
                return [pscustomobject]@{
                    Id    = "tag:$($rel.tag_name)"
                    Url   = "https://github.com/$Repo/archive/refs/tags/$($rel.tag_name).zip"
                    Label = "$($rel.tag_name)"
                }
            }
        } catch {
            # No published release yet (404) or offline: fall back to the main branch.
        }
    }
    try {
        $commit = Invoke-RestMethod -UseBasicParsing -TimeoutSec $TimeoutSec -Headers $headers `
            -Uri "https://api.github.com/repos/$Repo/commits/main"
        $sha = "$($commit.sha)"
        if ($sha -match '^[0-9a-f]{40}$') {
            return [pscustomobject]@{
                Id    = "commit:$sha"
                Url   = "https://github.com/$Repo/archive/$sha.zip"
                Label = "main@$($sha.Substring(0, 7))"
            }
        }
    } catch {
        # GitHub API unreachable or rate-limited.
    }
    return [pscustomobject]@{ Id = $null; Url = "https://github.com/$Repo/archive/refs/heads/main.zip"; Label = 'main' }
}
