@echo off
chcp 65001 > nul
title DateLimit - Sistema de Validades

echo ======================================================
echo           🚀 Iniciando DateLimit Web...
echo ======================================================
echo.

cd /d "%~dp0"

:: 1. Se não existir .env, cria a partir do .env.example
if not exist .env (
    if exist .env.example (
        copy .env.example .env > nul
        echo ⚠️ Arquivo .env criado. Se desejar, configure a DATABASE_URL do Neon nele.
    )
)

:: 2. Garante o ambiente virtual e as dependências
if not exist "venv\Scripts\python.exe" (
    echo [1/2] Criando ambiente virtual Python (primeira execucao)...
    python -m venv venv
    if errorlevel 1 (
        echo ❌ Erro: Python nao encontrado no PATH do sistema.
        echo Certifique-se de que o Python 3.10+ esta instalado.
        pause
        exit /b 1
    )
    echo [1/2] Instalando dependencias necessarias...
    .\venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
) else (
    .\venv\Scripts\python.exe -c "import flask, psycopg2, flask_sock, openpyxl, pandas" >nul 2>&1
    if errorlevel 1 (
        echo [1/2] Atualizando dependencias necessarias...
        .\venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
    )
)

:: 3. Abre o navegador automaticamente
echo [2/2] Abrindo navegador em http://localhost:5000...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:5000"

:: 4. Executa o servidor Flask
echo.
.\venv\Scripts\python.exe -m app.run

pause
