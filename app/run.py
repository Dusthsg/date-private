import time
import os
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
import gzip
from pathlib import Path
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_sock import Sock

from flask import session, redirect, url_for
from werkzeug.security import check_password_hash

from app.db import (
    init_db,
    is_postgres,
    is_postgres_configured,
    get_db_status,
    pull_cloud_to_sqlite,
    reconnect_postgres,
    set_on_reconnect_callback,
    query_lots,
    update_lot_quant,
    update_lot_price,
    delete_lot as db_delete_lot,
    restore_lot,
    purge_deleted_movement,
    purge_all_deleted_movements,
    create_lot,
    search_products,
    clear_query_cache,
    register_cache_clear_callback,
    get_user_by_username,
    get_user_by_id,
    get_user_by_pin,
    list_users,
    list_active_operators,
    create_user,
    update_user,
    delete_user,
    generate_api_token,
    log_audit,
    query_audit_logs,
    DATABASE_URL
)

from functools import lru_cache

BASE_DIR = Path(__file__).resolve().parent.parent

@lru_cache(maxsize=1)
def get_local_ip():
    """Detecta o IP local da máquina na rede Wi-Fi/Ethernet com cache para zero sobrecarga."""
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
app.secret_key = os.environ.get("SECRET_KEY", "datelimit-private-auth-secret-key-2026")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
sock = Sock(app)

def get_effective_user():
    """Retorna o operador ativo no momento (chão de loja) ou o usuário logado."""
    return session.get("active_operator") or session.get("name") or session.get("username") or "Operador"


# Clientes WebSocket conectados
connected_clients = set()
clients_lock = threading.Lock()


def broadcast_reload(reason="change", exclude_client_id=None):
    """
    Notifica todos os clientes WebSocket em thread paralela de alta velocidade.
    NUNCA bloqueia a resposta HTTP do usuário atual.
    """
    def _async_notify():
        dead_clients = []
        with clients_lock:
            clients_copy = list(connected_clients)

        payload = json.dumps({"type": "reload", "reason": reason, "sender": exclude_client_id})
        for client in clients_copy:
            try:
                # Não notifica o próprio cliente que acabou de enviar o formulário
                if exclude_client_id and getattr(client, "client_id", None) == exclude_client_id:
                    continue
                client.send(payload)
            except Exception:
                dead_clients.append(client)

        if dead_clients:
            with clients_lock:
                for d in dead_clients:
                    connected_clients.discard(d)

    threading.Thread(target=_async_notify, daemon=True).start()


# Inicializa o banco de dados (SQLite local primeiro, depois ligação com PostgreSQL)
try:
    init_db()
except Exception as e:
    print(f"⚠️ Aviso na inicialização do banco: {e}")

# Registra callback de recarregamento automático nos clientes quando o PostgreSQL reconectar
set_on_reconnect_callback(lambda: broadcast_reload("reconnected"))


@app.context_processor
def inject_context():
    """Injeta datas de referência, informações de rede e status de autenticação/auditoria."""
    today = date.today()
    local_ip = get_local_ip()
    port = 5000
    st = get_db_status()
    u_role = session.get("role", "operador")
    u_name = session.get("name", "Operador")
    active_op = session.get("active_operator") or u_name
    return {
        "now_date": today.strftime("%Y-%m-%d"),
        "critico_date": (today + timedelta(days=7)).strftime("%Y-%m-%d"),
        "atencao_date": (today + timedelta(days=29)).strftime("%Y-%m-%d"),
        "local_ip": local_ip,
        "port": port,
        "network_url": f"http://{local_ip}:{port}",
        "local_url": f"http://localhost:{port}",
        "is_postgres": st["postgres_online"],
        "db_name": st["db_name"],
        "last_sync": st["last_sync"],
        "is_admin": (u_role == "admin"),
        "current_user_name": u_name,
        "current_user_role": u_role,
        "active_operator": active_op,
    }


# ── Middleware de Autenticação Segura ─────────────────────────────────────────

@app.before_request
def check_authentication():
    # Ignora checagem em testes automatizados e em assets estáticos
    if app.config.get("TESTING"):
        return None

    path = request.path
    if (
        path.startswith("/static")
        or path.startswith("/favicon")
        or path in ("/login", "/ws")
    ):
        return None

    if not session.get("user_id"):
        if request.headers.get("HX-Request"):
            return Response("", 200, {"HX-Redirect": "/login"})
        return redirect("/login")


# ── Otimizações Globais de Rede e Cache ────────────────────────────────────────

# Cache em memória RAM ultrarrápido para HTML de linhas renderizadas (< 0.01ms)
_html_rows_cache = {}
_html_rows_lock = threading.Lock()

def clear_html_rows_cache():
    with _html_rows_lock:
        _html_rows_cache.clear()

register_cache_clear_callback(clear_html_rows_cache)

def render_lot_rows(lots, total, filtro, busca, page=1, has_more=False, next_page=2):
    """Renderiza as linhas da tabela de lotes com cache em RAM instantâneo para abas de filtro."""
    is_cacheable = (not busca and page == 1)
    if is_cacheable:
        with _html_rows_lock:
            cached = _html_rows_cache.get(filtro)
            if cached is not None:
                return cached

    html = render_template(
        "partials/lot_rows.html",
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca,
        page=page,
        has_more=has_more,
        next_page=next_page
    )

    if is_cacheable:
        with _html_rows_lock:
            _html_rows_cache[filtro] = html

    return html


@app.after_request
def compress_response(response):
    """
    Compactação gzip automática em tempo real para HTML e JSON (> 1KB)
    e cabeçalhos HTTP inteligentes de Cache-Control e Vary para máxima velocidade.
    """
    try:
        path = request.path
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        elif request.method == "GET" and not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Vary"] = "Accept-Encoding"

        accept_encoding = request.headers.get("Accept-Encoding", "")
        if (
            "gzip" in accept_encoding.lower()
            and response.status_code < 300
            and not response.direct_passthrough
            and not response.headers.get("Content-Encoding")
        ):
            data = response.get_data()
            if len(data) > 1024:
                compressed = gzip.compress(data, compresslevel=1)
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = len(compressed)
    except Exception:
        pass
    return response


def _prefetch_next_chunk(filtro: str, busca: str, page: int, limit: int = 50):
    """Pré-aquece a próxima página em segundo plano no cache em memória RAM (0.03ms no scroll)."""
    try:
        offset = max(0, (page - 1) * limit)
        query_lots(filtro=filtro, busca=busca, limit=limit, offset=offset)
    except Exception:
        pass


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
            if action == "register":
                ws.client_id = msg.get("clientId")
                continue
            elif action == "search":
                filtro = msg.get("filtro", "todos")
                busca = msg.get("busca", "").strip()
                seq = msg.get("seq")
                is_prewarm = bool(msg.get("prewarm"))

                limit = 50
                total, lots = query_lots(filtro=filtro, busca=busca, limit=limit, offset=0)
                has_more = len(lots) < total

                if has_more and not busca and not is_prewarm:
                    threading.Thread(target=_prefetch_next_chunk, args=(filtro, busca, 2, limit), daemon=True).start()

                html = render_lot_rows(
                    lots=lots,
                    total=total,
                    filtro=filtro,
                    busca=busca,
                    page=1,
                    has_more=has_more,
                    next_page=2
                )

                ws.send(json.dumps({
                    "type": "lots_update",
                    "html": html,
                    "total": total,
                    "filtro": filtro,
                    "busca": busca,
                    "seq": seq,
                    "prewarm": is_prewarm
                }))
            elif action == "ping":
                ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        with clients_lock:
            connected_clients.discard(ws)


# ── Rotas HTTP (Render inicial + Fallback HTMX + Chunks) ────────────────────────

@app.route("/")
def index():
    """Página principal — renderiza a lista inicial em chunks de 50 itens."""
    filtro = request.args.get("filtro", "todos")
    busca = request.args.get("busca", "").strip()
    page = request.args.get("page", 1, type=int)
    limit = 50
    offset = max(0, (page - 1) * limit)

    total, lots = query_lots(filtro=filtro, busca=busca, limit=limit, offset=offset)
    has_more = (offset + len(lots)) < total
    next_page = page + 1

    if has_more:
        threading.Thread(target=_prefetch_next_chunk, args=(filtro, busca, next_page, limit), daemon=True).start()

    # Se for página subsequente solicitada (chunk)
    if page > 1:
        return render_template(
            "partials/lot_rows_chunk.html",
            lots=lots,
            total=total,
            filtro=filtro,
            busca=busca,
            page=page,
            has_more=has_more,
            next_page=next_page
        )

    # Se for requisição HTMX pedindo só as linhas (ex: troca de filtro via fallback)
    if request.headers.get("HX-Request"):
        return render_lot_rows(
            lots=lots,
            total=total,
            filtro=filtro,
            busca=busca,
            page=1,
            has_more=has_more,
            next_page=next_page
        )

    return render_template(
        "index.html",
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca,
        page=1,
        has_more=has_more,
        next_page=next_page
    )


@app.route("/lots/page")
def lots_page():
    """Rota para chunks de paginação / rolagem infinita via HTMX."""
    filtro = request.args.get("filtro", "todos")
    busca = request.args.get("busca", "").strip()
    page = request.args.get("page", 2, type=int)
    limit = 50
    offset = max(0, (page - 1) * limit)

    total, lots = query_lots(filtro=filtro, busca=busca, limit=limit, offset=offset)
    has_more = (offset + len(lots)) < total
    next_page = page + 1

    if has_more:
        threading.Thread(target=_prefetch_next_chunk, args=(filtro, busca, next_page, limit), daemon=True).start()

    return render_template(
        "partials/lot_rows_chunk.html",
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca,
        page=page,
        has_more=has_more,
        next_page=next_page
    )


@app.route("/api/products/search", methods=["GET"])
def api_search_products():
    """Busca produtos no catálogo para o autocomplete na tela de cadastro."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        results = search_products(q, limit=10)
        return jsonify(results)
    except Exception as e:
        return jsonify([]), 200


@app.route("/lote", methods=["POST"])
def add_lot():
    """Cadastra um novo produto/lote."""
    name = request.form.get("name", "").strip().upper()
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
        # Registra auditoria completa com rastreamento do operador
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(
            effective_user,
            "CRIAR_LOTE",
            f"Cadastrou lote #{lot_id} de '{name}': {quant} un, validade {date_valid}, R$ {price:.2f}",
            target_id=lot_id,
            product_name=name,
            ip_address=ip
        )
    except Exception as e:
        return f'<div class="text-red-500 text-xs p-2">Erro ao cadastrar lote: {str(e)}</div>', 500

    # Notifica outros clientes via WebSocket em tempo real sem travar a requisição atual
    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("create_lot", exclude_client_id=client_id)

    # Recarrega a tabela para quem enviou o formulário
    filtro = request.form.get("filtro", "todos")
    busca = request.form.get("busca", "").strip()
    limit = 50
    total, lots = query_lots(filtro=filtro, busca=busca, limit=limit, offset=0)
    has_more = len(lots) < total

    return render_lot_rows(
        lots=lots,
        total=total,
        filtro=filtro,
        busca=busca,
        page=1,
        has_more=has_more,
        next_page=2
    )


@app.route("/lote/<int:lot_id>", methods=["DELETE"])
def delete_lot(lot_id: int):
    """Dar baixa (deletar) em um lote específico com resposta ultrarrápida (< 1ms)."""
    try:
        db_delete_lot(lot_id, sync=False)
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "BAIXA_LOTE", f"Deu baixa (deletou) no lote #{lot_id}", target_id=lot_id, ip_address=ip)
    except Exception as e:
        return f"Erro ao deletar: {str(e)}", 500

    # Notifica outros clientes conectados via WebSocket sem travar quem deletou
    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("delete_lot", exclude_client_id=client_id)

    # Retorna string vazia — o HTMX vai remover a <tr> do DOM do cliente atual
    return ""


@app.route("/lote/restaurar/<int:movement_id>", methods=["POST"])
def restore_lot_route(movement_id: int):
    """Restaura um lote baixado (deletado) de volta para o estoque com resposta instantânea."""
    try:
        ok = restore_lot(movement_id, sync=False)
        if not ok:
            return "Item não encontrado ou já restaurado.", 404
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "RESTAURAR_LOTE", f"Restaurou registro de baixa #{movement_id} de volta para a loja", target_id=movement_id, ip_address=ip)
    except Exception as e:
        return f"Erro ao restaurar lote: {str(e)}", 500

    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("restore_lot", exclude_client_id=client_id)
    return ""


@app.route("/lote/movimento/<int:movement_id>", methods=["DELETE"])
def purge_movement_route(movement_id: int):
    """Exclui permanentemente um registro de baixa (deletado) do histórico de auditoria."""
    try:
        purge_deleted_movement(movement_id, sync=False)
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "PURGAR_BAIXA", f"Excluiu definitivamente registro de baixa #{movement_id}", target_id=movement_id, ip_address=ip)
    except Exception as e:
        return f"Erro ao excluir registro de auditoria: {str(e)}", 500

    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("purge_movement", exclude_client_id=client_id)
    return ""


@app.route("/lote/deletados/limpar", methods=["POST", "DELETE"])
def purge_all_deleted_route():
    """Exclui permanentemente todos os registros de baixa do histórico de auditoria."""
    try:
        purge_all_deleted_movements()
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "LIMPAR_AUDITORIA", "Limpou todos os registros de baixas do histórico", ip_address=ip)
    except Exception as e:
        return f"Erro ao limpar auditoria: {str(e)}", 500

    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("purge_all_deleted", exclude_client_id=client_id)
    return render_lot_rows(lots=[], total=0, filtro="deletados", busca="", page=1, has_more=False, next_page=2)


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
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "EDITAR_QTD", f"Alterou quantidade do lote #{lot_id} para {quant} un", target_id=lot_id, ip_address=ip)
    except Exception as e:
        return f"Erro ao salvar: {str(e)}", 500

    # Notifica outros clientes de forma assíncrona
    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("update_quant", exclude_client_id=client_id)

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
        effective_user = get_effective_user()
        ip = request.remote_addr or "127.0.0.1"
        log_audit(effective_user, "EDITAR_PRECO", f"Alterou preço do lote #{lot_id} para R$ {price:.2f}", target_id=lot_id, ip_address=ip)
    except Exception as e:
        return f"Erro ao salvar: {str(e)}", 500

    # Notifica outros clientes de forma assíncrona
    client_id = request.headers.get("X-Client-ID")
    broadcast_reload("update_price", exclude_client_id=client_id)

    return render_template("partials/price_cell.html", lot={"lot_id": lot_id, "price": price})


@app.route("/import-planilha", methods=["GET", "POST"])
def import_planilha():
    """
    Recebe uma planilha Excel (.xlsx, .xls) ou CSV e executa a auditoria linha por linha,
    com salvamento automático de cópia de segurança (backup), detecção automática universal
    e suporte a mapeamento de colunas interativo.
    """
    if request.method == "GET":
        from flask import redirect
        return redirect("/")

    from app.auditor_import import audit_and_import_spreadsheet
    from app.spreadsheet_storage import save_spreadsheet_backup, update_spreadsheet_backup_status

    if "file" not in request.files:
        return jsonify({"success": False, "error": "Nenhum arquivo enviado. Selecione uma planilha (.xlsx, .xls ou .csv)."}), 400

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return jsonify({"success": False, "error": "Nenhum arquivo selecionado. Escolha um arquivo para importar."}), 400

    filename = uploaded_file.filename
    content = uploaded_file.read()

    # Salva IMEDIATAMENTE uma cópia segura em disco antes de qualquer processamento
    backup_entry = save_spreadsheet_backup(
        content,
        filename,
        status="PROCESSANDO",
        details="Iniciando auditoria..."
    )
    file_id = backup_entry["id"]

    custom_col_prod = request.form.get("col_prod") or None
    custom_col_valid = request.form.get("col_valid") or None
    custom_col_code = request.form.get("col_code") or None
    custom_col_quant = request.form.get("col_quant") or None

    try:
        result = audit_and_import_spreadsheet(
            content,
            filename,
            custom_col_prod=custom_col_prod,
            custom_col_valid=custom_col_valid,
            custom_col_code=custom_col_code,
            custom_col_quant=custom_col_quant
        )

        # Se for necessária confirmação manual de colunas pelo usuário
        if result.get("needs_mapping"):
            update_spreadsheet_backup_status(
                file_id,
                status="MAPEAMENTO_PENDENTE",
                details=result.get("error", "Confirmação de colunas necessária")
            )
            return jsonify({
                "success": False,
                "needs_mapping": True,
                "file_id": file_id,
                "filename": filename,
                "columns": result.get("columns", []),
                "preview": result.get("preview", []),
                "suggestions": result.get("suggestions", {}),
                "message": result.get("error", "Confirmação de colunas necessária")
            }), 200

        html_summary = render_template(
            "partials/import_summary.html",
            stats=result["stats"],
            logs=result.get("logs", [])
        )
        total_rows = result["stats"].get("total_linhas", 0)
        novos_lotes = result["stats"].get("novos_lotes", 0)
        update_spreadsheet_backup_status(
            file_id,
            status="SUCESSO",
            details=f"{total_rows} linhas processadas ({novos_lotes} novas remessas)",
            total_rows=total_rows,
            novos_lotes=novos_lotes
        )
        broadcast_reload("planilha_importada")
        return jsonify({
            "success": True,
            "file_id": file_id,
            "stats": result["stats"],
            "logs": result.get("logs", []),
            "html": html_summary
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_spreadsheet_backup_status(
            file_id,
            status="ERRO",
            details=str(e)
        )
        return jsonify({"success": False, "file_id": file_id, "error": str(e)}), 400


@app.route("/download-planilha/<file_id>", methods=["GET"])
def download_planilha(file_id):
    """Permite baixar a cópia original de qualquer planilha enviada ao servidor."""
    from app.spreadsheet_storage import get_spreadsheet_backup_by_id
    res = get_spreadsheet_backup_by_id(file_id)
    if not res:
        return "Arquivo de planilha não encontrado ou expirado.", 404
    content, original_filename, meta = res
    import io
    from flask import send_file
    return send_file(
        io.BytesIO(content),
        download_name=original_filename,
        as_attachment=True
    )


@app.route("/reprocess-planilha/<file_id>", methods=["POST"])
def reprocess_planilha(file_id):
    """Re-executa a auditoria e conciliação diretamente da cópia salva no servidor sem re-upload."""
    from app.spreadsheet_storage import get_spreadsheet_backup_by_id, update_spreadsheet_backup_status
    from app.auditor_import import audit_and_import_spreadsheet

    res = get_spreadsheet_backup_by_id(file_id)
    if not res:
        return jsonify({"success": False, "error": "Planilha não encontrada no histórico de backups."}), 404
    content, original_filename, meta = res

    custom_col_prod = request.form.get("col_prod") or None
    custom_col_valid = request.form.get("col_valid") or None
    custom_col_code = request.form.get("col_code") or None
    custom_col_quant = request.form.get("col_quant") or None

    try:
        result = audit_and_import_spreadsheet(
            content,
            original_filename,
            custom_col_prod=custom_col_prod,
            custom_col_valid=custom_col_valid,
            custom_col_code=custom_col_code,
            custom_col_quant=custom_col_quant
        )

        if result.get("needs_mapping"):
            update_spreadsheet_backup_status(
                file_id,
                status="MAPEAMENTO_PENDENTE",
                details=result.get("error", "Confirmação de colunas necessária")
            )
            return jsonify({
                "success": False,
                "needs_mapping": True,
                "file_id": file_id,
                "filename": original_filename,
                "columns": result.get("columns", []),
                "preview": result.get("preview", []),
                "suggestions": result.get("suggestions", {}),
                "message": result.get("error", "Confirmação de colunas necessária")
            }), 200

        html_summary = render_template(
            "partials/import_summary.html",
            stats=result["stats"],
            logs=result.get("logs", [])
        )
        total_rows = result["stats"].get("total_linhas", 0)
        novos_lotes = result["stats"].get("novos_lotes", 0)
        update_spreadsheet_backup_status(
            file_id,
            status="SUCESSO",
            details=f"{total_rows} linhas processadas ({novos_lotes} novas remessas)",
            total_rows=total_rows,
            novos_lotes=novos_lotes
        )
        broadcast_reload("planilha_importada")
        return jsonify({
            "success": True,
            "file_id": file_id,
            "stats": result["stats"],
            "logs": result.get("logs", []),
            "html": html_summary
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_spreadsheet_backup_status(file_id, status="ERRO", details=str(e))
        return jsonify({"success": False, "file_id": file_id, "error": str(e)}), 400


@app.route("/planilhas-salvas", methods=["GET"])
def list_planilhas_salvas():
    """Retorna a lista de cópias salvas de planilhas para exibição no painel."""
    from app.spreadsheet_storage import get_spreadsheet_backups
    backups = get_spreadsheet_backups()
    if request.headers.get("HX-Request") or request.args.get("html"):
        return render_template("partials/spreadsheet_history.html", backups=backups)
    return jsonify({"success": True, "backups": backups})


@app.route("/planilhas-salvas/<file_id>/delete", methods=["POST", "DELETE"])
def delete_planilha_salva(file_id):
    """Exclui uma cópia salva do histórico."""
    from app.spreadsheet_storage import delete_spreadsheet_backup, get_spreadsheet_backups
    delete_spreadsheet_backup(file_id)
    backups = get_spreadsheet_backups()
    return render_template("partials/spreadsheet_history.html", backups=backups)


@app.route("/upload-db", methods=["POST"])
def upload_db():
    """
    Recebe um arquivo de banco de dados (products.db) enviado pelo celular ou navegador,
    substitui o arquivo local com segurança e sincroniza com PostgreSQL se ativo.
    """
    import shutil
    import subprocess
    from datetime import datetime

    if "db_file" not in request.files:
        return """
        <div id="git-status-msg" class="border border-red-400 bg-red-100 text-red-700 px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between">
            <span>❌ Nenhum arquivo enviado no formulário.</span>
            <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
        </div>
        """, 400

    file = request.files["db_file"]
    if file.filename == "":
        return """
        <div id="git-status-msg" class="border border-red-400 bg-red-100 text-red-700 px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between">
            <span>❌ Nenhum arquivo selecionado.</span>
            <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
        </div>
        """, 400

    db_target = BASE_DIR / "database" / "products.db"
    db_target.parent.mkdir(parents=True, exist_ok=True)

    content = file.read()
    if len(content) < 100 or not content.startswith(b"SQLite format 3\x00"):
        return """
        <div id="git-status-msg" class="border border-red-400 bg-red-100 text-red-700 px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between">
            <span>❌ O arquivo enviado não é um banco SQLite válido (cabeçalho incorreto).</span>
            <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
        </div>
        """, 400

    # Cria cópia de segurança
    if db_target.exists():
        backup_path = BASE_DIR / "database" / "products.db.bak"
        try:
            shutil.copy2(db_target, backup_path)
        except Exception:
            pass

    # Grava novo banco
    try:
        with open(db_target, "wb") as f:
            f.write(content)
    except Exception as e:
        return f"""
        <div id="git-status-msg" class="border border-red-400 bg-red-100 text-red-700 px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between">
            <span>❌ Erro ao salvar banco no disco: {e}</span>
            <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
        </div>
        """, 500

    # Se estiver com PostgreSQL configurado, migra os dados recebidos para o Neon Postgres
    pg_msg = ""
    if is_postgres():
        try:
            from app.migrate_sqlite_to_pg import migrate
            migrate()
            pg_msg = " e sincronizado no Neon PostgreSQL"
        except Exception as e:
            pg_msg = f" (aviso na sincronização Postgres: {e})"
    else:
        try:
            init_db()
        except Exception as e:
            print("Aviso ao reinicializar SQLite após upload:", e)

    # Notifica outros clientes via WebSocket
    broadcast_reload("db_uploaded")

    # Realiza commit e push automático do banco recebido
    git_feedback = ""
    try:
        branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=str(BASE_DIR), capture_output=True, text=True)
        current_branch = branch_res.stdout.strip() or "main"
        subprocess.run(["git", "add", "-f", "database/products.db"], cwd=str(BASE_DIR), check=True)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        subprocess.run(["git", "commit", "-m", f"chore: upload do banco products.db pelo celular [{now_str}]"], cwd=str(BASE_DIR))
        push_res = subprocess.run(["git", "push", "origin", current_branch], cwd=str(BASE_DIR), capture_output=True, text=True)
        if push_res.returncode == 0 or "Everything up-to-date" in push_res.stderr or "Everything up-to-date" in push_res.stdout:
            git_feedback = f" e enviado ao Git ({current_branch})"
        else:
            git_feedback = f" (commit local realizado, push pendente)"
    except Exception as e:
        git_feedback = f" (aviso git: {e})"

    total, _ = query_lots(limit=1)
    return f"""
    <div id="git-status-msg" class="border border-green-400 bg-green-100 text-green-800 px-4 py-3 rounded-lg text-sm mb-3 flex items-center justify-between shadow-sm">
        <span>✅ <b>Banco do celular carregado com sucesso{pg_msg}{git_feedback}!</b> Total: {total} lotes ativos.</span>
        <button onclick="this.parentElement.remove()" class="text-xs font-bold ml-2">✕</button>
    </div>
    """


@app.route("/export-git", methods=["POST"])
def export_git():
    """Sincroniza e envia alterações para o repositório remoto Git no branch atual."""
    import subprocess
    from datetime import datetime

    msg_status = ""
    sucesso = False
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    try:
        branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=str(BASE_DIR), capture_output=True, text=True)
        current_branch = branch_res.stdout.strip() or "main"

        subprocess.run(["git", "add", "-A"], cwd=str(BASE_DIR), check=True, capture_output=True, text=True)
        if (BASE_DIR / "database" / "products.db").exists():
            subprocess.run(["git", "add", "-f", "database/products.db"], cwd=str(BASE_DIR), capture_output=True, text=True)

        diff_res = subprocess.run(["git", "status", "--porcelain"], cwd=str(BASE_DIR), capture_output=True, text=True)
        has_changes = bool(diff_res.stdout.strip())

        if has_changes:
            commit_res = subprocess.run(
                ["git", "commit", "-m", f"sync: atualizacao banco/codigo [{now_str}]"],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True
            )

        push_res = subprocess.run(
            ["git", "push", "origin", current_branch],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True
        )

        if push_res.returncode == 0:
            sucesso = True
            msg_status = f"✅ Alterações sincronizadas com o GitHub com sucesso! ({now_str})"
        elif "Everything up-to-date" in push_res.stderr or "Everything up-to-date" in push_res.stdout:
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


# ── Rotas de Controle de Conexão e Cópia Local (Pull) ─────────────────────────

@app.route("/api/connection/status", methods=["GET"])
def connection_status():
    """Retorna o estado da conexão (PostgreSQL vs cópia local SQLite)."""
    return jsonify(get_db_status())


@app.route("/api/connection/pull", methods=["POST"])
def connection_pull():
    """Puxa a cópia mais atualizada do PostgreSQL para o SQLite local e testa conexão."""
    result = pull_cloud_to_sqlite()
    if result.get("success"):
        broadcast_reload("pulled")
    return jsonify(result)


@app.route("/api/connection/reconnect", methods=["POST"])
def connection_reconnect():
    """Tenta forçar a reconexão imediata com o PostgreSQL."""
    ok = reconnect_postgres()
    st = get_db_status()
    st["reconnected"] = ok
    if ok:
        broadcast_reload("reconnected")
    return jsonify(st)


# ── Rotas e Agendador de Alertas Automáticos de Validade ──────────────────────

from app.db import get_alert_settings, save_alert_settings


@app.route("/api/alerts/settings", methods=["GET"])
def api_get_alert_settings():
    """Retorna as configurações atuais de alerta."""
    st = get_alert_settings()
    return jsonify(st), 200


@app.route("/api/alerts/settings", methods=["POST"])
def api_save_alert_settings():
    """Salva as configurações de alerta enviadas pela interface."""
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    if not data:
        return jsonify({"success": False, "error": "Dados inválidos."}), 400
    ok = save_alert_settings(data)
    if ok:
        return jsonify({"success": True, "settings": get_alert_settings()}), 200
    return jsonify({"success": False, "error": "Erro ao persistir configurações."}), 500


@app.route("/api/alerts/test", methods=["POST"])
def api_test_alert():
    """Executa um disparo de teste imediato com as credenciais fornecidas."""
    from app.alerts import dispatch_alert
    data = request.get_json(force=True, silent=True)
    if data:
        save_alert_settings(data)
    success, msg = dispatch_alert(is_test=True)
    return jsonify({"success": success, "message": msg, "settings": get_alert_settings()}), 200


@app.route("/api/alerts/trigger", methods=["POST"])
def api_trigger_alert_manual():
    """Dispara o alerta diário manualmente no momento atual."""
    from app.alerts import dispatch_alert
    success, msg = dispatch_alert(is_test=False)
    return jsonify({"success": success, "message": msg, "settings": get_alert_settings()}), 200


# Background Scheduler Thread: verifica horário matinal a cada 30 segundos
def _alert_scheduler_loop():
    while True:
        try:
            time.sleep(30)
            from app.alerts import check_and_run_scheduled_alert
            check_and_run_scheduled_alert()
        except Exception as e:
            time.sleep(30)

threading.Thread(target=_alert_scheduler_loop, daemon=True, name="AlertScheduler").start()


# ── Entry point ───────────────────────────────────────────────────────────────


# ── Rotas de Autenticação, Troca de Operador e Auditoria ────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_route():
    if session.get("user_id"):
        return redirect("/")

    error = None
    if request.method == "POST":
        login_type = request.form.get("login_type", "password")
        ip = request.remote_addr or "127.0.0.1"

        if login_type == "pin":
            pin = request.form.get("pin", "").strip()
            user = get_user_by_pin(pin)
            if user:
                session.permanent = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["name"] = user["name"]
                session["role"] = user["role"]
                session["active_operator"] = user["name"]
                log_audit(user["name"], "LOGIN", f"Acesso rápido via PIN ({user['role']})", user_id=user["id"], ip_address=ip)
                return redirect("/")
            else:
                error = "PIN incorreto ou operador inativo."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_user_by_username(username)
            if user and check_password_hash(user["password_hash"], password):
                if not user["active"]:
                    error = "Esta conta de usuário está desativada."
                else:
                    session.permanent = True
                    session["user_id"] = user["id"]
                    session["username"] = user["username"]
                    session["name"] = user["name"]
                    session["role"] = user["role"]
                    session["active_operator"] = user["name"]
                    log_audit(user["name"], "LOGIN", f"Login com senha ({user['role']})", user_id=user["id"], ip_address=ip)
                    return redirect("/")
            else:
                error = "Usuário ou senha incorretos."

    return render_template("login.html", error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout_route():
    ip = request.remote_addr or "127.0.0.1"
    name = session.get("name") or session.get("username") or "Usuário"
    u_id = session.get("user_id")
    log_audit(name, "LOGOUT", "Encerrou a sessão no sistema", user_id=u_id, ip_address=ip)
    session.clear()
    return redirect("/login")


@app.route("/api/operator/switch", methods=["POST"])
def switch_operator_route():
    data = request.get_json(silent=True) or request.form
    pin = (data.get("pin") or "").strip()
    if not pin:
        return jsonify({"success": False, "error": "PIN é obrigatório."}), 400

    user = get_user_by_pin(pin)
    if not user:
        return jsonify({"success": False, "error": "PIN não encontrado ou operador inativo."}), 401

    old_op = session.get("active_operator", "Desconhecido")
    session["active_operator"] = user["name"]
    session["active_operator_username"] = user["username"]
    ip = request.remote_addr or "127.0.0.1"
    log_audit(user["name"], "TROCA_OPERADOR", f"Trocou operador ativo no chão de loja (de '{old_op}' para '{user['name']}')", user_id=user["id"], ip_address=ip)
    return jsonify({"success": True, "active_operator": user["name"], "operator_name": user["name"]})


@app.route("/api/audit/logs", methods=["GET"])
def audit_logs_api():
    if session.get("role") != "admin":
        return jsonify({"error": "Acesso não autorizado"}), 403

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    user = request.args.get("user", "").strip() or None
    action = request.args.get("action", "").strip() or None
    search = request.args.get("search", "").strip() or None
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None

    total, logs = query_audit_logs(limit=limit, offset=offset, user=user, action=action, search=search, start_date=start_date, end_date=end_date)
    return jsonify({"total": total, "logs": logs})


@app.route("/api/audit/export", methods=["GET"])
def audit_export_api():
    if session.get("role") != "admin":
        return redirect("/login")

    user = request.args.get("user", "").strip() or None
    action = request.args.get("action", "").strip() or None
    search = request.args.get("search", "").strip() or None
    total, logs = query_audit_logs(limit=5000, offset=0, user=user, action=action, search=search)

    import io
    import csv
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Data/Hora", "Quem Mexeu", "Ação", "Detalhes", "Produto Relacionado", "IP Origem"])
    for l in logs:
        writer.writerow([
            l["id"],
            l["created_at"],
            l["username"],
            l["action"],
            l["details"],
            l["product_name"] or "",
            l["ip_address"] or ""
        ])

    csv_data = output.getvalue().encode("utf-8")
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=auditoria_datelimit_{date.today().strftime('%Y%m%d')}.csv"}
    )


@app.route("/api/users", methods=["GET", "POST"])
def users_api():
    if session.get("role") != "admin":
        return jsonify({"error": "Acesso não autorizado"}), 403

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        username = (data.get("username") or "").strip()
        password = data.get("password", "")
        pin = (data.get("pin") or "").strip()
        role = data.get("role", "operador")

        ok, msg = create_user(username, name, password, pin=pin, role=role)
        if ok:
            ip = request.remote_addr or "127.0.0.1"
            curr_user = session.get("name") or "Admin"
            log_audit(curr_user, "CRIAR_USUARIO", f"Cadastrou novo usuário '@{username}' ({name}) como {role}", ip_address=ip)
            u = get_user_by_username(username)
            return jsonify({"success": True, "message": msg, "user": u})
        return jsonify({"success": False, "error": msg}), 400

    users = list_users()
    return jsonify(users)


@app.route("/api/users/<int:user_id>", methods=["PUT", "POST"])
@app.route("/api/users/<int:user_id>/update", methods=["POST"])
def user_update_api(user_id: int):
    if session.get("role") != "admin" and session.get("user_id") != user_id:
        return jsonify({"error": "Acesso não autorizado"}), 403

    data = request.get_json(silent=True) or request.form
    name = data.get("name")
    password = data.get("password") or None
    pin = data.get("pin")
    role = data.get("role") if session.get("role") == "admin" else None
    active = data.get("active") if session.get("role") == "admin" else None

    ok, msg = update_user(user_id, name=name, password=password, pin=pin, role=role, active=active)
    if ok:
        ip = request.remote_addr or "127.0.0.1"
        curr_user = session.get("name") or "Admin"
        log_audit(curr_user, "EDITAR_USUARIO", f"Atualizou cadastro do usuário ID #{user_id}", target_id=user_id, ip_address=ip)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@app.route("/api/users/<int:user_id>/delete", methods=["POST", "DELETE"])
def user_delete_api(user_id: int):
    if session.get("role") != "admin":
        return jsonify({"error": "Acesso não autorizado"}), 403

    ok, msg = delete_user(user_id)
    if ok:
        ip = request.remote_addr or "127.0.0.1"
        curr_user = session.get("name") or "Admin"
        log_audit(curr_user, "EXCLUIR_USUARIO", f"Excluiu usuário ID #{user_id}", target_id=user_id, ip_address=ip)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400




# ── Rota do Painel Administrativo Dedicado ─────────────────────────────────────

@app.route("/admin")
@app.route("/admin/painel")
def admin_panel_route():
    if not session.get("user_id"):
        return redirect("/login")
    if session.get("role") != "admin":
        return Response("Acesso restrito a administradores", 403)

    admin_id = session.get("user_id")
    admin_user = get_user_by_id(admin_id)
    if not admin_user:
        return redirect("/login")

    # Se o admin ainda não tiver token gerado, gera um automaticamente
    if not admin_user.get("api_token"):
        new_tok = generate_api_token()
        update_user(admin_id, api_token=new_tok)
        admin_user["api_token"] = new_tok

    users_list = list_users()
    db_status = get_db_status()
    total_lots, _ = query_lots(limit=1)
    total_audit, _ = query_audit_logs(limit=1)

    return render_template(
        "admin_panel.html",
        admin=admin_user,
        users=users_list,
        db_status=db_status,
        total_lots=total_lots,
        total_audit=total_audit
    )


@app.route("/api/admin/profile", methods=["POST"])
def admin_profile_update():
    if not session.get("user_id") or session.get("role") != "admin":
        return jsonify({"error": "Acesso não autorizado"}), 403

    admin_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip() or None
    pin = data.get("pin")
    api_token = (data.get("api_token") or "").strip() or None

    if not name:
        return jsonify({"success": False, "error": "Nome completo não pode ficar em branco."}), 400
    if not username:
        return jsonify({"success": False, "error": "Nome de usuário não pode ficar em branco."}), 400

    ok, msg = update_user(
        admin_id,
        name=name,
        username=username,
        password=password,
        pin=pin,
        api_token=api_token
    )
    if ok:
        # Atualiza a sessão ativa imediatamente para refletir na interface
        session["name"] = name
        session["username"] = username
        if session.get("active_operator") == session.get("name"):
            session["active_operator"] = name

        ip = request.remote_addr or "127.0.0.1"
        log_audit(
            name,
            "PERFIL_ADMIN_ATUALIZADO",
            f"Admin atualizou dados de cadastro (Nome: '{name}', Login: '@{username}')",
            user_id=admin_id,
            ip_address=ip
        )
        updated_user = get_user_by_id(admin_id)
        return jsonify({"success": True, "message": "Perfil de administrador atualizado com sucesso!", "admin": updated_user})
    return jsonify({"success": False, "error": msg}), 400


@app.route("/api/admin/generate-token", methods=["POST"])
def admin_generate_token_route():
    if not session.get("user_id") or session.get("role") != "admin":
        return jsonify({"error": "Acesso não autorizado"}), 403

    token = generate_api_token()
    return jsonify({"success": True, "token": token})

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
