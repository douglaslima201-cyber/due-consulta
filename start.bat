@echo off
title DUE Consulta - Siscomex

echo.
echo ╔══════════════════════════════════════════════╗
echo ║   DUE Consulta — Siscomex Automation Tool    ║
echo ╚══════════════════════════════════════════════╝
echo.

echo 📦 Instalando dependências Python...
cd backend
pip install -r requirements.txt

echo.
echo 🎭 Instalando navegador Playwright (Chromium)...
python -m playwright install chromium

echo.
echo ✅ Instalação concluída!
echo.
echo 🚀 Iniciando backend na porta 5000...
echo 🌐 Abra o arquivo: frontend\index.html no seu navegador
echo.
python main.py

pause
