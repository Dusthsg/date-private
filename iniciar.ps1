# Script de Inicialização Rápida - DateLimit Web
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "          🚀 Iniciando DateLimit Web...               " -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan

# 1. Garante .env
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
    }
}

# 2. Garante venv e dependências
if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "[1/2] Criando ambiente virtual Python..." -ForegroundColor Yellow
    python -m venv venv
    Write-Host "[1/2] Instalando dependências..." -ForegroundColor Yellow
    .\venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
} else {
    try {
        .\venv\Scripts\python.exe -c "import flask, psycopg2, flask_sock, openpyxl, pandas" 2>$null
    } catch {
        Write-Host "[1/2] Atualizando dependências..." -ForegroundColor Yellow
        .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
    }
}

# 3. Abre navegador
Write-Host "[2/2] Abrindo navegador em http://localhost:5000..." -ForegroundColor Green
Start-Process "http://localhost:5000"

# 4. Roda o servidor
.\venv\Scripts\python.exe -m app.run
