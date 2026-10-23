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
    if ! command -v python >/dev/null 2>&1; then
        echo "📱 Instalando Python no Termux..."
        pkg update -y && pkg install -y python git
    fi
fi

# 2. Garante o arquivo .env com a URL de conexão do PostgreSQL
if [ ! -f .env ] || ! grep -q "DATABASE_URL" .env; then
    echo "[1/3] Configurando credenciais seguras da nuvem..."
    python -c "import base64; open('.env','w').write('DATABASE_URL=\"' + base64.b64decode('cG9zdGdyZXNxbDovL25lb25kYl9vd25lcjpucGdfSGJjaHptU0lUMWo4QGVwLWJsdWUtaGFsbC1iNXI1ZjM1ei1wb29sZXIuYy03LnVzLWVhc3QtMi5hd3MubmVvbi50ZWNoL25lb25kYj9zc2xtb2RlPXJlcXVpcmU=').decode('utf-8') + '\"\n')" 2>/dev/null || {
        echo "cG9zdGdyZXNxbDovL25lb25kYl9vd25lcjpucGdfSGJjaHptU0lUMWo4QGVwLWJsdWUtaGFsbC1iNXI1ZjM1ei1wb29sZXIuYy03LnVzLWVhc3QtMi5hd3MubmVvbi50ZWNoL25lb25kYj9zc2xtb2RlPXJlcXVpcmU=" | base64 -d > .tmp_url 2>/dev/null || base64 -D > .tmp_url
        echo "DATABASE_URL=\"$(cat .tmp_url)\"" > .env
        rm -f .tmp_url
    }
fi

# 3. Instala dependências Python necessárias (incluindo pg8000 e xlrd)
echo "[2/3] Verificando bibliotecas Python (pg8000, xlrd, openpyxl)..."
pip install -r requirements.txt --quiet 2>/dev/null || {
    echo "Instalando bibliotecas essenciais..."
    pip install flask flask-sock python-dotenv pandas openpyxl xlrd pg8000 --quiet
}

# 4. Tenta abrir o navegador do celular automaticamente
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

# 5. Executa o servidor Flask
python -m app.run
