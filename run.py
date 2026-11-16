#!/usr/bin/env python3
"""
DateLimit Web - Ponto de Entrada Principal
Ideal para hospedar diretamente no Celular (Termux / Pydroid 3) ou PC.
Acesso instantâneo e Local-First com sincronização automática na nuvem (PostgreSQL Neon).
"""
import os
import sys
from pathlib import Path

# Garante o diretório do projeto no sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.run import app, get_local_ip, is_postgres

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    ip = get_local_ip()
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true")
    db_label = "PostgreSQL (Nuvem)" if is_postgres() else "SQLite (Local)"

    print("\n" + "=" * 65)
    print("🚀 DateLimit Web rodando [Host Celular / Local]")
    print(f"📦 Armazenamento ativo: [{db_label}]")
    print(f"👉 No próprio celular/host: http://localhost:{port}")
    print(f"📱 Em outros aparelhos na mesma rede Wi-Fi:")
    print(f"   http://{ip}:{port}")
    print("=" * 65 + "\n")

    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
