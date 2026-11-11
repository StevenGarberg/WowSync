param(
    [string]$Python = 'python',
    [string]$Output = (Join-Path $env:LOCALAPPDATA 'WoWSync\portable-build')
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskOutput = [IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
& $Python -m venv (Join-Path $taskOutput 'venv')
if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment' }
$taskPython = Join-Path $taskOutput 'venv\Scripts\python.exe'
& $taskPython -m pip install 'pyinstaller==6.22.3'
if ($LASTEXITCODE -ne 0) { throw 'Could not install PyInstaller' }
& $taskPython -m PyInstaller --noconfirm --clean --onefile --windowed --name WoWSync --distpath (Join-Path $taskOutput 'dist') --workpath (Join-Path $taskOutput 'work') --specpath $taskOutput --paths $taskRoot --add-data "$(Join-Path $taskRoot 'wowsync\static');wowsync/static" (Join-Path $PSScriptRoot 'desktop_entry.py')
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Output (Join-Path $taskOutput 'dist\WoWSync.exe')
