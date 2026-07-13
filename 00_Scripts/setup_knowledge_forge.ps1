param(
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$Requirements = Join-Path $ScriptDir "knowledge_forge\requirements.txt"

function Write-Step($Message) {
  if (-not $Quiet) {
    Write-Host "[Knowledge Forge] $Message"
  }
}

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

  throw "Python was not found. Install Python 3.11+ and enable Add Python to PATH."
}

Set-Location $ScriptDir
$Python = Resolve-Python
Write-Step "Python: $($Python.Display)"

if (-not (Test-Path $Requirements)) {
  throw "Requirements file was not found: $Requirements"
}

Write-Step "Installing or repairing Python packages..."
& $Python.Exe @($Python.Args + @("-m", "pip", "install", "-r", $Requirements))
if ($LASTEXITCODE -ne 0) {
  throw "Package installation failed. Check network and pip configuration."
}

Write-Step "Initializing local database and defaults..."
& $Python.Exe @($Python.Args + @("-c", "from knowledge_forge import services; services.seed_defaults(); print('database ready')"))
if ($LASTEXITCODE -ne 0) {
  throw "Database initialization failed."
}

Write-Step "Checking core code..."
& $Python.Exe @($Python.Args + @("-m", "py_compile", "knowledge_forge.py", "knowledge_forge\app.py", "knowledge_forge\core_processing.py", "knowledge_forge\db.py", "knowledge_forge\enhancement.py", "knowledge_forge\extraction.py", "knowledge_forge\ingestion.py", "knowledge_forge\legacy_migration.py", "knowledge_forge\migrations.py", "knowledge_forge\recycle_bin.py", "knowledge_forge\services.py"))
if ($LASTEXITCODE -ne 0) {
  throw "Code check failed."
}

Write-Step "Environment is ready: $RootDir"
