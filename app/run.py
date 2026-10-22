"""
Interface web — DateLimit (Edição Privada com Suporte a WebSocket, PostgreSQL e SQLite)
Flask + WebSocket + HTMX + TailwindCSS.

Como rodar:
    # Com SQLite:
    python -m app.run

    # Com PostgreSQL:
    python -m app.run  (carrega automaticamente DATABASE_URL de .env ou .env.local)
"""

import json
import socket
import threading
from pathlib import Path
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify
from flask_sock import Sock

from app.db import (
    init_db,
    is_postgres,
    query_lots,
    update_lot_quant,
    update_lot_price,
    delete_lot as db_delete_lot,
    create_lot,
    DATABASE_URL
)

BASE_DIR = Path(__file__).resolve().parent.parent

def get_local_ip():
    """Detecta o IP local da máquina na rede Wi-Fi/Ethernet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── App Flask e WebSocket ─────────────────────────────────────────────────────
app = Flask(__name__)
sock = Sock(app)

# Clientes WebSocket conectados
connected_clients = set()
clients_lock = threading.Lock()


def broadcast_reload(reason="change"):
    """Notifica todos os clientes WebSocket conectados para atualizarem suas telas."""
    dead_clients = []
    with clients_lock:
        clients_copy = list(connected_clients)

    for client in clients_copy:
        try:
            client.send(json.dumps({"type": "reload", "reason": reason}))
        except Exception:
            dead_clients.append(client)

    if dead_clients:
        with clients_lock:
            for d in dead_clients:
                connected_clients.discard(d)


# Inicializa o banco de dados (PostgreSQL ou SQLite)
try:
    init_db()
except Exception as e:
    print(f"⚠️ Aviso na inicialização do banco: {e}")


@app.context_processor
def inject_context():
    """Injeta datas de referência e informações de rede nos templates."""
    today = date.today()
    local_ip = get_local_ip()
    port = 5000
    return {
        "now_date": today.strftime("%Y-%m-%d"),
        "critico_date": (today + timedelta(days=7)).strftime("%Y-%m-%d"),
        "atencao_date": (today + timedelta(days=29)).strftime("%Y-%m-%d"),
        "local_ip": local_ip,
        "port": port,
        "network_url": f"http://{local_ip}:{port}",
        "local_url": f"http://localhost:{port}",
        "is_postgres": is_postgres(),
        "db_name": "PostgreSQL" if is_postgres() else "SQLite",
    }


# ── Canal WebSocket ───────────────────────────────────────────────────────────

@sock.route("/ws")
def ws_handler(ws):
    """
    Canal WebSocket para busca instantânea e sincronização em tempo real entre dispositivos.
    """
    with clients_lock:
        connected_clients.add(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            action = msg.get("action")
            if action == "search":
                filtro = msg.get("filtro", "todos")
                busca = msg.get("busca", "").strip()

                total, lots = query_lots(filtro=filtro, busca=busca, limit=100)

                html = render_template(
                    "partials/lot_rows.html",
                    lots=lots,
                    total=total,
                    filtro=filtro,
                    busca=busca
                )

                ws.send(json.dumps({
                    "type": "lots_update",
                    "html": html,
                    "total": total,
                    "filtro": filtro,
                    "busca": busca
                }))
            elif action == "ping":
                ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        with clients_lock:
            connected_clients.discard(ws)


# ── Rotas HTTP (Render inicial + Fallback HTMX) ────────────────────────────────

@app.route("/")
def index():
    """Página principal — renderiza a lista inicial com resposta rápida."""
    filtro = request.args.get("filtro", "todos")
    busca = request.args.get("busca", "").strip()

    total, lots = query_lots(filtro=filtro, busca=busca, limit=100)

    # Se for requisição HTMX pedindo só as linhas
    if request.headers.get("HX-Request"):
        return render_template(
            "partials/lot_rows.html",
            lots=lots,
            total=total,
            filtro=filtro,
            busca=busca
        )

    return render_template(
        "index.html",
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca
    )


@app.route("/lote", methods=["POST"])
def add_lot():
    """Cadastra um novo produto/lote."""
    name = request.form.get("name", "").strip()
    quant_raw = request.form.get("quant", "0").strip()
    price_raw = request.form.get("price", "0.0").strip().replace(",", ".")
    date_valid = request.form.get("date_valid", "").strip()
    location = request.form.get("location", "LOJA").strip() or "LOJA"
    status = request.form.get("status", "NORMAL").strip() or "NORMAL"

    if not name:
        return '<div class="text-red-500 text-xs p-2">Nome do produto é obrigatório.</div>', 400
    if not date_valid:
        return '<div class="text-red-500 text-xs p-2">Data de validade é obrigatória.</div>', 400

    try:
        quant = int(quant_raw)
        price = float(price_raw)
    except ValueError:
        return '<div class="text-red-500 text-xs p-2">Quantidade ou preço inválidos.</div>', 400

    try:
        prod_id, lot_id = create_lot(
            name=name,
            quant=quant,
            price=price,
            date_valid=date_valid,
            location=location,
            status=status
        )
    except Exception as e:
        return f'<div class="text-red-500 text-xs p-2">Erro ao cadastrar lote: {str(e)}</div>', 500

    # Notifica outros clientes via WebSocket em tempo real
    broadcast_reload("create_lot")

    # Recarrega a tabela para quem enviou o formulário
    filtro = request.form.get("filtro", "todos")
    busca = request.form.get("busca", "").strip()
    total, lots = query_lots(filtro=filtro, busca=busca, limit=100)

    return render_template(
        "partials/lot_rows.html",
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca
    )


@app.route("/lote/<int:lot_id>", methods=["DELETE"])
def delete_lot(lot_id: int):
    """Dar baixa (deletar) em um lote específico."""
    try:
        db_delete_lot(lot_id)
    except Exception as e:
        return f"Erro ao deletar: {str(e)}", 500

    # Notifica outros clientes conectados via WebSocket
    broadcast_reload("delete_lot")

    # Retorna string vazia — o HTMX vai remover a <tr> do DOM do cliente atual
    return ""


@app.route("/lote/<int:lot_id>/quantidade", methods=["POST", "PATCH"])
def update_quant(lot_id: int):
    """Atualiza a quantidade de um lote específico."""
    quant_raw = request.form.get("quant", "").strip()
    try:
        quant = int(quant_raw)
        if quant < 0:
            return "Quantidade não pode ser negativa", 400
    except ValueError:
        return "Valor inválido", 400

    try:
        update_lot_quant(lot_id, quant)
    except Exception as e:
        return f"Erro ao salvar: {str(e)}", 500

    # Notifica outros clientes
    broadcast_reload("update_quant")

    return render_template("partials/quant_cell.html", lot={"lot_id": lot_id, "quant": quant})


@app.route("/lote/<int:lot_id>/preco", methods=["POST", "PATCH"])
def update_price(lot_id: int):
    """Atualiza o preço de um lote específico."""
    price_raw = request.form.get("price", "").strip().replace(",", ".")
    try:
        price = float(price_raw)
        if price < 0:
            return "Preço não pode ser negativo", 400
    except ValueError:
        return "Valor inválido", 400

    try:
        update_lot_price(lot_id, price)
    except Exception as e:
        return f"Erro ao salvar: {str(e)}", 500

    # Notifica outros clientes
    broadcast_reload("update_price")

    return render_template("partials/price_cell.html", lot={"lot_id": lot_id, "price": price})


@app.route("/export-git", methods=["POST"])
def export_git():
    """Sincroniza e envia alterações para o repositório date-private."""
    import subprocess
    from datetime import datetime

    msg_status = ""
    sucesso = False
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        subprocess.run(["git", "add", "."], cwd=str(BASE_DIR), check=True, capture_output=True, text=True)

        commit_res = subprocess.run(
            ["git", "commit", "-m", f"sync: atualizacao banco/codigo [{now_str}]"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True
        )

        push_res = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True
        )

        if push_res.returncode == 0:
            sucesso = True
            msg_status = f"✅ Alterações sincronizadas com o GitHub com sucesso! ({now_str})"
        else:
            if "Everything up-to-date" in push_res.stderr or "Everything up-to-date" in push_res.stdout:
                sucesso = True
                msg_status = f"✅ Tudo já estava atualizado no Git."
            else:
                sucesso = False
                msg_status = f"⚠️ Commit local realizado. Mensagem do push: {push_res.stderr or push_res.stdout}"
    except Exception as e:
        sucesso = False
        msg_status = f"❌ Erro ao sincronizar: {str(e)}"

    bg_color = "bg-green-100 border-green-400 text-green-700" if sucesso else "bg-red-100 border-red-400 text-red-700"
    return f"""
    <div id="git-status-msg" class="border px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between {bg_color}">
        <span>{msg_status}</span>
        <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
    </div>
    """


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ip = get_local_ip()
    port = 5000
    db_label = "PostgreSQL" if is_postgres() else "SQLite"
    print("\n" + "=" * 60)
    print(f"🚀 DateLimit Web rodando com WebSocket e banco [{db_label}]!")
    print(f"👉 Local:   http://localhost:{port}")
    print(f"📱 Celular / Outro dispositivo na rede:")
    print(f"   http://{ip}:{port}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=True)
