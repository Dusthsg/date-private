#!/bin/bash
# ============================================================
# 🚀 DateLimit Web - Inicializador para Termux (Android) & Linux
# ============================================================

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "======================================================"
echo "          🚀 Iniciando DateLimit no Termux...         "
echo "======================================================"
echo ""

# 1. Garante que o ambiente Termux tenha Python e Git
if [ -n "$PREFIX" ] && [ -d "$PREFIX" ]; then
    echo "📱 Ambiente Termux detectado!"
    if ! command -v python >/dev/null 2>&1; then
        echo "Instalando Python no Termux..."
        pkg update -y && pkg install -y python git clang libpq
    fi
fi

# 2. Instala dependências Python necessárias
echo "[1/2] Verificando bibliotecas Python..."
pip install -r requirements.txt --quiet 2>/dev/null || {
    echo "Instalando dependências essenciais..."
    pip install flask flask-sock python-dotenv pandas openpyxl --quiet
}

# 3. Tenta abrir o navegador do celular automaticamente
if command -v termux-open-url >/dev/null 2>&1; then
    (sleep 2 && termux-open-url http://localhost:5000) &
elif command -v xdg-open >/dev/null 2>&1; then
    (sleep 2 && xdg-open http://localhost:5000) &
fi

echo ""
echo "======================================================"
echo "👉 Acesse pelo Chrome/Navegador do celular:"
echo "   http://localhost:5000"
echo "======================================================"
echo ""

# 4. Executa o servidor Flask
python -m app.run
