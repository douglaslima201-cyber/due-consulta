@echo off
title Ferramentas Enterprise — Rumo Brasil

echo.
echo ╔══════════════════════════════════════════════════╗
echo ║    Ferramentas Enterprise — Rumo Brasil          ║
echo ╚══════════════════════════════════════════════════╝
echo.

echo [1/3] Instalando dependencias Python...
cd backend
pip install -r requirements.txt -q

echo [2/3] Instalando navegador Playwright (Chromium)...
python -m playwright install chromium

echo [3/3] Iniciando servidor na porta 5000...
echo.
echo  Acesse: http://localhost:5000/portal.html
echo  (abrindo navegador automaticamente em 3 segundos)
echo.

:: Abre o browser após 3 segundos em segundo plano
start /B cmd /C "timeout /T 3 /NOBREAK >nul && start http://localhost:5000/portal.html"

python main.py

pause
