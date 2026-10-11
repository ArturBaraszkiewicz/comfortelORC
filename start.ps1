param([int]$Port = 8787, [string]$BindAddress = '127.0.0.1', [string]$PublicUrl = '')
$ErrorActionPreference = 'Stop'
$appDirectory = $PSScriptRoot
$appPython = Join-Path $appDirectory '.venv\Scripts\python.exe'
Push-Location $appDirectory
try {
    if (-not (Test-Path -LiteralPath $appPython)) {
        Write-Host 'Przygotowuję lokalne środowisko Python...'
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Nie udało się utworzyć środowiska. Wymagany Python 3.10 lub nowszy.' }
    }
    & $appPython -c "import fastapi, uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $appPython -m pip install -r requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Instalacja zależności nie powiodła się.' }
    }
    & $appPython -c "import os; from pathlib import Path; from orchestrator.store import Store; from orchestrator.auth import Auth; s = Store(Path(os.environ.get('LOCAL_ORCHESTRATOR_DATA', 'data')) / 'orchestrator.db'); s.initialize(); raise SystemExit(0 if Auth(s).has_users() else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Utwórz pierwsze konto do panelu (login: admin).'
        & $appPython manage.py add-user admin --role admin
        if ($LASTEXITCODE -ne 0) { throw 'Nie udało się utworzyć konta.' }
    }
    $launchArguments = @('run.py', '--port', $Port, '--host', $BindAddress)
    if ($PublicUrl) { $launchArguments += @('--public-url', $PublicUrl) }
    & $appPython @launchArguments
    if ($LASTEXITCODE -ne 0) { throw 'Aplikacja zakończyła się błędem. Szczegóły znajdują się powyżej.' }
} finally {
    Pop-Location
}
