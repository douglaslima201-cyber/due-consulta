Write-Host "Encerrando servidor anterior..." -ForegroundColor Yellow
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Iniciando servidor Flask..." -ForegroundColor Green
Set-Location "$PSScriptRoot\backend"
python main.py
