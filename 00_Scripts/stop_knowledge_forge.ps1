$ErrorActionPreference = "SilentlyContinue"

$matches = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "knowledge_forge\.py" }

if (-not $matches) {
  Write-Host "[Knowledge Forge] No running service was found."
  exit 0
}

foreach ($proc in $matches) {
  Stop-Process -Id $proc.ProcessId -Force
  Write-Host "[Knowledge Forge] Stopped PID=$($proc.ProcessId)"
}
