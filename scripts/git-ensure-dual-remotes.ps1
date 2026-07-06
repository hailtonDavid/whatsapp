#Requires -Version 5.1
<#
.SYNOPSIS
  Garante remotos origin (GitHub + push duplo) e gitea (Gitea local).
#>
[CmdletBinding()]
param([switch] $Quiet)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $RepoRoot

if (-not $env:GIT_CONFIG_COUNT) {
  $env:GIT_CONFIG_COUNT = 1
  $env:GIT_CONFIG_KEY_0 = "safe.directory"
  $env:GIT_CONFIG_VALUE_0 = $RepoRoot
}

$defaultGithub = "https://github.com/hailtonDavid/whatsapp.git"
$defaultGitea = "http://localhost:8030/hailtonDavid/whatsapp.git"

function W([string]$m) {
  if (-not $Quiet) { Write-Host $m -ForegroundColor Cyan }
}

$prev = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& git config --unset-all remote.origin.pushurl 2>&1 | Out-Null
$ErrorActionPreference = $prev

$names = @(& git remote 2>$null)
if ($names -contains "gitea") {
  & git remote set-url gitea $defaultGitea
} else {
  & git remote add gitea $defaultGitea
}

& git remote set-url origin $defaultGithub
& git remote set-url --add --push origin $defaultGithub
& git remote set-url --add --push origin $defaultGitea

W "[Git] origin: fetch GitHub; push GitHub + Gitea. Remote gitea OK."
