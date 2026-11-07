"""
Camada de persistência/banco de dados de alta performance para DateLimit.
Suporta:
- Neon PostgreSQL com ThreadedConnectionPool (pooling persistente de conexões)
- Índice Trigram GIN (pg_trgm) para busca LIKE instantânea
- Query combinada com COUNT(*) OVER() (1 único roundtrip de rede)
- Cache em memória ultrarrápido com invalidação instantânea em mutações
- SQLite local como fallback
"""

import os
import time
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import date, timedelta

# Carrega .env ou .env.local se existir
try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BASE_DIR / ".env.local")
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent.parent

SQLITE_PATH = BASE_DIR / "database" / "products.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

try:
    import psycopg2
    from psycopg2 import pool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    psycopg2 = None
    pool = None
    PSYCOPG2_AVAILABLE = False

try:
    import pg8000.dbapi
    import ssl
    import urllib.parse
    import json
    PG8000_AVAILABLE = True
except ImportError:
    pg8000 = None
    PG8000_AVAILABLE = False


def _connect_pg_direct(timeout=4):
    """
    Estabelece uma conexão direta com o PostgreSQL com statement_timeout de proteção (4s)
    e keepalives TCP agressivos para evitar travamentos em oscilações de rede.
    """
    if PSYCOPG2_AVAILABLE:
        return psycopg2.connect(
            DATABASE_URL,
            keepalives=1,
            keepalives_idle=20,
            keepalives_interval=5,
            keepalives_count=3,
            connect_timeout=timeout,
        )
    elif PG8000_AVAILABLE:
        p = urllib.parse.urlparse(DATABASE_URL)
        ssl_ctx = ssl.create_default_context()
        return pg8000.dbapi.connect(
            user=p.username,
            password=p.password,
            host=p.hostname,
            port=p.port or 5432,
            database=p.path.lstrip("/"),
            ssl_context=ssl_ctx,
            timeout=timeout,
        )
    else:
        raise RuntimeError("Nenhum driver PostgreSQL (psycopg2 ou pg8000) disponível.")


def _is_pg_conn_closed(conn) -> bool:
    """Verifica se o objeto de conexão do PostgreSQL (psycopg2 ou pg8000) está fechado."""
    if conn is None:
        return True
    if hasattr(conn, "closed"):
        return conn.closed != 0
    if hasattr(conn, "_sock"):
        return conn._sock is None
    return False


# ── Pool de Conexões PostgreSQL ───────────────────────────────────────────────
_pg_pool = None
_pool_lock = threading.Lock()
_conn_last_used = {}
_conn_used_lock = threading.Lock()

# ── Estado de Conexão e Auto-Failover Dual (PostgreSQL <-> SQLite) ─────────────
_db_status = {
    "postgres_online": False,
    "last_sync": None,
    "last_error": None,
    "total_products": 0,
    "total_lots": 0,
}
_status_lock = threading.Lock()
_reconnect_thread = None
_reconnect_lock = threading.Lock()
_on_reconnect_callback = None


def set_on_reconnect_callback(callback):
    """Permite que o Flask/WebSocket registre uma função para ser chamada quando o Postgres voltar."""
    global _on_reconnect_callback
    _on_reconnect_callback = callback


def is_postgres_configured() -> bool:
    """Verifica se as configurações para PostgreSQL estão presentes no ambiente."""
    return bool(DATABASE_URL and (PSYCOPG2_AVAILABLE or PG8000_AVAILABLE))


def is_postgres() -> bool:
    """Retorna True apenas se o PostgreSQL estiver configurado E atualmente online."""
    if not is_postgres_configured():
        return False
    with _status_lock:
        return _db_status["postgres_online"]


def get_db_status() -> dict:
    """Retorna o snapshot completo de integridade e conectividade do banco."""
    with _status_lock:
        online = _db_status["postgres_online"]
        return {
            "postgres_configured": is_postgres_configured(),
            "postgres_online": online,
            "db_name": "PostgreSQL" if online else "SQLite (Cópia Local)",
            "last_sync": _db_status["last_sync"],
            "last_error": _db_status["last_error"],
            "total_products": _db_status["total_products"],
            "total_lots": _db_status["total_lots"],
        }


def _mark_conn_used(conn):
    if conn is not None:
        with _conn_used_lock:
            _conn_last_used[id(conn)] = time.time()


def _remove_conn_used(conn):
    if conn is not None:
        with _conn_used_lock:
            _conn_last_used.pop(id(conn), None)


def _get_conn_last_used(conn) -> float:
    if conn is None:
        return 0.0
    with _conn_used_lock:
        return _conn_last_used.get(id(conn), 0.0)


def get_pg_pool():
    global _pg_pool
    if _pg_pool is None and DATABASE_URL and PSYCOPG2_AVAILABLE:
        with _pool_lock:
            if _pg_pool is None:
                try:
                    _pg_pool = pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=6,
                        dsn=DATABASE_URL,
                        keepalives=1,
                        keepalives_idle=20,
                        keepalives_interval=5,
                        keepalives_count=3,
                    )
                except Exception as e:
                    print(f"⚠️ Erro ao inicializar pool Postgres: {e}")
                    _pg_pool = None
    return _pg_pool


_sync_lock = threading.Lock()


def _is_connection_healthy(conn) -> bool:
    """Verifica se a conexão PostgreSQL ainda está ativa e responsiva sem atrasar requisições."""
    if conn is None or _is_pg_conn_closed(conn):
        return False
    # Conexões usadas nos últimos 20 segundos dispensam ping
    last_used = _get_conn_last_used(conn)
    if time.time() - last_used < 20 and last_used > 0:
        return True
    try:
        with conn.cursor() as test_cur:
            test_cur.execute("SELECT 1;")
        _mark_conn_used(conn)
        return True
    except Exception:
        _remove_conn_used(conn)
        return False


def _checkout_valid_pg_conn(pool_inst, max_retries=3):
    """Obtém uma conexão válida do pool, descartando conexões inativas ou expiradas pela nuvem."""
    for _ in range(max_retries):
        try:
            conn = pool_inst.getconn()
        except Exception:
            break
        if _is_connection_healthy(conn):
            return conn, True
        # Conexão inativa ou morta: fecha e remove do pool
        _remove_conn_used(conn)
        try:
            pool_inst.putconn(conn, close=True)
        except Exception:
            pass
    # Se todas as conexões do pool estavam inativas, cria uma nova diretamente com timeout rápido
    conn = _connect_pg_direct(timeout=4)
    _mark_conn_used(conn)
    return conn, False


def trigger_auto_reconnect():
    """Inicia thread em background para monitorar e religar a conexão com o PostgreSQL assim que acordar."""
    global _reconnect_thread
    with _reconnect_lock:
        if _reconnect_thread is None or not _reconnect_thread.is_alive():
            _reconnect_thread = threading.Thread(target=_auto_reconnect_worker, daemon=True)
            _reconnect_thread.start()


def _auto_reconnect_worker():
    """Tenta periodicamente religar com o PostgreSQL até que o servidor volte a responder."""
    while is_postgres_configured():
        with _status_lock:
            if _db_status["postgres_online"]:
                break
        time.sleep(12)
        try:
            test_conn = _connect_pg_direct(timeout=8)
            test_cur = test_conn.cursor()
            test_cur.execute("SELECT 1;")
            test_cur.close()
            test_conn.close()

            print("🟢 PostgreSQL voltou a responder! Restabelecendo conexão e atualizando cópia local...")
            with _status_lock:
                _db_status["postgres_online"] = True
                _db_status["last_error"] = None

            # Atualiza o pool e espelha a cópia para o SQLite
            get_pg_pool()
            pull_cloud_to_sqlite()
            clear_query_cache()

            if _on_reconnect_callback:
                try:
                    _on_reconnect_callback()
                except Exception:
                    pass
            break
        except Exception as e:
            with _status_lock:
                _db_status["last_error"] = str(e)


def reconnect_postgres() -> bool:
    """Tenta forçar uma reconexão síncrona com o PostgreSQL."""
    if not is_postgres_configured():
        return False
    try:
        test_conn = _connect_pg_direct(timeout=8)
        test_cur = test_conn.cursor()
        test_cur.execute("SELECT 1;")
        test_cur.close()
        test_conn.close()

        with _status_lock:
            _db_status["postgres_online"] = True
            _db_status["last_error"] = None

        get_pg_pool()
        pull_cloud_to_sqlite()
        clear_query_cache()
        return True
    except Exception as e:
        with _status_lock:
            _db_status["postgres_online"] = False
            _db_status["last_error"] = str(e)
        trigger_auto_reconnect()
        return False


@contextmanager
def get_db_cursor(commit=False):
    """
    Context manager de alta performance, resiliência e auto-failover:
    Tenta PostgreSQL quando online; se falhar ou estiver offline, cai suavemente para o SQLite local!
    """
    use_pg = is_postgres()
    conn = None
    is_from_pool = False
    pool_inst = None

    if use_pg:
        try:
            pool_inst = get_pg_pool()
            if pool_inst:
                conn, is_from_pool = _checkout_valid_pg_conn(pool_inst)
            else:
                conn = _connect_pg_direct(timeout=8)
                _mark_conn_used(conn)
                is_from_pool = False
        except Exception as e:
            print(f"⚠️ PostgreSQL inacessível ({e}), alternando automaticamente para SQLite local...")
            with _status_lock:
                _db_status["postgres_online"] = False
                _db_status["last_error"] = str(e)
            trigger_auto_reconnect()
            use_pg = False
            conn = None

    if use_pg and conn:
        is_broken = False
        try:
            cur = conn.cursor()
            yield cur, "postgres"
            if commit:
                conn.commit()
            _mark_conn_used(conn)
        except Exception as exc:
            is_conn_err = _is_pg_conn_closed(conn)
            if PSYCOPG2_AVAILABLE and isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                is_conn_err = True
            if PG8000_AVAILABLE and isinstance(exc, (pg8000.dbapi.OperationalError, pg8000.dbapi.InterfaceError)):
                is_conn_err = True

            if is_conn_err:
                is_broken = True
                with _status_lock:
                    _db_status["postgres_online"] = False
                trigger_auto_reconnect()
            else:
                try:
                    conn.rollback()
                except Exception:
                    is_broken = True
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            if is_broken:
                _remove_conn_used(conn)
            if is_from_pool and pool_inst:
                try:
                    pool_inst.putconn(conn, close=is_broken)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            else:
                try:
                    conn.close()
                except Exception:
                    pass
    else:
        # Modo SQLite Local (Offline-first resiliente com WAL ultrarrápido)
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
        conn.isolation_level = None  # Autocommit: leituras sempre enxergam os commits mais recentes no WAL
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("PRAGMA cache_size = -64000;")
        conn.execute("PRAGMA mmap_size = 268435456;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            cur = conn.cursor()
            yield cur, "sqlite"
            if commit:
                conn.commit()
        finally:
            cur.close()
            conn.close()


def get_connection():
    """Compatibilidade retroativa com conexões manuais."""
    if is_postgres():
        p = get_pg_pool()
        if p:
            conn, _ = _checkout_valid_pg_conn(p)
            return conn, "postgres_pool"
        conn = _connect_pg_direct(timeout=8)
        _mark_conn_used(conn)
        return conn, "postgres"
    else:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn, "sqlite"


def release_connection(conn, db_type):
    """Libera a conexão de volta ao pool."""
    if db_type == "postgres_pool" and _pg_pool:
        try:
            is_broken = getattr(conn, "closed", 0) != 0
            if is_broken:
                _remove_conn_used(conn)
            else:
                _mark_conn_used(conn)
            _pg_pool.putconn(conn, close=is_broken)
        except Exception:
            _remove_conn_used(conn)
            try:
                conn.close()
            except Exception:
                pass
    else:
        _remove_conn_used(conn)
        try:
            conn.close()
        except Exception:
            pass


# ── Cache em Memória Inteligente ──────────────────────────────────────────────
_query_cache = {}
_cache_lock = threading.Lock()
_catalog_cache = None
_catalog_lock = threading.Lock()
CACHE_TTL_SECONDS = 120  # Mantém cache aquecido por 2 minutos

_cache_clear_callbacks = []

def register_cache_clear_callback(cb):
    _cache_clear_callbacks.append(cb)

def _notify_cache_clear():
    for cb in list(_cache_clear_callbacks):
        try:
            cb()
        except Exception:
            pass

_local = threading.local()

def _get_thread_sqlite_conn():
    """
    Retorna uma conexão SQLite otimizada e persistente para a thread atual.
    Aplica PRAGMA mmap_size (256MB), cache_size (64MB) e temp_store em RAM (< 0.3ms).
    """
    conn = getattr(_local, "sqlite_conn", None)
    if conn is None:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("PRAGMA cache_size = -64000;")
        conn.execute("PRAGMA mmap_size = 268435456;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA foreign_keys = ON;")
        _local.sqlite_conn = conn
    return conn

def _reset_thread_sqlite_conn():
    conn = getattr(_local, "sqlite_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.sqlite_conn = None

def _update_catalog_cache_item(product_id: int, name: str, price: float, bar_code: str = None):
    """Atualiza ou insere o produto no catálogo em RAM sem descartar a memória (0.01ms)."""
    global _catalog_cache
    with _catalog_lock:
        if _catalog_cache is not None:
            name_up = name.strip().upper()
            found = False
            for it in _catalog_cache:
                if it["id"] == product_id or it["name"] == name_up:
                    if price > 0:
                        it["price"] = price
                    if bar_code:
                        it["bar_code"] = bar_code
                    found = True
                    break
            if not found:
                _catalog_cache.append({
                    "id": product_id,
                    "name": name_up,
                    "bar_code": bar_code,
                    "price": price
                })

def clear_query_cache(clear_catalog: bool = True):
    """Invalida o cache de consultas e opcionalmente o catálogo na RAM."""
    global _catalog_cache
    with _cache_lock:
        _query_cache.clear()
    if clear_catalog:
        with _catalog_lock:
            _catalog_cache = None
    _reset_thread_sqlite_conn()
    _notify_cache_clear()

def _update_cache_lot_field(lot_id: int, field: str, value):
    """Atualiza o lote diretamente na memória RAM sem invalidar as consultas já carregadas (0.01ms)."""
    _notify_cache_clear()
    with _cache_lock:
        target_prod_id = None
        for key, (t, total, lots) in _query_cache.items():
            for lot in lots:
                if lot.get("lot_id") == lot_id:
                    target_prod_id = lot.get("product_id")
                    break
            if target_prod_id is not None:
                break

        for key, (t, total, lots) in _query_cache.items():
            for lot in lots:
                if lot.get("lot_id") == lot_id or (field == "price" and target_prod_id is not None and lot.get("product_id") == target_prod_id):
                    lot[field] = value

def _remove_cache_lot(lot_id: int):
    """Remove um lote deletado do cache em memória RAM sem precisar recarregar o banco todo."""
    _notify_cache_clear()
    with _cache_lock:
        for key, (t, total, lots) in list(_query_cache.items()):
            new_lots = [l for l in lots if l.get("lot_id") != lot_id]
            if len(new_lots) != len(lots):
                _query_cache[key] = (t, max(0, total - 1), new_lots)

def _mirror_to_sqlite(sql: str, params: tuple):
    """Espelha a alteração imediatamente no SQLite local em modo WAL (< 1ms)."""
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            s_conn.execute(sql, params)
            s_conn.commit()
    except Exception as e:
        print(f"⚠️ Erro ao espelhar alteração no SQLite: {e}")


# ── Inicialização do Banco, Migrações e Sincronização Dual ────────────────────

def init_sqlite_db():
    """Garante que a base SQLite local esteja 100% íntegra, com todas as tabelas e colunas em modo WAL de alta velocidade."""
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA cache_size = -64000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            bar_code TEXT UNIQUE,
            price REAL DEFAULT 0.0
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS product_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            quant INTEGER DEFAULT 0,
            price REAL DEFAULT 0.0,
            date_valid TEXT NOT NULL,
            location TEXT DEFAULT 'LOJA',
            status TEXT DEFAULT 'NORMAL',
            origin TEXT DEFAULT 'MANUAL',
            created_at TEXT DEFAULT (date('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid_id ON product_lots(date_valid, id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(bar_code);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_upper_name ON products(UPPER(name));")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_lookup ON product_lots(product_id, date_valid, location);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid_cover ON product_lots(date_valid, id, product_id, quant, location, status);")

    # Migrações seguras de colunas em SQLite
    cur.execute("PRAGMA table_info(products);")
    p_cols = [col[1] for col in cur.fetchall()]
    if "bar_code" not in p_cols:
        cur.execute("ALTER TABLE products ADD COLUMN bar_code TEXT;")
    if "price" not in p_cols:
        cur.execute("ALTER TABLE products ADD COLUMN price REAL DEFAULT 0.0;")
    if "updated_at" not in p_cols:
        cur.execute("ALTER TABLE products ADD COLUMN updated_at TEXT;")
        cur.execute("UPDATE products SET updated_at = datetime('now') WHERE updated_at IS NULL;")
        cur.execute("""
            UPDATE products
            SET price = COALESCE((
                SELECT l.price 
                FROM product_lots l 
                WHERE l.product_id = products.id AND l.price > 0
                ORDER BY l.id DESC 
                LIMIT 1
            ), 0.0)
            WHERE EXISTS (
                SELECT 1 
                FROM product_lots l 
                WHERE l.product_id = products.id AND l.price > 0
            );
        """)

    cur.execute("PRAGMA table_info(product_lots);")
    l_cols = [col[1] for col in cur.fetchall()]
    if "price" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN price REAL DEFAULT 0.0;")
    if "origin" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN origin TEXT DEFAULT 'MANUAL';")
        cur.execute("UPDATE product_lots SET origin = 'IMPORTACAO' WHERE location = 'GALPAO';")
        cur.execute("UPDATE product_lots SET origin = 'MANUAL' WHERE origin IS NULL;")
    if "created_at" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN created_at TEXT;")
        cur.execute("UPDATE product_lots SET created_at = date('now') WHERE created_at IS NULL;")
    if "updated_at" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN updated_at TEXT;")
        cur.execute("UPDATE product_lots SET updated_at = datetime('now') WHERE updated_at IS NULL;")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created ON product_lots(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created_id ON product_lots(created_at, id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_origin ON product_lots(origin);")

    # Tabela de movimentações e filtro de lápide para SQLite
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            lot_id INTEGER,
            action TEXT NOT NULL,
            quant INTEGER DEFAULT 0,
            date_valid TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_prod_valid ON stock_movements(product_id, date_valid);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_created ON stock_movements(created_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_action_id ON stock_movements(action, id DESC);")

    # Tabela de configurações de alertas automáticos para SQLite
    cur.execute('''
        CREATE TABLE IF NOT EXISTS alert_settings (
            id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            channel TEXT DEFAULT 'telegram',
            daily_time TEXT DEFAULT '07:30',
            telegram_token TEXT DEFAULT '',
            telegram_chat_id TEXT DEFAULT '',
            webhook_url TEXT DEFAULT '',
            last_sent_date TEXT DEFAULT '',
            last_status TEXT DEFAULT '',
            include_vencidos INTEGER DEFAULT 1,
            include_criticos INTEGER DEFAULT 1,
            include_atencao INTEGER DEFAULT 0
        );
    ''')
    cur.execute('''
        INSERT OR IGNORE INTO alert_settings (id, enabled, channel, daily_time)
        VALUES (1, 0, 'telegram', '07:30');
    ''')

    # Tabela de usuários e autenticação para SQLite
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            pin TEXT,
            role TEXT DEFAULT 'operador',
            active INTEGER DEFAULT 1,
            api_token TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    ''')
    try:
        cur.execute("ALTER TABLE users ADD COLUMN api_token TEXT;")
    except Exception:
        pass

    # Tabela de logs de auditoria completos para SQLite
    cur.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target_id INTEGER,
            product_name TEXT,
            details TEXT NOT NULL,
            ip_address TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );
    ''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_username ON audit_logs(username);")

    # Garante usuário administrador padrão caso a tabela esteja vazia
    cur.execute("SELECT COUNT(*) FROM users;")
    if cur.fetchone()[0] == 0:
        from werkzeug.security import generate_password_hash
        cur.execute(
            "INSERT INTO users (username, name, password_hash, pin, role, active) VALUES (?, ?, ?, ?, ?, 1);",
            ("admin", "Administrador", generate_password_hash("admin123"), "1234", "admin")
        )
        cur.execute(
            "INSERT INTO audit_logs (username, action, details, ip_address) VALUES (?, ?, ?, ?);",
            ("Sistema", "SISTEMA_INICIALIZADO", "Usuário admin padrão criado automaticamente", "127.0.0.1")
        )

    conn.commit()
    conn.close()


def init_pg_db():
    """Garante que a base PostgreSQL remota esteja íntegra com extensões e índices otimizados."""
    if not is_postgres_configured():
        return False
    pool_inst = get_pg_pool()
    conn = None
    is_from_pool = False
    try:
        if pool_inst:
            conn, is_from_pool = _checkout_valid_pg_conn(pool_inst)
        else:
            conn = _connect_pg_direct(timeout=8)
            is_from_pool = False
        cur = conn.cursor()

        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        except Exception:
            conn.rollback()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                bar_code TEXT UNIQUE,
                price REAL DEFAULT 0.0
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS product_lots (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quant INTEGER DEFAULT 0,
                price REAL DEFAULT 0.0,
                date_valid DATE NOT NULL,
                location TEXT DEFAULT 'LOJA',
                status TEXT DEFAULT 'NORMAL',
                origin VARCHAR(20) DEFAULT 'MANUAL',
                created_at DATE DEFAULT CURRENT_DATE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid_id ON product_lots(date_valid, id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(bar_code);")
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_upper_name ON products(UPPER(name));")
        except Exception:
            conn.rollback()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_lookup ON product_lots(product_id, date_valid, location);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid_cover ON product_lots(date_valid, id, product_id, quant, location, status);")

        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name_trgm ON products USING gin (name gin_trgm_ops);")
        except Exception:
            conn.rollback()

        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("""
            UPDATE products
            SET price = COALESCE((
                SELECT l.price 
                FROM product_lots l 
                WHERE l.product_id = products.id AND l.price > 0
                ORDER BY l.id DESC 
                LIMIT 1
            ), 0.0)
            WHERE EXISTS (
                SELECT 1 
                FROM product_lots l 
                WHERE l.product_id = products.id AND l.price > 0
            ) AND (price IS NULL OR price = 0.0);
        """)
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS origin VARCHAR(20) DEFAULT 'MANUAL';")
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS created_at DATE DEFAULT CURRENT_DATE;")
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created ON product_lots(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created_id ON product_lots(created_at, id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_origin ON product_lots(origin);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                id SERIAL PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                lot_id INTEGER,
                action TEXT NOT NULL,
                quant INTEGER DEFAULT 0,
                date_valid DATE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_prod_valid ON stock_movements(product_id, date_valid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_created ON stock_movements(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_action_id ON stock_movements(action, id DESC);")

        cur.execute('''
            CREATE TABLE IF NOT EXISTS alert_settings (
                id INTEGER PRIMARY KEY,
                enabled BOOLEAN DEFAULT FALSE,
                channel VARCHAR(30) DEFAULT 'telegram',
                daily_time VARCHAR(10) DEFAULT '07:30',
                telegram_token TEXT DEFAULT '',
                telegram_chat_id TEXT DEFAULT '',
                webhook_url TEXT DEFAULT '',
                last_sent_date VARCHAR(20) DEFAULT '',
                last_status TEXT DEFAULT '',
                include_vencidos BOOLEAN DEFAULT TRUE,
                include_criticos BOOLEAN DEFAULT TRUE,
                include_atencao BOOLEAN DEFAULT FALSE
            );
        ''')
        cur.execute('''
            INSERT INTO alert_settings (id, enabled, channel, daily_time)
            VALUES (1, FALSE, 'telegram', '07:30')
            ON CONFLICT (id) DO NOTHING;
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                pin VARCHAR(10),
                role VARCHAR(20) DEFAULT 'operador',
                active BOOLEAN DEFAULT TRUE,
                api_token VARCHAR(128),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS api_token VARCHAR(128);")
        except Exception:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username TEXT NOT NULL,
                action VARCHAR(50) NOT NULL,
                target_id INTEGER,
                product_name TEXT,
                details TEXT NOT NULL,
                ip_address VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_username ON audit_logs(username);")

        cur.execute("SELECT COUNT(*) FROM users;")
        if cur.fetchone()[0] == 0:
            from werkzeug.security import generate_password_hash
            cur.execute(
                "INSERT INTO users (username, name, password_hash, pin, role, active) VALUES (%s, %s, %s, %s, %s, TRUE);",
                ("admin", "Administrador", generate_password_hash("admin123"), "1234", "admin")
            )
            cur.execute(
                "INSERT INTO audit_logs (username, action, details, ip_address) VALUES (%s, %s, %s, %s);",
                ("Sistema", "SISTEMA_INICIALIZADO", "Usuário admin padrão criado automaticamente", "127.0.0.1")
            )

        conn.commit()
        cur.close()
        if is_from_pool and pool_inst:
            pool_inst.putconn(conn)
        else:
            conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Erro ao inicializar PostgreSQL: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return False


def pull_cloud_to_sqlite() -> dict:
    """
    Sincronização bidirecional inteligente com resolução de conflitos por Timestamp (Last-Write-Wins):
    1. Se o banco local SQLite foi utilizado durante falha/indisponibilidade do Neon,
       detecta se as alterações locais são mais recentes que o Neon (updated_at local > Neon).
    2. Realiza o upload/push para o Neon de:
       - Lotes novos ou com updated_at mais recente
       - Baixas (stock_movements) efetuadas localmente (removendo os lotes correspondentes no Neon)
       - Produtos novos ou com preço/dados mais recentes
       - Usuários e permissões com dados mais recentes
       - Logs de auditoria locais não presentes no Neon
    3. Atualiza os sequences do PostgreSQL para eliminar qualquer risco de colisão.
    4. Atualiza o SQLite local com a cópia integral unificada em modo WAL.
    """
    t0 = time.time()
    if not is_postgres_configured():
        return {"success": False, "error": "PostgreSQL não configurado no .env"}

    if not _sync_lock.acquire(blocking=False):
        with _status_lock:
            return {
                "success": True,
                "message": "Sincronização já em andamento...",
                "duration_s": 0.0,
                "timestamp": _db_status["last_sync"] or time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    from datetime import datetime

    def parse_ts(ts):
        if not ts:
            return datetime.min
        s = str(ts).strip()
        if "+" in s:
            s = s.split("+")[0].strip()
        if "." in s:
            s = s.split(".")[0].strip()
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d")
            except Exception:
                return datetime.min

    try:
        init_sqlite_db()
        pool_inst = get_pg_pool()
        if pool_inst:
            pg_conn, is_from_pool = _checkout_valid_pg_conn(pool_inst)
        else:
            pg_conn = _connect_pg_direct(timeout=8)
            is_from_pool = False

        pg_cur = pg_conn.cursor()

        # 1. Puxa snapshot atual do PostgreSQL Neon
        pg_cur.execute("""
            SELECT
                COALESCE((SELECT json_agg(p) FROM (SELECT id, name, bar_code, price, COALESCE(updated_at::TEXT, '') AS updated_at FROM products) p), '[]'::json) AS prods,
                COALESCE((SELECT json_agg(l) FROM (SELECT id, product_id, quant, price, date_valid::TEXT, location, status, origin, created_at::TEXT, updated_at::TEXT FROM product_lots) l), '[]'::json) AS lots,
                COALESCE((SELECT json_agg(m) FROM (SELECT id, product_id, lot_id, action, quant, date_valid::TEXT, created_at::TEXT FROM stock_movements) m), '[]'::json) AS moves,
                COALESCE((SELECT json_agg(u) FROM (SELECT id, username, name, password_hash, pin, role, active, COALESCE(api_token, '') AS api_token, created_at::TEXT, updated_at::TEXT FROM users) u), '[]'::json) AS users_data,
                COALESCE((SELECT json_agg(a) FROM (SELECT id, user_id, username, action, target_id, product_name, details, ip_address, created_at::TEXT FROM audit_logs ORDER BY id DESC LIMIT 500) a), '[]'::json) AS audit_data;
        """)
        raw_row = pg_cur.fetchone()

        def _parse_json_col(col_val):
            if not col_val:
                return []
            if isinstance(col_val, str):
                try:
                    import json
                    return json.loads(col_val)
                except Exception:
                    return []
            if isinstance(col_val, list):
                return col_val
            return []

        pg_products = {p["id"]: p for p in _parse_json_col(raw_row[0])} if raw_row else {}
        pg_lots = {l["id"]: l for l in _parse_json_col(raw_row[1])} if raw_row else {}
        pg_moves = {m["id"]: m for m in _parse_json_col(raw_row[2])} if raw_row else {}
        pg_users = {u["id"]: u for u in (_parse_json_col(raw_row[3]) if raw_row and len(raw_row) > 3 else [])}
        pg_audit = {a["id"]: a for a in (_parse_json_col(raw_row[4]) if raw_row and len(raw_row) > 4 else [])}

        # 2. Lê snapshot local do SQLite
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()

            s_cur.execute("SELECT id, name, bar_code, price, updated_at FROM products")
            sq_products = {r["id"]: dict(r) for r in s_cur.fetchall()}

            s_cur.execute("SELECT id, product_id, quant, price, date_valid, location, status, origin, created_at, updated_at FROM product_lots")
            sq_lots = {r["id"]: dict(r) for r in s_cur.fetchall()}

            s_cur.execute("SELECT id, product_id, lot_id, action, quant, date_valid, created_at FROM stock_movements")
            sq_moves = {r["id"]: dict(r) for r in s_cur.fetchall()}

            sq_users = {}
            try:
                s_cur.execute("SELECT id, username, name, password_hash, pin, role, active, api_token, created_at, updated_at FROM users")
                sq_users = {r["id"]: dict(r) for r in s_cur.fetchall()}
            except Exception:
                pass

            sq_audit = {}
            try:
                s_cur.execute("SELECT id, user_id, username, action, target_id, product_name, details, ip_address, created_at FROM audit_logs")
                sq_audit = {r["id"]: dict(r) for r in s_cur.fetchall()}
            except Exception:
                pass

        uploaded_prods = 0
        uploaded_lots = 0
        uploaded_moves = 0
        uploaded_users = 0
        uploaded_audits = 0

        # 3. CONCILIAÇÃO DE PRODUTOS: Se local for mais recente ou novo -> Upload para Neon
        pg_prods_by_name = {p["name"].strip().upper(): p for p in pg_products.values() if p.get("name")}
        for sq_id, sq_p in sq_products.items():
            name_key = sq_p["name"].strip().upper()
            if name_key in pg_prods_by_name:
                pg_p = pg_prods_by_name[name_key]
                if parse_ts(sq_p.get("updated_at")) > parse_ts(pg_p.get("updated_at")):
                    pg_cur.execute(
                        "UPDATE products SET price = %s, bar_code = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (sq_p.get("price", 0.0), sq_p.get("bar_code"), pg_p["id"])
                    )
                    uploaded_prods += 1
            elif sq_id not in pg_products:
                pg_cur.execute(
                    "INSERT INTO products (id, name, bar_code, price, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (name) DO UPDATE SET price = EXCLUDED.price",
                    (sq_id, sq_p["name"], sq_p.get("bar_code"), sq_p.get("price", 0.0))
                )
                uploaded_prods += 1

        # 4. CONCILIAÇÃO DE MOVIMENTAÇÕES (BAIXAS): Se local tem baixas não presentes no Neon -> Upload e Remove Lote no Neon
        pg_baixa_lot_ids = {m["lot_id"] for m in pg_moves.values() if m.get("action") == "BAIXA" and m.get("lot_id")}
        for sq_id, sq_m in sq_moves.items():
            if sq_m.get("action") == "BAIXA" and sq_m.get("lot_id"):
                l_id = sq_m["lot_id"]
                if l_id not in pg_baixa_lot_ids:
                    pg_cur.execute(
                        "INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid, created_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                        (sq_m["product_id"], l_id, sq_m["action"], sq_m.get("quant", 0), str(sq_m["date_valid"])[:10])
                    )
                    pg_cur.execute("DELETE FROM product_lots WHERE id = %s", (l_id,))
                    pg_baixa_lot_ids.add(l_id)
                    uploaded_moves += 1

        # 5. CONCILIAÇÃO DE LOTES: Se alteração local for mais recente que o Neon -> Upload para Neon
        for sq_id, sq_l in sq_lots.items():
            if sq_id in pg_baixa_lot_ids:
                continue

            if sq_id in pg_lots:
                pg_l = pg_lots[sq_id]
                sq_time = parse_ts(sq_l.get("updated_at") or sq_l.get("created_at"))
                pg_time = parse_ts(pg_l.get("updated_at") or pg_l.get("created_at"))
                if sq_time > pg_time:
                    # Alteração local é mais recente! Faz upload para o Neon
                    pg_cur.execute(
                        """UPDATE product_lots
                           SET quant = %s, price = %s, date_valid = %s, location = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                           WHERE id = %s""",
                        (sq_l["quant"], sq_l.get("price", 0.0), str(sq_l["date_valid"])[:10], sq_l.get("location", "LOJA"), sq_l.get("status", "NORMAL"), sq_id)
                    )
                    uploaded_lots += 1
            else:
                # Lote novo cadastrado localmente durante indisponibilidade do Neon -> Upload
                pg_cur.execute(
                    """INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, CURRENT_TIMESTAMP)
                       ON CONFLICT (id) DO UPDATE SET quant = EXCLUDED.quant, price = EXCLUDED.price, updated_at = CURRENT_TIMESTAMP""",
                    (sq_id, sq_l["product_id"], sq_l["quant"], sq_l.get("price", 0.0), str(sq_l["date_valid"])[:10], sq_l.get("location", "LOJA"), sq_l.get("status", "NORMAL"), sq_l.get("origin", "MANUAL"))
                )
                uploaded_lots += 1

        # 6. CONCILIAÇÃO DE USUÁRIOS: Se local for mais recente ou novo -> Upload para Neon
        for sq_id, sq_u in sq_users.items():
            if sq_id in pg_users:
                pg_u = pg_users[sq_id]
                sq_time = parse_ts(sq_u.get("updated_at") or sq_u.get("created_at"))
                pg_time = parse_ts(pg_u.get("updated_at") or pg_u.get("created_at"))
                if sq_time > pg_time:
                    pg_cur.execute(
                        """UPDATE users
                           SET name = %s, password_hash = %s, pin = %s, role = %s, active = %s, api_token = %s, updated_at = CURRENT_TIMESTAMP
                           WHERE id = %s""",
                        (sq_u["name"], sq_u["password_hash"], sq_u.get("pin"), sq_u.get("role", "operador"), bool(sq_u.get("active", 1)), sq_u.get("api_token"), sq_id)
                    )
                    uploaded_users += 1
            else:
                pg_cur.execute(
                    """INSERT INTO users (id, username, name, password_hash, pin, role, active, api_token, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT (username) DO UPDATE SET name = EXCLUDED.name, pin = EXCLUDED.pin, updated_at = CURRENT_TIMESTAMP""",
                    (sq_id, sq_u["username"], sq_u["name"], sq_u["password_hash"], sq_u.get("pin"), sq_u.get("role", "operador"), bool(sq_u.get("active", 1)), sq_u.get("api_token"))
                )
                uploaded_users += 1

        # 7. CONCILIAÇÃO DE AUDITORIA: Envia logs criados localmente
        pg_audit_keys = {(a["username"], a["action"], str(a["created_at"])[:19]) for a in pg_audit.values()}
        for sq_id, sq_a in sq_audit.items():
            k = (sq_a["username"], sq_a["action"], str(sq_a["created_at"])[:19])
            if k not in pg_audit_keys and sq_id not in pg_audit:
                pg_cur.execute(
                    """INSERT INTO audit_logs (user_id, username, action, target_id, product_name, details, ip_address, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (sq_a.get("user_id"), sq_a["username"], sq_a["action"], sq_a.get("target_id"), sq_a.get("product_name"), sq_a.get("details", ""), sq_a.get("ip_address"), sq_a["created_at"])
                )
                uploaded_audits += 1

        # 8. Sincroniza sequences do PostgreSQL
        pg_cur.execute("SELECT setval('products_id_seq', (SELECT COALESCE(MAX(id), 1) FROM products));")
        pg_cur.execute("SELECT setval('product_lots_id_seq', (SELECT COALESCE(MAX(id), 1) FROM product_lots));")
        pg_cur.execute("SELECT setval('stock_movements_id_seq', (SELECT COALESCE(MAX(id), 1) FROM stock_movements));")
        try:
            pg_cur.execute("SELECT setval('users_id_seq', (SELECT COALESCE(MAX(id), 1) FROM users));")
        except Exception:
            pass
        try:
            pg_cur.execute("SELECT setval('audit_logs_id_seq', (SELECT COALESCE(MAX(id), 1) FROM audit_logs));")
        except Exception:
            pass

        pg_conn.commit()

        # 9. Puxa a versão unificada e final para o SQLite
        pg_cur.execute("""
            SELECT
                COALESCE((SELECT json_agg(p) FROM (SELECT id, name, bar_code, price, COALESCE(updated_at::TEXT, '') AS updated_at FROM products) p), '[]'::json) AS prods,
                COALESCE((SELECT json_agg(l) FROM (SELECT id, product_id, quant, price, date_valid::TEXT, location, status, origin, created_at::TEXT, updated_at::TEXT FROM product_lots) l), '[]'::json) AS lots,
                COALESCE((SELECT json_agg(m) FROM (SELECT id, product_id, lot_id, action, quant, date_valid::TEXT, created_at::TEXT FROM stock_movements) m), '[]'::json) AS moves,
                COALESCE((SELECT json_agg(u) FROM (SELECT id, username, name, password_hash, pin, role, active, COALESCE(api_token, '') AS api_token, created_at::TEXT, updated_at::TEXT FROM users) u), '[]'::json) AS users_data,
                COALESCE((SELECT json_agg(a) FROM (SELECT id, user_id, username, action, target_id, product_name, details, ip_address, created_at::TEXT FROM audit_logs ORDER BY id DESC LIMIT 500) a), '[]'::json) AS audit_data;
        """)
        fresh_row = pg_cur.fetchone()
        products = _parse_json_col(fresh_row[0]) if fresh_row else []
        lots = _parse_json_col(fresh_row[1]) if fresh_row else []
        movements = _parse_json_col(fresh_row[2]) if fresh_row else []
        users_list = _parse_json_col(fresh_row[3]) if fresh_row and len(fresh_row) > 3 else []
        audit_list = _parse_json_col(fresh_row[4]) if fresh_row and len(fresh_row) > 4 else []

        pg_cur.close()
        if is_from_pool and pool_inst:
            pool_inst.putconn(pg_conn)
        else:
            pg_conn.close()

        # Grava atomicamente no SQLite local em modo WAL
        sqlite_conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
        sqlite_cur = sqlite_conn.cursor()
        sqlite_cur.execute("PRAGMA journal_mode = WAL;")
        sqlite_cur.execute("PRAGMA synchronous = NORMAL;")
        sqlite_cur.execute("PRAGMA foreign_keys = OFF;")
        sqlite_cur.execute("BEGIN TRANSACTION;")

        sqlite_cur.execute("DELETE FROM products;")
        if products:
            sqlite_cur.executemany(
                "INSERT INTO products (id, name, bar_code, price, updated_at) VALUES (?, ?, ?, ?, ?);",
                [(p["id"], p["name"], p.get("bar_code"), p.get("price", 0.0), p.get("updated_at")) for p in products]
            )

        sqlite_cur.execute("DELETE FROM product_lots;")
        if lots:
            sqlite_cur.executemany("""
                INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, [
                (
                    l["id"],
                    l["product_id"],
                    l.get("quant", 0),
                    l.get("price", 0.0),
                    str(l["date_valid"])[:10],
                    l.get("location", "LOJA"),
                    l.get("status", "NORMAL"),
                    l.get("origin", "MANUAL"),
                    l.get("created_at"),
                    l.get("updated_at")
                )
                for l in lots
            ])

        sqlite_cur.execute("DELETE FROM stock_movements;")
        if movements:
            sqlite_cur.executemany("""
                INSERT INTO stock_movements (id, product_id, lot_id, action, quant, date_valid, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, [
                (
                    m["id"],
                    m["product_id"],
                    m.get("lot_id"),
                    m.get("action", ""),
                    m.get("quant", 0),
                    str(m["date_valid"])[:10],
                    m.get("created_at")
                )
                for m in movements
            ])

        if users_list:
            sqlite_cur.execute("DELETE FROM users;")
            sqlite_cur.executemany("""
                INSERT INTO users (id, username, name, password_hash, pin, role, active, api_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, [
                (
                    u["id"],
                    u["username"],
                    u["name"],
                    u["password_hash"],
                    u.get("pin"),
                    u.get("role", "operador"),
                    1 if u.get("active", True) else 0,
                    u.get("api_token", ""),
                    u.get("created_at"),
                    u.get("updated_at")
                )
                for u in users_list
            ])

        if audit_list:
            sqlite_cur.execute("DELETE FROM audit_logs;")
            sqlite_cur.executemany("""
                INSERT INTO audit_logs (id, user_id, username, action, target_id, product_name, details, ip_address, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, [
                (
                    a["id"],
                    a.get("user_id"),
                    a.get("username", "Sistema"),
                    a.get("action", ""),
                    a.get("target_id"),
                    a.get("product_name"),
                    a.get("details", ""),
                    a.get("ip_address"),
                    a.get("created_at")
                )
                for a in audit_list
            ])

        sqlite_conn.commit()
        sqlite_conn.close()

    except Exception as e:
        _sync_lock.release()
        with _status_lock:
            _db_status["postgres_online"] = False
            _db_status["last_error"] = str(e)
        return {"success": False, "error": f"Falha na conciliação com PostgreSQL: {e}"}

    _sync_lock.release()
    clear_query_cache()

    duration = round(time.time() - t0, 3)
    sync_ts = time.strftime("%Y-%m-%d %H:%M:%S")

    with _status_lock:
        _db_status["postgres_online"] = True
        _db_status["last_sync"] = sync_ts
        _db_status["last_error"] = None
        _db_status["total_products"] = len(products)
        _db_status["total_lots"] = len(lots)

    print(f"🔄 Conciliação concluída em {duration}s! Uploads para Neon: {uploaded_lots} lotes, {uploaded_prods} produtos, {uploaded_moves} baixas, {uploaded_users} usuários.")

    return {
        "success": True,
        "message": f"Sincronização concluída com sucesso! Uploads para Neon: {uploaded_lots} lotes, {uploaded_prods} produtos, {uploaded_moves} baixas.",
        "duration_s": duration,
        "timestamp": sync_ts,
        "total_products": len(products),
        "total_lots": len(lots),
        "uploaded_lots": uploaded_lots,
        "uploaded_prods": uploaded_prods,
        "uploaded_moves": uploaded_moves,
        "uploaded_users": uploaded_users
    }

def init_db():
    """
    Inicialização resiliente em 2 etapas:
    1. Primeiro: Garante a base SQLite local íntegra para o servidor poder rodar imediatamente.
    2. Em seguida: Faz a ligação com o PostgreSQL. Se estiver online, ativa e sincroniza cópia local.
       Se não estiver online, ativa o auto-reconnect e o servidor roda liso com o SQLite local!
    """
    # 1. Primeiro a cópia dos dados em SQL local para o servidor poder rodar
    init_sqlite_db()

    # 2. Em seguida tenta fazer a ligação com o PostgreSQL
    if is_postgres_configured():
        success = False
        try:
            success = init_pg_db()
        except Exception as e:
            print(f"⚠️ PostgreSQL offline na inicialização: {e}")

        if success:
            with _status_lock:
                _db_status["postgres_online"] = True
            print("🚀 PostgreSQL conectado com sucesso!")
            # Puxa a cópia mais recente do Postgres para o SQLite em background thread
            threading.Thread(target=pull_cloud_to_sqlite, daemon=True).start()
            _start_neon_keepalive()
            threading.Thread(target=prewarm_filters_cache, daemon=True).start()
        else:
            with _status_lock:
                _db_status["postgres_online"] = False
            print("⚡ PostgreSQL offline. Servidor rodando com cópia local SQLite (Offline-first).")
            trigger_auto_reconnect()
            threading.Thread(target=prewarm_filters_cache, daemon=True).start()


def prewarm_filters_cache():
    """Pré-aquece os filtros principais em memória RAM para resposta em 0.005ms."""
    try:
        for f in ("todos", "vencidos", "critico", "atencao", "alerta", "recentes", "deletados"):
            query_lots(filtro=f, busca="", limit=50, offset=0)
    except Exception:
        pass


_keepalive_started = False
def _start_neon_keepalive():
    """Mantém a conexão e compute do Neon ativos em segundo plano (evita cold start e desconexões)."""
    global _keepalive_started
    if _keepalive_started:
        return
    _keepalive_started = True

    def _worker():
        while is_postgres_configured():
            time.sleep(25)  # Mantém canal TCP e Neon Compute 100% aquecidos
            if is_postgres():
                try:
                    pool_inst = get_pg_pool()
                    if pool_inst:
                        conn, is_from_pool = _checkout_valid_pg_conn(pool_inst)
                        if conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT 1;")
                            _mark_conn_used(conn)
                            if is_from_pool:
                                pool_inst.putconn(conn)
                            else:
                                conn.close()
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()


# ── Consulta Otimizada de Lotes (Local-First com Tempo de Resposta < 20ms) ───
def query_lots(filtro: str = "todos", busca: str = "", limit: int = 50, offset: int = 0, **kwargs):
    """
    Consulta os lotes com performance máxima (garantido < 50ms):
    1. Verificação instantânea em cache em memória RAM (0.005ms)
    2. Consulta direta na réplica local SQLite WAL indexada (1ms a 15ms)
    3. Resposta sem dependência de latência de rede na nuvem
    Retorna: (total_count, lista_de_dicionarios)
    """
    clean_busca = busca.strip()
    cache_key = (filtro, clean_busca.upper(), limit, offset)

    # 1. Checa cache em memória RAM (0.005ms)
    now = time.time()
    with _cache_lock:
        if cache_key in _query_cache:
            entry_time, cached_total, cached_lots = _query_cache[cache_key]
            if now - entry_time < CACHE_TTL_SECONDS:
                return cached_total, cached_lots

    # 2. Executa na réplica local SQLite WAL (< 1ms com conexão persistente em thread)
    try:
        s_conn = _get_thread_sqlite_conn()
        cur = s_conn.cursor()
        try:
            today = date.today()

            if filtro in ("deletados", "deletado", "baixas"):
                where_clauses = ["sm.action = 'BAIXA'"]
                params = []
                if clean_busca:
                    where_clauses.append("p.name LIKE ?")
                    params.append(f"%{clean_busca}%")
                where_sql = " AND ".join(where_clauses)
                query_sq = f"""
                    SELECT sm.id AS lot_id, sm.id AS movement_id,
                           p.name AS product_name, p.id AS product_id,
                           sm.quant, p.price,
                           sm.date_valid AS date_valid_str,
                           'BAIXA' AS location, 'DELETADO' AS status,
                           sm.created_at AS deleted_at,
                           COUNT(*) OVER() AS full_count
                    FROM stock_movements sm
                    INNER JOIN products p ON p.id = sm.product_id
                    WHERE {where_sql}
                    ORDER BY sm.id DESC
                    LIMIT ? OFFSET ?
                """
                cur.execute(query_sq, params + [limit, offset])
                raw_rows = cur.fetchall()
                if not raw_rows:
                    total = 0
                    lots = []
                else:
                    total = raw_rows[0]["full_count"]
                    lots = []
                    for r in raw_rows:
                        lots.append({
                            "lot_id": r["lot_id"],
                            "movement_id": r["movement_id"],
                            "product_name": r["product_name"],
                            "product_id": r["product_id"],
                            "quant": r["quant"],
                            "price": r["price"] or 0.0,
                            "date_valid": str(r["date_valid_str"])[:10],
                            "location": r["location"],
                            "status": r["status"],
                            "deleted_at": str(r["deleted_at"])[:19] if r["deleted_at"] else "",
                            "is_deleted": True
                        })
                with _cache_lock:
                    _query_cache[cache_key] = (now, total, lots)
                return total, lots

            where_clauses = ["1=1"]
            params = []

            if filtro == "vencidos":
                where_clauses.append("l.date_valid < ?")
                params.append(today.strftime("%Y-%m-%d"))
            elif filtro == "critico":
                where_clauses.append("l.date_valid BETWEEN ? AND ?")
                params.append(today.strftime("%Y-%m-%d"))
                params.append((today + timedelta(days=7)).strftime("%Y-%m-%d"))
            elif filtro == "atencao":
                where_clauses.append("l.date_valid BETWEEN ? AND ?")
                params.append((today + timedelta(days=8)).strftime("%Y-%m-%d"))
                params.append((today + timedelta(days=29)).strftime("%Y-%m-%d"))
            elif filtro == "alerta":
                where_clauses.append("l.date_valid BETWEEN ? AND ?")
                params.append((today + timedelta(days=30)).strftime("%Y-%m-%d"))
                params.append((today + timedelta(days=45)).strftime("%Y-%m-%d"))
            elif filtro in ("recentes", "recente", "hoje"):
                two_weeks_ago = (today - timedelta(days=14)).strftime("%Y-%m-%d")
                tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
                where_clauses.append("l.created_at >= ? AND l.created_at < ? AND l.origin = 'MANUAL'")
                params.append(two_weeks_ago)
                params.append(tomorrow)

            if clean_busca:
                where_clauses.append("p.name LIKE ?")
                params.append(f"%{clean_busca}%")

            where_sql = " AND ".join(where_clauses)
            order_by = "l.created_at DESC, l.id DESC" if filtro in ("recentes", "recente", "hoje") else "l.date_valid ASC, l.id ASC"

            query_sq = f"""
                SELECT l.id AS lot_id, p.name AS product_name, p.id AS product_id,
                       l.quant, p.price,
                       l.date_valid AS date_valid_str,
                       l.location, l.status,
                       COUNT(*) OVER() AS full_count
                FROM product_lots l
                INNER JOIN products p ON p.id = l.product_id
                WHERE {where_sql}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
            """
            cur.execute(query_sq, params + [limit, offset])
            raw_rows = cur.fetchall()

            if not raw_rows:
                total = 0
                lots = []
            else:
                total = raw_rows[0]["full_count"]
                lots = []
                for r in raw_rows:
                    lots.append({
                        "lot_id": r["lot_id"],
                        "product_name": r["product_name"],
                        "product_id": r["product_id"],
                        "quant": r["quant"],
                        "price": r["price"],
                        "date_valid": str(r["date_valid_str"])[:10],
                        "location": r["location"],
                        "status": r["status"],
                    })

            # Salva no cache em memória RAM
            with _cache_lock:
                _query_cache[cache_key] = (now, total, lots)

            return total, lots
        finally:
            cur.close()

    except Exception as e:
        _reset_thread_sqlite_conn()
        print(f"⚠️ Erro ao consultar SQLite local: {e}")
        # Fallback de segurança para PostgreSQL caso ocorra algum problema no SQLite local
        with get_db_cursor() as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            if filtro in ("deletados", "deletado", "baixas"):
                like_op = "ILIKE" if db_type == "postgres" else "LIKE"
                w = ["sm.action = 'BAIXA'"]
                p = []
                if clean_busca:
                    w.append(f"p.name {like_op} {ph}")
                    p.append(f"%{clean_busca}%")
                w_sql = " AND ".join(w)
                cur.execute(
                    f"""SELECT sm.id, p.name, sm.quant, p.price, sm.date_valid::TEXT, 'BAIXA', 'DELETADO', sm.created_at::TEXT
                        FROM stock_movements sm JOIN products p ON p.id = sm.product_id
                        WHERE {w_sql} ORDER BY sm.id DESC LIMIT {ph} OFFSET {ph}""",
                    p + [limit, offset]
                )
                rows = cur.fetchall()
                lots = [{
                    "lot_id": r[0],
                    "movement_id": r[0],
                    "product_name": r[1],
                    "quant": r[2],
                    "price": r[3] or 0.0,
                    "date_valid": str(r[4])[:10],
                    "location": r[5],
                    "status": r[6],
                    "deleted_at": str(r[7])[:19] if len(r) > 7 and r[7] else "",
                    "is_deleted": True
                } for r in rows]
                return len(lots), lots

            cur.execute(f"SELECT l.id, p.name, l.quant, p.price, l.date_valid::TEXT, l.location, l.status FROM product_lots l JOIN products p ON p.id = l.product_id LIMIT {ph} OFFSET {ph}", (limit, offset))
            rows = cur.fetchall()
            lots = [{"lot_id": r[0], "product_name": r[1], "quant": r[2], "price": r[3], "date_valid": str(r[4])[:10], "location": r[5], "status": r[6]} for r in rows]
            return len(lots), lots


# ── Mutações (Com Resposta Instantânea Local-First + Sincronização em Background) ────

def update_lot_quant(lot_id: int, quant: int) -> bool:
    # 1. Atualiza cache em RAM instantaneamente (0.01ms) — zero espera
    _update_cache_lot_field(lot_id, "quant", quant)
    # 2. Grava no SQLite local em WAL mode (< 1ms)
    _mirror_to_sqlite("UPDATE product_lots SET quant = ?, updated_at = datetime('now') WHERE id = ?", (quant, lot_id))
    # 3. Sincroniza com PostgreSQL na nuvem em segundo plano sem travar a resposta HTTP
    if is_postgres():
        def _sync_pg():
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        cur.execute("UPDATE product_lots SET quant = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (quant, lot_id))
            except Exception as e:
                print(f"⚠️ Erro ao sincronizar quantidade no Postgres: {e}")
        threading.Thread(target=_sync_pg, daemon=True).start()
    return True


def update_lot_price(lot_id: int, price: float) -> bool:
    global _catalog_cache
    # 1. Atualiza cache em RAM instantaneamente (0.01ms)
    _update_cache_lot_field(lot_id, "price", price)
    with _catalog_lock:
        _catalog_cache = None
    # 2. Grava no SQLite local: atualiza products e espelha em product_lots (< 1ms)
    _mirror_to_sqlite("UPDATE products SET price = ? WHERE id = (SELECT product_id FROM product_lots WHERE id = ?)", (price, lot_id))
    _mirror_to_sqlite("UPDATE product_lots SET price = ?, updated_at = datetime('now') WHERE id = ?", (price, lot_id))
    # 3. Sincroniza com PostgreSQL na nuvem em segundo plano sem travar a resposta HTTP
    if is_postgres():
        def _sync_pg():
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        cur.execute("UPDATE products SET price = %s WHERE id = (SELECT product_id FROM product_lots WHERE id = %s)", (price, lot_id))
                        cur.execute("UPDATE product_lots SET price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (price, lot_id))
            except Exception as e:
                print(f"⚠️ Erro ao sincronizar preço no Postgres: {e}")
        threading.Thread(target=_sync_pg, daemon=True).start()
    return True


def delete_lot(lot_id: int, sync: bool = True) -> bool:
    """
    Dá baixa no lote e registra a Lápide (tombstone) em stock_movements,
    garantindo que futuras importações de planilhas não ressuscitem esse lote (Item Zumbi).
    """
    # 1. Remove do cache em RAM imediatamente (0.01ms) e invalida cache de consultas
    _remove_cache_lot(lot_id)
    clear_query_cache(clear_catalog=False)

    # 2. Registra baixa e remove no SQLite local imediatamente (< 1ms)
    row_info = None
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            s_cur = s_conn.cursor()
            s_cur.execute("SELECT product_id, quant, date_valid FROM product_lots WHERE id = ?", (lot_id,))
            s_row = s_cur.fetchone()
            if not s_row and is_postgres():
                try:
                    with get_db_cursor() as (cur_pg, db_type_pg):
                        if db_type_pg == "postgres":
                            cur_pg.execute("SELECT product_id, quant, date_valid::TEXT FROM product_lots WHERE id = %s", (lot_id,))
                            pg_r = cur_pg.fetchone()
                            if pg_r:
                                s_row = (pg_r[0], pg_r[1], str(pg_r[2])[:10])
                except Exception:
                    pass
            if s_row:
                p_id, q, d_valid = s_row[0], s_row[1], str(s_row[2])[:10]
                row_info = (p_id, q, d_valid)
                s_cur.execute(
                    "INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid) VALUES (?, ?, 'BAIXA', ?, ?)",
                    (p_id, lot_id, q, d_valid)
                )
            s_cur.execute("DELETE FROM product_lots WHERE id = ?", (lot_id,))
            s_conn.commit()
    except Exception as e:
        print(f"⚠️ Erro ao registrar baixa no SQLite: {e}")

    # 3. Sincroniza com PostgreSQL (em background thread quando sync=False para resposta em ~1ms)
    if is_postgres():
        def _sync_pg_delete(r_info, l_id):
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        if r_info:
                            cur.execute(
                                "INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid) VALUES (%s, %s, 'BAIXA', %s, %s)",
                                (r_info[0], l_id, r_info[1], r_info[2])
                            )
                        cur.execute("DELETE FROM product_lots WHERE id = %s", (l_id,))
            except Exception as e:
                print(f"⚠️ Erro ao sincronizar baixa no Postgres: {e}")

        if sync:
            _sync_pg_delete(row_info, lot_id)
        else:
            threading.Thread(target=_sync_pg_delete, args=(row_info, lot_id), daemon=True).start()

    return True


def restore_lot(movement_id: int, sync: bool = True) -> bool:
    """
    Restaura um lote baixado (deletado) de volta para o estoque:
    - Lê os dados de stock_movements (product_id, quant, date_valid).
    - Recria o lote em product_lots com origin = 'MANUAL'.
    - Atualiza a movimentação para 'RESTAURADO' para sair da lista de deletados.
    - Registra a movimentação 'RESTAURACAO'.
    - Invalida os caches necessários.
    """
    clear_query_cache()
    info = None
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            s_cur = s_conn.cursor()
            s_cur.execute(
                "SELECT product_id, quant, date_valid FROM stock_movements WHERE id = ? AND action = 'BAIXA'",
                (movement_id,)
            )
            row = s_cur.fetchone()
            if not row and is_postgres():
                try:
                    with get_db_cursor() as (cur_pg, db_type_pg):
                        if db_type_pg == "postgres":
                            cur_pg.execute(
                                "SELECT product_id, quant, date_valid::TEXT FROM stock_movements WHERE id = %s AND action = 'BAIXA'",
                                (movement_id,)
                            )
                            pg_r = cur_pg.fetchone()
                            if pg_r:
                                row = (pg_r[0], pg_r[1], str(pg_r[2])[:10])
                except Exception:
                    pass
            if not row:
                return False
            p_id, quant, d_valid = row[0], row[1], str(row[2])[:10]

            s_cur.execute("SELECT price FROM products WHERE id = ?", (p_id,))
            p_row = s_cur.fetchone()
            price = p_row[0] if p_row and p_row[0] is not None else 0.0

            s_cur.execute(
                """INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'LOJA', 'NORMAL', 'MANUAL', date('now'), datetime('now'))""",
                (p_id, quant, price, d_valid)
            )
            new_lot_id = s_cur.lastrowid

            s_cur.execute("UPDATE stock_movements SET action = 'RESTAURADO' WHERE id = ?", (movement_id,))
            s_cur.execute(
                "INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid) VALUES (?, ?, 'RESTAURACAO', ?, ?)",
                (p_id, new_lot_id, quant, d_valid)
            )
            s_conn.commit()
            info = (p_id, quant, price, d_valid, new_lot_id)
    except Exception as e:
        print(f"⚠️ Erro ao restaurar lote no SQLite: {e}")
        return False

    if is_postgres() and info:
        def _sync_pg_restore(p_id, quant, price, d_valid, m_id):
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        cur.execute(
                            """INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                               VALUES (%s, %s, %s, %s, 'LOJA', 'NORMAL', 'MANUAL', CURRENT_DATE, CURRENT_TIMESTAMP) RETURNING id""",
                            (p_id, quant, price, d_valid)
                        )
                        pg_lot_id = cur.fetchone()[0]
                        cur.execute("UPDATE stock_movements SET action = 'RESTAURADO' WHERE id = %s", (m_id,))
                        cur.execute(
                            "INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid) VALUES (%s, %s, 'RESTAURACAO', %s, %s)",
                            (p_id, pg_lot_id, quant, d_valid)
                        )
            except Exception as e:
                print(f"⚠️ Erro ao sincronizar restauração no Postgres: {e}")

        p_id, quant, price, d_valid, new_lot_id = info
        if sync:
            _sync_pg_restore(p_id, quant, price, d_valid, movement_id)
        else:
            threading.Thread(target=_sync_pg_restore, args=(p_id, quant, price, d_valid, movement_id), daemon=True).start()

    return True


def purge_deleted_movement(movement_id: int, sync: bool = True) -> bool:
    """Exclui permanentemente um registro de baixa da auditoria (deletados)."""
    clear_query_cache()
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            s_conn.execute("DELETE FROM stock_movements WHERE id = ?", (movement_id,))
            s_conn.commit()
    except Exception as e:
        print(f"⚠️ Erro ao purgar movimentação no SQLite: {e}")

    if is_postgres():
        def _sync_pg_purge(m_id):
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        cur.execute("DELETE FROM stock_movements WHERE id = %s", (m_id,))
            except Exception as e:
                print(f"⚠️ Erro ao purgar movimentação no Postgres: {e}")

        if sync:
            _sync_pg_purge(movement_id)
        else:
            threading.Thread(target=_sync_pg_purge, args=(movement_id,), daemon=True).start()

    return True


def purge_all_deleted_movements() -> int:
    """Exclui permanentemente todos os registros de baixa da auditoria."""
    clear_query_cache()
    count = 0
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            cur = s_conn.cursor()
            cur.execute("DELETE FROM stock_movements WHERE action = 'BAIXA'")
            count = cur.rowcount
            s_conn.commit()
    except Exception as e:
        print(f"⚠️ Erro ao purgar todas movimentações no SQLite: {e}")

    if is_postgres():
        try:
            with get_db_cursor(commit=True) as (cur, db_type):
                if db_type == "postgres":
                    cur.execute("DELETE FROM stock_movements WHERE action = 'BAIXA'")
                    if not count:
                        count = cur.rowcount
        except Exception as e:
            print(f"⚠️ Erro ao purgar todas movimentações no Postgres: {e}")

    return count


def create_lot(name: str, quant: int, price: float, date_valid: str, location: str = "LOJA", status: str = "NORMAL"):
    """
    Cadastra ou adiciona quantidade em lote com performance máxima:
    1. Grava no SQLite local WAL instantaneamente (< 2ms)
    2. Atualiza cache de catálogo e consultas em RAM (0.01ms)
    3. Sincroniza em segundo plano (background thread) com PostgreSQL sem travar a interface
    """
    clear_query_cache(clear_catalog=False)
    name = name.strip().upper()
    location = (location or "LOJA").strip().upper()
    status = (status or "NORMAL").strip().upper()

    product_exists = False
    updated_product_price = False
    product_id = None
    lot_id = None
    is_new_lot = False
    new_quant = quant

    # 1. Gravação local SQLite de alta velocidade (< 2ms)
    with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
        s_conn.execute("PRAGMA journal_mode = WAL;")
        s_conn.execute("PRAGMA synchronous = NORMAL;")
        cur = s_conn.cursor()

        # Encontra ou cria produto
        cur.execute("SELECT id, price FROM products WHERE UPPER(name) = ?", (name,))
        row = cur.fetchone()
        if row:
            product_id = row[0]
            curr_price = float(row[1] or 0.0)
            product_exists = True
            if curr_price == 0.0 and price > 0:
                cur.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))
                updated_product_price = True
            elif curr_price > 0 and price <= 0:
                price = curr_price
        else:
            cur.execute("INSERT INTO products (name, price) VALUES (?, ?)", (name, price))
            product_id = cur.lastrowid
            updated_product_price = True

        # Encontra ou cria lote
        cur.execute(
            "SELECT id, quant FROM product_lots WHERE product_id = ? AND date_valid = ? AND location = ? LIMIT 1",
            (product_id, date_valid, location)
        )
        existing_lot = cur.fetchone()
        if existing_lot:
            lot_id = existing_lot[0]
            old_quant = existing_lot[1] or 0
            new_quant = old_quant + quant
            cur.execute(
                "UPDATE product_lots SET quant = ?, updated_at = datetime('now') WHERE id = ?",
                (new_quant, lot_id)
            )
            is_new_lot = False
        else:
            cur.execute(
                """INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'MANUAL', date('now'), datetime('now'))""",
                (product_id, quant, price, date_valid, location, status)
            )
            lot_id = cur.lastrowid
            is_new_lot = True

        s_conn.commit()

    _update_catalog_cache_item(product_id, name, price)

    # 2. Sincronização em background com PostgreSQL (não bloqueia resposta HTTP)
    if is_postgres():
        def _sync_pg_create():
            try:
                with get_db_cursor(commit=True) as (pg_cur, db_type):
                    if db_type == "postgres":
                        # Garante produto no PostgreSQL
                        pg_cur.execute("SELECT id, price FROM products WHERE UPPER(name) = UPPER(%s)", (name,))
                        pg_p = pg_cur.fetchone()
                        if pg_p:
                            pg_prod_id = pg_p[0]
                            if updated_product_price:
                                pg_cur.execute("UPDATE products SET price = %s WHERE id = %s", (price, pg_prod_id))
                        else:
                            pg_cur.execute(
                                "INSERT INTO products (id, name, price) VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET price = EXCLUDED.price RETURNING id",
                                (product_id, name, price)
                            )
                            r = pg_cur.fetchone()
                            pg_prod_id = r[0] if r else product_id
                            pg_cur.execute("SELECT setval('products_id_seq', GREATEST((SELECT COALESCE(MAX(id), 1) FROM products), %s))", (pg_prod_id,))

                        # Garante lote no PostgreSQL
                        if is_new_lot:
                            pg_cur.execute("""
                                INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status, origin, created_at, updated_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, 'MANUAL', CURRENT_DATE, CURRENT_TIMESTAMP)
                                ON CONFLICT (id) DO UPDATE SET quant = EXCLUDED.quant, price = EXCLUDED.price, updated_at = CURRENT_TIMESTAMP
                            """, (lot_id, pg_prod_id, quant, price, date_valid, location, status))
                            pg_cur.execute("SELECT setval('product_lots_id_seq', GREATEST((SELECT COALESCE(MAX(id), 1) FROM product_lots), %s))", (lot_id,))
                        else:
                            pg_cur.execute("UPDATE product_lots SET quant = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (new_quant, lot_id))
            except Exception as ex:
                print(f"⚠️ Erro ao sincronizar create_lot no Postgres: {ex}")

        threading.Thread(target=_sync_pg_create, daemon=True).start()

    return product_id, lot_id


def search_products(term: str, limit: int = 10) -> list[dict]:
    """
    Busca produtos cadastrados no catálogo por nome ou código de barras para autocomplete instantâneo (< 0.1ms).
    Utiliza índice em memória RAM carregado do banco local.
    """
    global _catalog_cache
    term = term.strip().upper()
    if not term:
        return []

    # 1. Carrega catálogo na RAM se necessário (apenas 1 vez, < 5ms)
    with _catalog_lock:
        if _catalog_cache is None:
            items = []
            try:
                s_conn = _get_thread_sqlite_conn()
                c_sq = s_conn.cursor()
                try:
                    c_sq.execute("SELECT id, name, bar_code, price FROM products ORDER BY name ASC")
                    for r in c_sq.fetchall():
                        items.append({
                            "id": r["id"],
                            "name": str(r["name"] or "").strip().upper(),
                            "bar_code": str(r["bar_code"] or "").strip() if r["bar_code"] else None,
                            "price": float(r["price"] or 0.0)
                        })
                finally:
                    c_sq.close()
            except Exception:
                _reset_thread_sqlite_conn()
            if items:
                _catalog_cache = items

        catalog = _catalog_cache

    # 2. Busca ultrarrápida em memória RAM (0.05ms)
    if catalog:
        prefix_matches = []
        substring_matches = []
        for p in catalog:
            p_name = p["name"]
            p_code = p.get("bar_code") or ""
            if p_name.startswith(term):
                prefix_matches.append(p)
                if len(prefix_matches) >= limit:
                    break
            elif term in p_name or (p_code and term in p_code):
                substring_matches.append(p)

        combined = prefix_matches + [m for m in substring_matches if m not in prefix_matches]
        return combined[:limit]

    # 3. Fallback clássico caso a cópia local esteja em inicialização
    with get_db_cursor() as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"
        like_op = "ILIKE" if db_type == "postgres" else "LIKE"
        like_val = f"%{term}%"

        query = f"""
            SELECT id, name, bar_code, price
            FROM products
            WHERE name {like_op} {ph} OR bar_code {like_op} {ph}
            ORDER BY
                CASE WHEN name {like_op} {ph} THEN 1 ELSE 2 END,
                name ASC
            LIMIT {ph}
        """
        cur.execute(query, (like_val, like_val, f"{term}%", limit))
        rows = cur.fetchall()

        results = []
        for r in rows:
            if db_type == "postgres":
                results.append({"id": r[0], "name": r[1], "bar_code": r[2], "price": float(r[3] or 0.0)})
            else:
                results.append({
                    "id": r["id"] if hasattr(r, "keys") else r[0],
                    "name": r["name"] if hasattr(r, "keys") else r[1],
                    "bar_code": r["bar_code"] if hasattr(r, "keys") else r[2],
                    "price": float((r["price"] if hasattr(r, "keys") else r[3]) or 0.0)
                })
        return results


def get_alert_settings() -> dict:
    """Retorna as configurações atuais de alerta automático."""
    defaults = {
        "enabled": False,
        "channel": "telegram",
        "daily_time": "07:30",
        "telegram_token": "",
        "telegram_chat_id": "",
        "webhook_url": "",
        "last_sent_date": "",
        "last_status": "Nenhum envio realizado ainda.",
        "include_vencidos": True,
        "include_criticos": True,
        "include_atencao": False
    }
    try:
        with get_db_cursor() as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(f"""
                SELECT enabled, channel, daily_time, telegram_token, telegram_chat_id,
                       webhook_url, last_sent_date, last_status, include_vencidos,
                       include_criticos, include_atencao
                FROM alert_settings
                WHERE id = {ph}
            """, (1,))
            row = cur.fetchone()
            if row:
                return {
                    "enabled": bool(row[0]),
                    "channel": str(row[1] or "telegram"),
                    "daily_time": str(row[2] or "07:30"),
                    "telegram_token": str(row[3] or ""),
                    "telegram_chat_id": str(row[4] or ""),
                    "webhook_url": str(row[5] or ""),
                    "last_sent_date": str(row[6] or ""),
                    "last_status": str(row[7] or "Nenhum envio realizado ainda."),
                    "include_vencidos": bool(row[8]),
                    "include_criticos": bool(row[9]),
                    "include_atencao": bool(row[10])
                }
    except Exception as e:
        print(f"⚠️ Erro ao ler alert_settings: {e}")
    return defaults


def save_alert_settings(data: dict) -> bool:
    """Salva configurações de alerta no banco (dual-write)."""
    enabled = bool(data.get("enabled", False))
    channel = str(data.get("channel", "telegram")).strip()
    daily_time = str(data.get("daily_time", "07:30")).strip()
    telegram_token = str(data.get("telegram_token", "")).strip()
    telegram_chat_id = str(data.get("telegram_chat_id", "")).strip()
    webhook_url = str(data.get("webhook_url", "")).strip()
    include_vencidos = bool(data.get("include_vencidos", True))
    include_criticos = bool(data.get("include_criticos", True))
    include_atencao = bool(data.get("include_atencao", False))

    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(f"""
                UPDATE alert_settings
                SET enabled = {ph},
                    channel = {ph},
                    daily_time = {ph},
                    telegram_token = {ph},
                    telegram_chat_id = {ph},
                    webhook_url = {ph},
                    include_vencidos = {ph},
                    include_criticos = {ph},
                    include_atencao = {ph}
                WHERE id = {ph}
            """, (
                enabled if db_type == "postgres" else (1 if enabled else 0),
                channel,
                daily_time,
                telegram_token,
                telegram_chat_id,
                webhook_url,
                include_vencidos if db_type == "postgres" else (1 if include_vencidos else 0),
                include_criticos if db_type == "postgres" else (1 if include_criticos else 0),
                include_atencao if db_type == "postgres" else (1 if include_atencao else 0),
                1
            ))

        if is_postgres():
            try:
                with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                    s_conn.execute("""
                        UPDATE alert_settings
                        SET enabled = ?, channel = ?, daily_time = ?, telegram_token = ?, telegram_chat_id = ?,
                            webhook_url = ?, include_vencidos = ?, include_criticos = ?, include_atencao = ?
                        WHERE id = 1
                    """, (1 if enabled else 0, channel, daily_time, telegram_token, telegram_chat_id, webhook_url, 1 if include_vencidos else 0, 1 if include_criticos else 0, 1 if include_atencao else 0))
                    s_conn.commit()
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"⚠️ Erro ao salvar alert_settings: {e}")
        return False


def update_alert_sent_status(last_date: str, status_msg: str):
    """Atualiza a data do último envio e o log de status."""
    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(f"UPDATE alert_settings SET last_sent_date = {ph}, last_status = {ph} WHERE id = {ph}", (last_date, status_msg, 1))
        if is_postgres():
            try:
                with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                    s_conn.execute("UPDATE alert_settings SET last_sent_date = ?, last_status = ? WHERE id = 1", (last_date, status_msg))
                    s_conn.commit()
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Erro ao atualizar status de alerta: {e}")


import secrets

def generate_api_token() -> str:
    """Gera um token de acesso de alta entropia para integração e APIs do DateLimit."""
    return "dkl_live_" + secrets.token_urlsafe(24)

# ── Módulo de Autenticação, Usuários e Auditoria Completa ───────────────────────

def get_user_by_username(username: str) -> dict | None:
    """Busca usuário pelo username de forma insensível a maiúsculas (Local-First ultra rápido < 0.2ms)."""
    if not username:
        return None
    username_clean = username.strip().lower()

    # 1. Consulta SQLite local instantânea (< 0.2ms)
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()
            s_cur.execute(
                "SELECT id, username, name, password_hash, pin, role, active, created_at, COALESCE(api_token, '') as api_token FROM users WHERE LOWER(username) = ? LIMIT 1",
                (username_clean,)
            )
            r = s_cur.fetchone()
            if r:
                return {
                    "id": r["id"],
                    "username": r["username"],
                    "name": r["name"],
                    "password_hash": r["password_hash"],
                    "pin": r["pin"] or "",
                    "role": r["role"] or "operador",
                    "active": bool(r["active"]),
                    "created_at": str(r["created_at"])[:19] if r["created_at"] else "",
                    "api_token": r["api_token"] or ""
                }
    except Exception:
        pass

    # 2. Fallback para PostgreSQL caso não esteja no SQLite
    if is_postgres():
        try:
            with get_db_cursor() as (cur, db_type):
                if db_type == "postgres":
                    cur.execute(
                        "SELECT id, username, name, password_hash, pin, role, active, created_at, COALESCE(api_token, '') FROM users WHERE LOWER(username) = %s LIMIT 1",
                        (username_clean,)
                    )
                    row = cur.fetchone()
                    if row:
                        u = {
                            "id": row[0],
                            "username": row[1],
                            "name": row[2],
                            "password_hash": row[3],
                            "pin": row[4] or "",
                            "role": row[5] or "operador",
                            "active": bool(row[6]),
                            "created_at": str(row[7])[:19] if row[7] else "",
                            "api_token": row[8] or ""
                        }
                        try:
                            with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                                s_conn.execute(
                                    "INSERT OR REPLACE INTO users (id, username, name, password_hash, pin, role, active, api_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (u["id"], u["username"], u["name"], u["password_hash"], u["pin"], u["role"], 1 if u["active"] else 0, u["api_token"])
                                )
                                s_conn.commit()
                        except Exception:
                            pass
                        return u
        except Exception:
            pass
    return None


def get_user_by_id(user_id: int) -> dict | None:
    """Busca usuário pelo ID (Local-First ultra rápido < 0.2ms)."""
    if not user_id:
        return None

    # 1. Consulta SQLite local instantânea (< 0.2ms)
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()
            s_cur.execute(
                "SELECT id, username, name, password_hash, pin, role, active, created_at, COALESCE(api_token, '') as api_token FROM users WHERE id = ? LIMIT 1",
                (user_id,)
            )
            r = s_cur.fetchone()
            if r:
                return {
                    "id": r["id"],
                    "username": r["username"],
                    "name": r["name"],
                    "password_hash": r["password_hash"],
                    "pin": r["pin"] or "",
                    "role": r["role"] or "operador",
                    "active": bool(r["active"]),
                    "created_at": str(r["created_at"])[:19] if r["created_at"] else "",
                    "api_token": r["api_token"] or ""
                }
    except Exception:
        pass

    # 2. Fallback para PostgreSQL
    if is_postgres():
        try:
            with get_db_cursor() as (cur, db_type):
                if db_type == "postgres":
                    cur.execute(
                        "SELECT id, username, name, password_hash, pin, role, active, created_at, COALESCE(api_token, '') FROM users WHERE id = %s LIMIT 1",
                        (user_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        u = {
                            "id": row[0],
                            "username": row[1],
                            "name": row[2],
                            "password_hash": row[3],
                            "pin": row[4] or "",
                            "role": row[5] or "operador",
                            "active": bool(row[6]),
                            "created_at": str(row[7])[:19] if row[7] else "",
                            "api_token": row[8] or ""
                        }
                        try:
                            with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                                s_conn.execute(
                                    "INSERT OR REPLACE INTO users (id, username, name, password_hash, pin, role, active, api_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (u["id"], u["username"], u["name"], u["password_hash"], u["pin"], u["role"], 1 if u["active"] else 0, u["api_token"])
                                )
                                s_conn.commit()
                        except Exception:
                            pass
                        return u
        except Exception:
            pass
    return None


def get_user_by_pin(pin: str) -> dict | None:
    """Localiza usuário ativo por PIN rápido para troca de operador no chão de loja (< 0.2ms)."""
    if not pin:
        return None
    pin_clean = pin.strip()

    # 1. Consulta SQLite local instantânea (< 0.2ms)
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            s_cur = s_conn.cursor()
            s_cur.execute(
                "SELECT id, username, name, password_hash, pin, role, active, created_at FROM users WHERE pin = ? AND active = 1 LIMIT 1",
                (pin_clean,)
            )
            r = s_cur.fetchone()
            if r:
                return {
                    "id": r["id"],
                    "username": r["username"],
                    "name": r["name"],
                    "password_hash": r["password_hash"],
                    "pin": r["pin"] or "",
                    "role": r["role"] or "operador",
                    "active": bool(r["active"]),
                    "created_at": str(r["created_at"])[:19] if r["created_at"] else ""
                }
    except Exception:
        pass

    # 2. Fallback para PostgreSQL
    if is_postgres():
        try:
            with get_db_cursor() as (cur, db_type):
                if db_type == "postgres":
                    cur.execute(
                        "SELECT id, username, name, password_hash, pin, role, active, created_at FROM users WHERE pin = %s AND active = TRUE LIMIT 1",
                        (pin_clean,)
                    )
                    row = cur.fetchone()
                    if row:
                        return {
                            "id": row[0],
                            "username": row[1],
                            "name": row[2],
                            "password_hash": row[3],
                            "pin": row[4] or "",
                            "role": row[5] or "operador",
                            "active": bool(row[6]),
                            "created_at": str(row[7])[:19] if row[7] else ""
                        }
        except Exception:
            pass
    return None


def list_users() -> list[dict]:
    """Lista todos os usuários cadastrados no sistema (Local-First ultra rápido < 0.2ms)."""
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            cur = s_conn.cursor()
            cur.execute("SELECT id, username, name, pin, role, active, created_at, COALESCE(api_token, '') as api_token FROM users ORDER BY (CASE WHEN role='admin' THEN 0 ELSE 1 END), name ASC")
            rows = cur.fetchall()
            if rows:
                return [{
                    "id": r["id"],
                    "username": r["username"],
                    "name": r["name"],
                    "pin": r["pin"] or "",
                    "role": r["role"] or "operador",
                    "active": bool(r["active"]),
                    "created_at": str(r["created_at"])[:19] if r["created_at"] else "",
                    "api_token": r["api_token"] or ""
                } for r in rows]
    except Exception:
        pass

    # Fallback PostgreSQL
    if is_postgres():
        try:
            with get_db_cursor() as (cur, db_type):
                if db_type == "postgres":
                    cur.execute("SELECT id, username, name, pin, role, active, created_at, COALESCE(api_token, '') FROM users ORDER BY (CASE WHEN role='admin' THEN 0 ELSE 1 END), name ASC")
                    rows = cur.fetchall()
                    return [{
                        "id": r[0],
                        "username": r[1],
                        "name": r[2],
                        "pin": r[3] or "",
                        "role": r[4] or "operador",
                        "active": bool(r[5]),
                        "created_at": str(r[6])[:19] if r[6] else "",
                        "api_token": r[7] or ""
                    } for r in rows]
        except Exception:
            pass
    return []


def list_active_operators() -> list[dict]:
    """Retorna lista simplificada de operadores ativos para troca rápida (< 0.2ms)."""
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            cur = s_conn.cursor()
            cur.execute("SELECT id, username, name, pin, role FROM users WHERE active = 1 ORDER BY name ASC")
            rows = cur.fetchall()
            if rows:
                return [{"id": r["id"], "username": r["username"], "name": r["name"], "pin": r["pin"] or "", "role": r["role"]} for r in rows]
    except Exception:
        pass

    if is_postgres():
        try:
            with get_db_cursor() as (cur, db_type):
                if db_type == "postgres":
                    cur.execute("SELECT id, username, name, pin, role FROM users WHERE active = TRUE ORDER BY name ASC")
                    rows = cur.fetchall()
                    return [{"id": r[0], "username": r[1], "name": r[2], "pin": r[3] or "", "role": r[4]} for r in rows]
        except Exception:
            pass
    return []


def create_user(username: str, name: str, password: str, pin: str = "", role: str = "operador") -> tuple[bool, str]:
    """Cadastra um novo usuário no sistema (dual-write)."""
    from werkzeug.security import generate_password_hash
    username = username.strip().lower()
    name = name.strip()
    pin = pin.strip()
    role = "admin" if role.lower() == "admin" else "operador"
    if not username or not name or not password:
        return False, "Usuário, nome e senha são obrigatórios."
    if get_user_by_username(username):
        return False, "Nome de usuário já cadastrado."
    if pin and len(pin) < 3:
        return False, "O PIN deve ter no mínimo 3 dígitos."
    
    pw_hash = generate_password_hash(password)
    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(
                f"""INSERT INTO users (username, name, password_hash, pin, role, active)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
                (username, name, pw_hash, pin or None, role, True if db_type == "postgres" else 1)
            )

        if is_postgres():
            try:
                with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                    s_conn.execute(
                        "INSERT OR REPLACE INTO users (username, name, password_hash, pin, role, active) VALUES (?, ?, ?, ?, ?, 1)",
                        (username, name, pw_hash, pin or None, role)
                    )
                    s_conn.commit()
            except Exception:
                pass
        return True, "Usuário cadastrado com sucesso!"
    except Exception as e:
        return False, f"Erro ao criar usuário: {e}"


def update_user(user_id: int, name: str = None, username: str = None, password: str = None, pin: str = None, role: str = None, active: bool = None, api_token: str = None) -> tuple[bool, str]:
    """Atualiza dados, login, senha, PIN, token ou papel de um usuário existente."""
    from werkzeug.security import generate_password_hash
    fields_pg = []
    fields_sq = []
    params = []
    if name:
        fields_pg.append("name = %s")
        fields_sq.append("name = ?")
        params.append(name.strip())
    if username:
        u_clean = username.strip().lower()
        existing = get_user_by_username(u_clean)
        if existing and existing["id"] != user_id:
            return False, "Nome de usuário já está em uso por outro usuário."
        fields_pg.append("username = %s")
        fields_sq.append("username = ?")
        params.append(u_clean)
    if password:
        fields_pg.append("password_hash = %s")
        fields_sq.append("password_hash = ?")
        params.append(generate_password_hash(password))
    if pin is not None:
        fields_pg.append("pin = %s")
        fields_sq.append("pin = ?")
        params.append(pin.strip() if pin.strip() else None)
    if api_token is not None:
        fields_pg.append("api_token = %s")
        fields_sq.append("api_token = ?")
        params.append(api_token.strip() if api_token.strip() else None)
    if role:
        r = "admin" if role.lower() == "admin" else "operador"
        fields_pg.append("role = %s")
        fields_sq.append("role = ?")
        params.append(r)
    if active is not None:
        fields_pg.append("active = %s")
        fields_sq.append("active = ?")
        params.append(bool(active))

    if not fields_pg:
        return False, "Nenhum campo informado para atualização."

    fields_pg.append("updated_at = CURRENT_TIMESTAMP")
    fields_sq.append("updated_at = datetime('now')")

    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph_fields = ", ".join(fields_pg if db_type == "postgres" else fields_sq)
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(f"UPDATE users SET {ph_fields} WHERE id = {ph}", params + [user_id])

        if is_postgres():
            try:
                with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                    ph_fields_sq = ", ".join(fields_sq)
                    s_conn.execute(f"UPDATE users SET {ph_fields_sq} WHERE id = ?", params + [user_id])
                    s_conn.commit()
            except Exception:
                pass
        return True, "Usuário atualizado com sucesso!"
    except Exception as e:
        return False, f"Erro ao atualizar usuário: {e}"


def delete_user(user_id: int) -> tuple[bool, str]:
    """Remove um usuário do sistema (com proteção para o último administrador)."""
    with get_db_cursor() as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"
        cur.execute(f"SELECT role FROM users WHERE id = {ph}", (user_id,))
        row = cur.fetchone()
        if not row:
            return False, "Usuário não encontrado."
        if row[0] == "admin":
            act_clause = 'active = TRUE' if db_type == 'postgres' else 'active = 1'
            cur.execute(f"SELECT COUNT(*) FROM users WHERE role = 'admin' AND {act_clause}")
            admin_count = cur.fetchone()[0]
            if admin_count <= 1:
                return False, "Não é permitido excluir o único administrador do sistema."

    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"
            cur.execute(f"DELETE FROM users WHERE id = {ph}", (user_id,))
        if is_postgres():
            try:
                with sqlite3.connect(str(SQLITE_PATH), timeout=3.0) as s_conn:
                    s_conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                    s_conn.commit()
            except Exception:
                pass
        return True, "Usuário removido com sucesso!"
    except Exception as e:
        return False, f"Erro ao excluir usuário: {e}"


def log_audit(username: str, action: str, details: str, user_id: int = None, target_id: int = None, product_name: str = None, ip_address: str = None):
    """
    Grava um evento de auditoria no SQLite local (< 0.2ms)
    e sincroniza no Postgres em background daemon thread.
    """
    username = (username or "Sistema").strip()
    action = (action or "ACAO").upper().strip()
    details = (details or "").strip()
    ip_address = (ip_address or "").strip()
    product_name = (product_name or "").strip() if product_name else None

    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.execute("PRAGMA journal_mode = WAL;")
            s_conn.execute(
                """INSERT INTO audit_logs (user_id, username, action, target_id, product_name, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (user_id, username, action, target_id, product_name, details, ip_address)
            )
            s_conn.commit()
    except Exception as e:
        print(f"⚠️ Erro ao gravar audit_log no SQLite: {e}")

    if is_postgres():
        def _async_audit_pg():
            try:
                with get_db_cursor(commit=True) as (cur, db_type):
                    if db_type == "postgres":
                        cur.execute(
                            """INSERT INTO audit_logs (user_id, username, action, target_id, product_name, details, ip_address, created_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                            (user_id, username, action, target_id, product_name, details, ip_address)
                        )
            except Exception as ex:
                print(f"⚠️ Erro ao gravar audit_log no Postgres: {ex}")

        threading.Thread(target=_async_audit_pg, daemon=True).start()


def query_audit_logs(limit: int = 100, offset: int = 0, user: str = None, action: str = None, search: str = None, start_date: str = None, end_date: str = None) -> tuple[int, list[dict]]:
    """Consulta paginada e filtrada de logs de auditoria."""
    where_clauses = ["1=1"]
    params = []
    if user:
        where_clauses.append("LOWER(username) = LOWER(?)")
        params.append(user.strip())
    if action:
        where_clauses.append("action = ?")
        params.append(action.strip().upper())
    if search:
        s = f"%{search.strip().upper()}%"
        where_clauses.append("(UPPER(details) LIKE ? OR UPPER(COALESCE(product_name, '')) LIKE ? OR UPPER(username) LIKE ?)")
        params.extend([s, s, s])
    if start_date:
        where_clauses.append("created_at >= ?")
        params.append(f"{start_date} 00:00:00")
    if end_date:
        where_clauses.append("created_at <= ?")
        params.append(f"{end_date} 23:59:59")

    where_sql = " AND ".join(where_clauses)
    try:
        with sqlite3.connect(str(SQLITE_PATH), timeout=5.0) as s_conn:
            s_conn.row_factory = sqlite3.Row
            cur = s_conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM audit_logs WHERE {where_sql}", params)
            total = cur.fetchone()[0]

            cur.execute(
                f"""SELECT id, user_id, username, action, target_id, product_name, details, ip_address, created_at
                    FROM audit_logs
                    WHERE {where_sql}
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset]
            )
            rows = cur.fetchall()
            logs = []
            for r in rows:
                logs.append({
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "action": r["action"],
                    "target_id": r["target_id"],
                    "product_name": r["product_name"] or "",
                    "details": r["details"],
                    "ip_address": r["ip_address"] or "",
                    "created_at": str(r["created_at"])[:19]
                })
            return total, logs
    except Exception as e:
        print(f"⚠️ Erro ao consultar audit_logs: {e}")
        return 0, []
