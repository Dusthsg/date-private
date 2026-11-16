"""
Ponto de entrada WSGI para servidores de produção (Render, Gunicorn, etc.)
"""
import os
from app.run import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true")
    app.run(host="0.0.0.0", port=port, debug=debug)
