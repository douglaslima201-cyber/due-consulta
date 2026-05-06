#!/bin/bash
# ══════════════════════════════════════════════
#   DUE Consulta — Script de instalação e início
# ══════════════════════════════════════════════

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   DUE Consulta — Siscomex Automation Tool    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Verificar Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 não encontrado. Instale Python 3.10+"
  exit 1
fi

echo "📦 Instalando dependências Python..."
cd backend
pip install -r requirements.txt --quiet

echo "🎭 Instalando navegador Playwright (Chromium)..."
python -m playwright install chromium

echo ""
echo "✅ Instalação concluída!"
echo ""
echo "🚀 Iniciando backend na porta 5000..."
echo "🌐 Abra o arquivo: frontend/index.html no seu navegador"
echo ""
python main.py
