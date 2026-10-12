$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    $pythonPath = $bundledPython
} else {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}
$port = if ($env:MINIRM_PORT) { [int]$env:MINIRM_PORT } else { 8765 }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Port $port is already in use. If MiniRM is running, open http://127.0.0.1:$port"
    Read-Host 'Press Enter to close'
    exit 1
}
Write-Host "MiniRM: http://127.0.0.1:$port"
Write-Host 'Demo accounts: analyst / reviewer / developer / admin'
Write-Host 'Demo password: MiniRM2026! (unless customized at first startup)'
Write-Host 'Press Ctrl+C to stop the server.'
Start-Process "http://127.0.0.1:$port"
& $pythonPath -u server.py --port $port
