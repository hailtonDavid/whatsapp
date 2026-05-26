# DSM — executar testes no ambiente local (alinhado à detecção do DSM, v6)
# Uso: .\dsm-run-tests.ps1 -ProjectRoot "D:\\caminho\\do\\projeto"
# Revisão automática no DSM antes do download; versões guardadas em test_script_versions.
param(
  [Parameter(Mandatory = $false)]
  [string]$ProjectRoot = 'D:\\Sistemas\\whatsapp',
  [string]$OutFile = "dsm-test-results.json",
  [switch]$SkipAutoInstall,
  [switch]$Strict
)

$ErrorActionPreference = "Continue"
$ScriptVersion = "6"
$started = (Get-Date).ToString("o")
$items = @()
$notes = @("script_version=$ScriptVersion", "generator=dsm-external-test-script-v6")

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
  Write-Error "Pasta do projeto não encontrada: $ProjectRoot"
  exit 2
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Write-Host "Projeto: $root"

function Test-ProjPath([string]$rel) {
  return Test-Path -LiteralPath (Join-Path -Path $root -ChildPath $rel)
}

function Get-BootstrapPython {
  foreach ($c in @("py", "python")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
  }
  return $null
}

function Get-ProjectPython {
  foreach ($rel in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe")) {
    $p = Join-Path $root $rel
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return $null
}

function Ensure-ProjectVenv {
  $py = Get-ProjectPython
  if ($py) { return $py }
  $boot = Get-BootstrapPython
  if (-not $boot) { return $null }
  $venvDir = Join-Path $root "venv"
  if (-not (Test-Path -LiteralPath $venvDir)) {
    Write-Host "Criando ambiente virtual em venv\ ..."
    $created = Run-TestCmd @($boot, "-m", "venv", "venv")
    if ($created.returncode -ne 0) {
      Write-Warning "Falha ao criar venv; a usar Python do sistema."
      return $boot
    }
  }
  $venvPy = Join-Path $root "venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venvPy) { return $venvPy }
  return $boot
}

function Run-TestCmd {
  param([Parameter(Mandatory = $true)][string[]]$Command)
  if (-not $Command -or $Command.Count -eq 0) {
    return @{
      command = @("empty")
      returncode = -1
      stdout = ""
      stderr = "Comando vazio"
      elapsed_ms = 0
    }
  }
  $exe = $Command[0]
  $args = @()
  if ($Command.Length -gt 1) { $args = $Command[1..($Command.Length - 1)] }
  $outFile = Join-Path $env:TEMP ("dsm-test-out-" + [guid]::NewGuid().ToString("n") + ".txt")
  $errFile = Join-Path $env:TEMP ("dsm-test-err-" + [guid]::NewGuid().ToString("n") + ".txt")
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $err = ""
  try {
    $p = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $root `
      -NoNewWindow -PassThru -Wait `
      -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $code = $p.ExitCode
  } catch {
    $code = -1
    $err = $_.Exception.Message
  }
  $sw.Stop()
  $out = ""
  if ($outFile -and (Test-Path -LiteralPath $outFile)) { $out = Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue }
  if ($errFile -and (Test-Path -LiteralPath $errFile)) {
    $err = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $outFile,$errFile -Force -ErrorAction SilentlyContinue
  $outS = ($out | Out-String)
  $errS = ($err | Out-String)
  return @{
    command = $Command
    returncode = $code
    stdout = $outS.Substring(0, [Math]::Min(200000, $outS.Length))
    stderr = $errS.Substring(0, [Math]::Min(80000, $errS.Length))
    elapsed_ms = [int]$sw.ElapsedMilliseconds
  }
}

function Get-VenvTool([string]$tool) {
  foreach ($rel in @(".venv\Scripts\$tool.exe", "venv\Scripts\$tool.exe")) {
    $p = Join-Path $root $rel
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return $null
}

function Resolve-PlannedCommand {
  param($step)
  if (-not $step) { return $null }
  $resolver = [string]$step.resolver
  switch ($resolver) {
    "project_python" {
      $py = Get-ProjectPython
      if (-not $py) { $py = Ensure-ProjectVenv }
      if (-not $py) { return $null }
      $ma = @($step.module_args)
      return @($py) + $ma
    }
    "pytest" {
      $pt = Get-VenvTool "pytest"
      if ($pt) { return @($pt) + @($step.args) }
      $py = Get-ProjectPython
      if (-not $py) { $py = Ensure-ProjectVenv }
      if ($py) { return @($py, "-m", "pytest") + @($step.args) }
      return $null
    }
    "ruff" {
      $rf = Get-VenvTool "ruff"
      if ($rf) { return @($rf) + @($step.args) }
      if (Get-Command ruff -ErrorAction SilentlyContinue) { return @("ruff") + @($step.args) }
      $py = Get-ProjectPython
      if (-not $py) { $py = Ensure-ProjectVenv }
      if ($py) { return @($py, "-m", "ruff") + @($step.args) }
      return $null
    }
    "plain" {
      return @($step.argv)
    }
    default { return $null }
  }
}

$planJson = @'
{
  "generator_version": 6,
  "producer": "dsm-external-test-script-v6",
  "full_suite": true,
  "packages_to_install": ["pytest", "ruff", "coverage", "mypy", "radon"],
  "run_steps": [
    {
      "resolver": "project_python",
      "label": "coverage run -m pytest -q --maxfail=5",
      "module_args": ["-m", "coverage", "run", "-m", "pytest", "-q", "--maxfail=5"]
    },
    {
      "resolver": "project_python",
      "label": "coverage report -m",
      "module_args": ["-m", "coverage", "report", "-m"]
    },
    {
      "resolver": "ruff",
      "label": "ruff check .",
      "args": ["check", "."]
    },
    {
      "resolver": "project_python",
      "label": "mypy .",
      "module_args": ["-m", "mypy", "."]
    },
    {
      "resolver": "project_python",
      "label": "radon cc -s -n B src",
      "module_args": ["-m", "radon", "cc", "-s", "-n", "B", "src"]
    }
  ],
  "command_labels": [
    "coverage run -m pytest -q --maxfail=5",
    "coverage report -m",
    "ruff check .",
    "mypy .",
    "radon cc -s -n B src"
  ],
  "review_ok": true
}
'@
$plan = $planJson | ConvertFrom-Json
$notes += "plan_steps=$($plan.run_steps.Count)"

if (-not $SkipAutoInstall) {
  $py = Ensure-ProjectVenv
  if ($py) {
    $notes += "deps_auto_install=true"
    $items += Run-TestCmd @($py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "pip", "wheel")
    if (Test-ProjPath "requirements.txt") {
      $items += Run-TestCmd @($py, "-m", "pip", "install", "-q", "-r", "requirements.txt")
    }
    if (Test-ProjPath "requirements-dev.txt") {
      $items += Run-TestCmd @($py, "-m", "pip", "install", "-q", "-r", "requirements-dev.txt")
    }
    $pkgs = @($plan.packages_to_install)
    if ($pkgs.Count -gt 0) {
      $pipCmd = @($py, "-m", "pip", "install", "-q") + $pkgs
      $items += Run-TestCmd $pipCmd
    }
  } else {
    $notes += "python_missing=true"
  }
}

foreach ($step in $plan.run_steps) {
  $cmd = Resolve-PlannedCommand $step
  $lab = [string]$step.label
  if (-not $cmd) {
    $msg = "Nao foi possivel resolver: $lab"
    Write-Warning $msg
    $items += @{
      command = @("unresolved")
      returncode = -127
      stdout = ""
      stderr = $msg
      elapsed_ms = 0
    }
    if ($Strict) { break }
    continue
  }
  Write-Host "Executando: $($cmd -join ' ')"
  $items += Run-TestCmd $cmd
}

if ($items.Count -eq 0) {
  $notes += "no_runner_executed"
  $items += @{
    command = @("none")
    returncode = -1
    stdout = ""
    stderr = "Nenhum comando executado. Atualize o script no DSM (versao 6) e confira a pasta do projeto."
    elapsed_ms = 0
  }
}

$failed = @($items | Where-Object { $_.returncode -ne 0 }).Count
$report = @{
  producer = "dsm-external-test-script-v6"
  script_version = $ScriptVersion
  project_name = "WhatsApp"
  project_root = $root
  root_hint_container = "/mnt/d/Sistemas/whatsapp"
  started_at = $started
  detection_notes = ($notes -join "; ")
  planned_commands = @($plan.command_labels)
  results = @($items)
  stats = @{
    total = $items.Count
    failed = $failed
    passed = [Math]::Max(0, $items.Count - $failed)
  }
}
$json = $report | ConvertTo-Json -Depth 12 -Compress:$false
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$outPath = Join-Path (Get-Location) $OutFile
[System.IO.File]::WriteAllText($outPath, $json, $utf8NoBom)
Write-Host ""
Write-Host "Relatorio salvo em $outPath"
Write-Host "Comandos: $($items.Count) | Falhas: $failed"
if ($Strict -and $failed -gt 0) { exit 1 }
foreach ($r in $items) {
  $cmd = ($r.command -join ' ')
  Write-Host ("  [0] {1} ({2} ms)" -f $r.returncode, $cmd, $r.elapsed_ms)
}
