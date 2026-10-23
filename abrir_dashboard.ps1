# Abre el dashboard de loveworm; si el servidor no esta corriendo, lo levanta primero.
$root = $PSScriptRoot
$url = 'http://127.0.0.1:5000'

function Test-Dashboard {
    try { Invoke-WebRequest "$url/api/state" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Test-Dashboard)) {
    Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList '-m', 'loveworm.dashboard' `
        -WorkingDirectory $root -WindowStyle Minimized
    for ($i = 0; $i -lt 30 -and -not (Test-Dashboard); $i++) { Start-Sleep -Seconds 1 }
}

Start-Process $url
