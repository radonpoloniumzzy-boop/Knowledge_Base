param(
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$SetupScript = Join-Path $ScriptDir "setup_knowledge_forge.ps1"
$LastUrlFile = Join-Path $RootDir "Knowledge_Forge\last_url.txt"
$BasePort = 8765
$MaxPort = 8799

function Resolve-Python {
  $candidates = @(
    @{ Exe = "python"; Args = @() },
    @{ Exe = "py"; Args = @("-3") }
  )

  foreach ($candidate in $candidates) {
    try {
      $output = & $candidate.Exe @($candidate.Args + @("--version")) 2>&1
      if ($LASTEXITCODE -eq 0 -and "$output" -match "Python") {
        return [pscustomobject]@{
          Exe = $candidate.Exe
          Args = $candidate.Args
          Display = "$($candidate.Exe) $($candidate.Args -join ' ')".Trim()
        }
      }
    } catch {
      continue
    }
  }

  throw "Python was not found. Run the environment repair launcher or install Python 3.11+."
}

function Get-AppUrl($Port) {
  return "http://127.0.0.1:$Port"
}

function Test-AppReady($Port) {
  try {
    $url = "$(Get-AppUrl $Port)/packs"
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
    return ($response.StatusCode -eq 200 -and $response.Content.Contains("data-tag-picker"))
  } catch {
    return $false
  }
}

function Test-PortFree($Port) {
  $listener = $null
  try {
    $address = [System.Net.IPAddress]::Parse("127.0.0.1")
    $listener = [System.Net.Sockets.TcpListener]::new($address, $Port)
    $listener.Start()
    return $true
  } catch {
    return $false
  } finally {
    if ($listener) {
      $listener.Stop()
    }
  }
}

function Find-RunningAppPort {
  for ($port = $BasePort; $port -le $MaxPort; $port++) {
    if (-not (Test-PortFree $port)) {
      if (Test-AppReady $port) {
        return $port
      }
    }
  }
  return $null
}

function Find-FreePort {
  for ($port = $BasePort; $port -le $MaxPort; $port++) {
    if (Test-PortFree $port) {
      return $port
    }
  }
  throw "No free local port was found between $BasePort and $MaxPort."
}

function Quote-Arg($Value) {
  return "'" + ($Value -replace "'", "''") + "'"
}

function Ensure-Dependencies($Python) {
  $check = "import importlib.util,sys; mods=['fastapi','uvicorn','jinja2','multipart','markitdown']; missing=[m for m in mods if importlib.util.find_spec(m) is None]; print(','.join(missing)); sys.exit(1 if missing else 0)"
  $output = & $Python.Exe @($Python.Args + @("-c", $check)) 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[Knowledge Forge] Missing packages: $output"
    Write-Host "[Knowledge Forge] Running environment setup..."
    powershell -NoProfile -ExecutionPolicy Bypass -File $SetupScript -Quiet
    if ($LASTEXITCODE -ne 0) {
      throw "Automatic setup failed. Run the environment repair launcher manually."
    }
  }
}

function Prepare-Database($Python) {
  Write-Host "[Knowledge Forge] Checking database migrations..."
  $code = @"
from knowledge_forge import services
from knowledge_forge.db import DATA_DIR, DB_PATH
from knowledge_forge.legacy_migration import LegacyKnowledgeMigrator

services.seed_defaults()
migrator = LegacyKnowledgeMigrator(DB_PATH, DATA_DIR)
if migrator.needs_migration():
    report = migrator.migrate()
    print(f'legacy assets migrated: {report.created_sources}')
else:
    print('database ready')
"@
  & $Python.Exe @($Python.Args + @("-c", $code))
  if ($LASTEXITCODE -ne 0) {
    throw "Database migration failed. The service was not started."
  }
}

Set-Location $ScriptDir
$Python = Resolve-Python
Ensure-Dependencies $Python

$runningPort = Find-RunningAppPort
if ($runningPort) {
  $url = Get-AppUrl $runningPort
  Write-Host "[Knowledge Forge] Service is already running: $url"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LastUrlFile) | Out-Null
  Set-Content -Path $LastUrlFile -Value $url -Encoding ASCII
  if (-not $NoBrowser) {
    Start-Process $url
  }
  exit 0
}

Prepare-Database $Python

$port = Find-FreePort
$url = Get-AppUrl $port
Write-Host "[Knowledge Forge] Starting local service on $url ..."

$pythonArgs = @($Python.Args + @("knowledge_forge.py"))
$pythonCommand = "& " + (Quote-Arg $Python.Exe)
foreach ($arg in $pythonArgs) {
  $pythonCommand += " " + (Quote-Arg $arg)
}
$command = "`$env:KNOWLEDGE_FORGE_PORT='$port'; Set-Location " + (Quote-Arg $ScriptDir) + "; " + $pythonCommand
$process = Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  if (Test-AppReady $port) {
    $ready = $true
    break
  }
}

if (-not $ready) {
  throw "Service failed to start. Launcher PID=$($process.Id). Run the stop launcher and retry."
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LastUrlFile) | Out-Null
Set-Content -Path $LastUrlFile -Value $url -Encoding ASCII

Write-Host "[Knowledge Forge] Ready: $url"
if (-not $NoBrowser) {
  Start-Process $url
}
