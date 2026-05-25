# DSM — executar testes no ambiente local e gerar JSON para upload no GestorSistema.
# Uso: .\dsm-run-tests.ps1 -ProjectRoot "D:\\caminho\\do\\projeto"
param(
  [Parameter(Mandatory = $false)]
  [string]$ProjectRoot = "D:\\Sistemas\\whatsapp",
  [string]$OutFile = "dsm-test-results.json",
  [switch]$SkipAutoInstall
)

$ErrorActionPreference = "Continue"
$ScriptVersion = "4"
$started = (Get-Date).ToString("o")
$items = @()
$notes = @("script_version=$ScriptVersion")

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
  Write-Error "Pasta do projeto não encontrada: $ProjectRoot"
  Write-Host "Use: .\dsm-run-tests.ps1 -ProjectRoot 'D:\\Sistemas\\GestorSistema'"
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

function Install-ProjectTestDeps {
  param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [bool]$FullSuite = $false
  )
  $out = @()
  Write-Host "A instalar dependencias de teste (pip)..."
  $out += Run-TestCmd @($PythonExe, "-m", "pip", "install", "-q", "--disable-pip-version-check", "pip", "wheel")
  if (Test-ProjPath "requirements.txt") {
    $out += Run-TestCmd @($PythonExe, "-m", "pip", "install", "-q", "-r", "requirements.txt")
  }
  if (Test-ProjPath "requirements-dev.txt") {
    $out += Run-TestCmd @($PythonExe, "-m", "pip", "install", "-q", "-r", "requirements-dev.txt")
  }
  $pkgs = @("pytest", "ruff")
  if ($FullSuite) { $pkgs += @("coverage", "mypy") }
  $pipCmd = @($PythonExe, "-m", "pip", "install", "-q") + $pkgs
  $out += Run-TestCmd $pipCmd
  return $out
}

function Test-PythonRepoLayout {
  if ((Test-ProjPath "tests") -or (Test-ProjPath "pytest.ini") -or (Test-ProjPath "pyproject.toml") -or (Test-ProjPath "conftest.py")) {
    return $true
  }
  if ((Test-ProjPath "requirements.txt") -or (Test-ProjPath "setup.py") -or (Test-ProjPath "src")) {
    return $true
  }
  $py = Get-ChildItem -LiteralPath $root -Filter "*.py" -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\(\.git|\.venv|venv|node_modules|profile_|exports)\\' } |
    Select-Object -First 1
  return ($null -ne $py)
}

function Run-TestCmd {
  param([Parameter(Mandatory = $true)][string[]]$Command)
  $exe = $Command[0]
  $args = @()
  if ($Command.Length -gt 1) {
    $args = $Command[1..($Command.Length - 1)]
  }
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
  if (-not $err) { $err = "" }
  $out = ""
  if ($outFile -and (Test-Path -LiteralPath $outFile)) { $out = Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue }
  if ($errFile -and (Test-Path -LiteralPath $errFile)) { $err = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue }
  Remove-Item -LiteralPath $outFile,$errFile -Force -ErrorAction SilentlyContinue
  return @{
    command = $Command
    returncode = $code
    stdout = ($out | Out-String).Substring(0, [Math]::Min(200000, ($out | Out-String).Length))
    stderr = ($err | Out-String).Substring(0, [Math]::Min(80000, ($err | Out-String).Length))
    elapsed_ms = [int]$sw.ElapsedMilliseconds
  }
}

function Get-VenvPytest {
  foreach ($rel in @(".venv\Scripts\pytest.exe", "venv\Scripts\pytest.exe")) {
    $p = Join-Path $root $rel
    if (Test-Path -LiteralPath $p) { return $p }
  }
  return $null
}

function Invoke-RuffCheck {
  param([string]$PythonExe = "")
  $py = $PythonExe
  if (-not $py) { $py = Get-ProjectPython }
  if (-not $py) { $py = Get-BootstrapPython }
  if (-not $py) {
    return @{
      command = @("ruff")
      returncode = -127
      stdout = ""
      stderr = "Python nao encontrado para executar Ruff."
      elapsed_ms = 0
    }
  }
  $target = if (Test-ProjPath "src") { "src" } else { "." }
  Write-Host "Executando: $py -m ruff check $target"
  return Run-TestCmd @($py, "-m", "ruff", "check", $target)
}

function Invoke-PyTestRun {
  param([string]$TargetDir = "")
  $pytestExe = Get-VenvPytest
  if ($pytestExe) {
    $cmd = @($pytestExe, "-q", "--maxfail=5")
    if ($TargetDir) { $cmd += $TargetDir }
    Write-Host "Executando: $($cmd -join ' ')"
    return Run-TestCmd $cmd
  }
  $py = Get-ProjectPython
  if ($py) {
    $cmd = @($py, "-m", "pytest", "-q", "--maxfail=5")
    if ($TargetDir) { $cmd += $TargetDir }
    Write-Host "Executando: $($cmd -join ' ')"
    return Run-TestCmd $cmd
  }
  foreach ($pair in @(
    ,@("pytest", "-q", "--maxfail=5")
    ,@("py", "-m", "pytest", "-q", "--maxfail=5")
  )) {
    if (Get-Command $pair[0] -ErrorAction SilentlyContinue) {
      $cmd = @($pair[0]) + ($pair[1] -split '\s+')
      if ($TargetDir) { $cmd += $TargetDir }
      return Run-TestCmd $cmd
    }
  }
  return @{
    command = @("pytest")
    returncode = -127
    stdout = ""
    stderr = "pytest nao encontrado. No venv do projeto: python -m pip install pytest"
    elapsed_ms = 0
  }
}

$hasPyTests = (Test-ProjPath "tests") -or (Test-ProjPath "pytest.ini") -or (Test-ProjPath "pyproject.toml") -or (Test-ProjPath "conftest.py")
$hasPyRepo = Test-PythonRepoLayout
$notes += "has_py_tests=$hasPyTests; has_py_repo=$hasPyRepo"
$py = $null
if (($hasPyTests -or $hasPyRepo) -and (-not $SkipAutoInstall)) {
  $py = Ensure-ProjectVenv
  if ($py) {
    $notes += "deps_auto_install=true"
  }
}
if ($hasPyTests) {
  $notes += "mode=pytest_suite"
  if ($py -and (-not $SkipAutoInstall)) {
    foreach ($step in (Install-ProjectTestDeps -PythonExe $py -FullSuite $true)) { $items += $step }
  }
  $items += Invoke-RuffCheck -PythonExe $py
  $items += Invoke-PyTestRun ""
} elseif ($hasPyRepo) {
  $notes += "mode=python_smoke"
  if (-not $py) { $py = Get-ProjectPython }
  if (-not $py) { $py = Get-BootstrapPython }
  if ($py) {
    if (-not $SkipAutoInstall) {
      foreach ($step in (Install-ProjectTestDeps -PythonExe $py -FullSuite $false)) { $items += $step }
    }
    $target = if (Test-ProjPath "src") { "src" } else { "" }
    Write-Host "Smoke: compileall + pytest (python=$py)"
    $compilePath = if ($target) { $target } else { "." }
    $items += Run-TestCmd @($py, "-m", "compileall", "-q", $compilePath)
    $items += Invoke-PyTestRun $target
    $ruffInVenv = Join-Path $root "venv\Scripts\ruff.exe"
    if (-not (Test-Path -LiteralPath $ruffInVenv)) { $ruffInVenv = Join-Path $root ".venv\Scripts\ruff.exe" }
    if (Test-Path -LiteralPath $ruffInVenv) {
      $rt = if ($target) { $target } else { "." }
      $items += Run-TestCmd @($ruffInVenv, "check", $rt)
    } elseif (Get-Command ruff -ErrorAction SilentlyContinue) {
      $rt = if ($target) { $target } else { "." }
      $items += Run-TestCmd @("ruff", "check", $rt)
    }
  } else {
    $notes += "python_missing=true"
    $items += @{
      command = @("python")
      returncode = -127
      stdout = ""
      stderr = "Python nao encontrado. Crie .venv no projeto ou adicione python/py ao PATH."
      elapsed_ms = 0
    }
  }
} else {
  $notes += "mode=none"
}
if (Test-ProjPath "package.json") {
  if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "Executando: npm test"
    $items += Run-TestCmd @("npm", "test", "--", "--watch=false")
  } else {
    $notes += "npm_missing=true"
  }
}
if (Test-ProjPath "pom.xml") {
  if (Get-Command mvn -ErrorAction SilentlyContinue) {
    $items += Run-TestCmd @("mvn", "-q", "test")
  }
}
if (Test-ProjPath "go.mod") {
  if (Get-Command go -ErrorAction SilentlyContinue) {
    $items += Run-TestCmd @("go", "test", "./...")
  }
}

if ($items.Count -eq 0) {
  $notes += "no_runner_executed"
  $items += @{
    command = @("none")
    returncode = -1
    stdout = ""
    stderr = "Nenhuma suite executada. Atualize o script (versao 3) no DSM e use -ProjectRoot com a pasta que contem src/ ou tests/."
    elapsed_ms = 0
  }
}

$failed = @($items | Where-Object { $_.returncode -ne 0 }).Count
$report = @{
  producer = "dsm-external-test-script-v4"
  script_version = $ScriptVersion
  project_name = "WhatsApp"
  project_root = $root
  root_hint_container = "/mnt/d/Sistemas/whatsapp"
  started_at = $started
  detection_notes = ($notes -join "; ")
  results = @($items)
  stats = @{
    total = $items.Count
    failed = $failed
    passed = [Math]::Max(0, $items.Count - $failed)
  }
}
$json = $report | ConvertTo-Json -Depth 10 -Compress:$false
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$outPath = Join-Path (Get-Location) $OutFile
[System.IO.File]::WriteAllText($outPath, $json, $utf8NoBom)
Write-Host ""
Write-Host "Relatorio salvo em $outPath"
Write-Host "Comandos: $($items.Count) | Falhas: $failed"
foreach ($r in $items) {
  $cmd = ($r.command -join ' ')
  Write-Host ("  [0] {1} ({2} ms)" -f $r.returncode, $cmd, $r.elapsed_ms)
}
