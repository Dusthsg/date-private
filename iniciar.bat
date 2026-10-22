@echo off
chcp 65001 > nul
title DateLimit - Sistema de Validades

echo ======================================================
echo           🚀 Iniciando DateLimit Web...
echo ======================================================
echo.

cd /d "%~dp0"

:: 1. Garante o arquivo .env com a conexão do banco
if not exist .env (
    echo [1/3] Configurando credenciais do banco de dados...
    echo DATABASE_URL="postgresql://neondb_owner:npg_HbchzmSIT1j8@ep-blue-hall-b5r5f35z-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require" > .env
)

:: 2. Garante o ambiente virtual e as dependências
if not exist "venv\Scripts\python.exe" (
    echo [2/3] Criando ambiente virtual Python (primeira execucao)...
    python -m venv venv
    if errorlevel 1 (
        echo ❌ Erro: Python nao encontrado no PATH do sistema.
        echo Certifique-se de que o Python 3.10+ esta instalado.
        pause
        exit /b 1
    )
    echo [2/3] Instalando dependencias necessarias...
    .\venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
) else (
    .\venv\Scripts\python.exe -c "import flask, psycopg2, flask_sock, openpyxl, pandas" >nul 2>&1
    if errorlevel 1 (
        echo [2/3] Atualizando dependencias necessarias...
        .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
    )
)

:: 3. Abre o navegador automaticamente
echo [3/3] Abrindo navegador em http://localhost:5000...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:5000"

:: 4. Executa o servidor Flask
echo.
.\venv\Scripts\python.exe -m app.run

pause
