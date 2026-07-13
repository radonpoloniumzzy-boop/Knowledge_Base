$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$Launcher = Join-Path $ScriptDir "start_knowledge_forge.bat"

if (-not (Test-Path $Launcher)) {
  throw "Launcher was not found: $Launcher"
}

$ShortcutName = [string]::Concat(
  [char]0x77E5, [char]0x8BC6, [char]0x70BC, [char]0x5236, [char]0x53F0, ".lnk"
)
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop $ShortcutName
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Launcher
$Shortcut.WorkingDirectory = $RootDir
$Shortcut.Description = "Start local Knowledge Forge"
$Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
$Shortcut.Save()

Write-Host "[Knowledge Forge] Desktop shortcut created: $ShortcutPath"
