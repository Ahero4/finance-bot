$ErrorActionPreference = 'Stop'
$tokenLine = Get-Content -LiteralPath (Join-Path $PSScriptRoot '.env') |
    Where-Object { $_ -match '^BOT_TOKEN=' } |
    Select-Object -First 1
if (-not $tokenLine -or -not $tokenLine.Substring(10).Trim()) {
    throw 'Set BOT_TOKEN in the local .env file.'
}
$env:BOT_TOKEN = $tokenLine.Substring(10).Trim()
$pythonPath = Join-Path $PSScriptRoot '.venv-audit\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'The verified .venv-audit environment is missing. See README.md for setup.'
}
Push-Location $PSScriptRoot
try {
    & $pythonPath (Join-Path $PSScriptRoot 'bot.py')
    if ($LASTEXITCODE -ne 0) { throw "Bot exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
    Remove-Item Env:BOT_TOKEN -ErrorAction SilentlyContinue
}
