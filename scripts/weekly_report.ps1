$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Create the project .venv first. See README.md.'
}
Push-Location -LiteralPath $projectDir
try {
    & $pythonExe app.py report
    if ($LASTEXITCODE -ne 0) { throw 'Weekly report generation failed.' }
}
finally {
    Pop-Location
}
