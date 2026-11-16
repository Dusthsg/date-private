#!/bin/bash
# Script de inicialização rápida para Termux / Android Host
echo "=========================================="
echo "📱 Iniciando DateLimit Host no Celular..."
echo "=========================================="
cd "$(dirname "$0")"

# Instala dependências se necessário (pg8000 puro Python)
pip install -r requirements.txt --quiet

# Inicia o servidor DateLimit
python run.py
