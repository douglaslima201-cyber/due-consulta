Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Set-Location "$PSScriptRoot\backend"
python main.py
