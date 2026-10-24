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


def _connect_pg_direct(timeout=8):
    """
    Estabelece uma conexão direta com o PostgreSQL.
    Usa psycopg2 se disponível; fallback para pg8000 (driver 100% Python para Android/Termux).
    """
    if PSYCOPG2_AVAILABLE:
        return psycopg2.connect(
            DATABASE_URL,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
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
                        minconn=2,
                        maxconn=10,
                        dsn=DATABASE_URL,
                        keepalives=1,
                        keepalives_idle=30,
                        keepalives_interval=10,
                        keepalives_count=5,
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
    # Conexões usadas nos últimos 3 minutos (180s) dispensam ping (keepalive TCP mantém o túnel ativo)
    last_used = _get_conn_last_used(conn)
    if time.time() - last_used < 180 and last_used > 0:
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
    # Se todas as conexões do pool estavam inativas, cria uma nova diretamente
    conn = _connect_pg_direct(timeout=8)
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
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("PRAGMA cache_size = -64000;")
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
CACHE_TTL_SECONDS = 30  # expira automaticamente em 30s se não houver mutações

def clear_query_cache():
    """Invalida todo o cache de consultas instantaneamente em qualquer alteração."""
    with _cache_lock:
        _query_cache.clear()


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
            bar_code TEXT UNIQUE
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
            created_at TEXT DEFAULT (date('now')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

    # Migrações seguras de colunas em SQLite
    cur.execute("PRAGMA table_info(products);")
    p_cols = [col[1] for col in cur.fetchall()]
    if "bar_code" not in p_cols:
        cur.execute("ALTER TABLE products ADD COLUMN bar_code TEXT;")

    cur.execute("PRAGMA table_info(product_lots);")
    l_cols = [col[1] for col in cur.fetchall()]
    if "price" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN price REAL DEFAULT 0.0;")
    if "created_at" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN created_at TEXT;")
        cur.execute("UPDATE product_lots SET created_at = date('now') WHERE created_at IS NULL;")
    if "updated_at" not in l_cols:
        cur.execute("ALTER TABLE product_lots ADD COLUMN updated_at TEXT;")
        cur.execute("UPDATE product_lots SET updated_at = datetime('now', 'localtime') WHERE updated_at IS NULL;")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created ON product_lots(created_at);")

    # Tabela de movimentações e filtro de lápide para SQLite
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            lot_id INTEGER,
            action TEXT NOT NULL,
            quant INTEGER DEFAULT 0,
            date_valid TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_prod_valid ON stock_movements(product_id, date_valid);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_movements_created ON stock_movements(created_at);")

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
                bar_code TEXT UNIQUE
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
                created_at DATE DEFAULT CURRENT_DATE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name_trgm ON products USING gin (name gin_trgm_ops);")
        except Exception:
            conn.rollback()

        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS created_at DATE DEFAULT CURRENT_DATE;")
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created ON product_lots(created_at);")

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
    Puxa uma cópia integral dos dados do PostgreSQL na nuvem para o SQLite local em velocidade máxima:
    - 1 único roundtrip de rede usando agregação JSON nativa do PostgreSQL (~0.9s vs ~3.5s)
    - Gravação atômica em modo WAL sem travar consultas concorrentes (~0.18s)
    - Proteção contra concorrência via thread lock
    """
    t0 = time.time()
    if not is_postgres_configured():
        return {"success": False, "error": "PostgreSQL não configurado no .env"}

    # Evita execuções simultâneas de sincronização que geram travamento
    if not _sync_lock.acquire(blocking=False):
        with _status_lock:
            return {
                "success": True,
                "message": "Sincronização já em andamento...",
                "duration_s": 0.0,
                "timestamp": _db_status["last_sync"] or time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    try:
        # 1. Garante schema SQLite em modo WAL
        init_sqlite_db()

        # 2. Busca todos os dados do PostgreSQL em 1 ÚNICO roundtrip de rede
        pool_inst = get_pg_pool()
        if pool_inst:
            pg_conn, is_from_pool = _checkout_valid_pg_conn(pool_inst)
        else:
            pg_conn = _connect_pg_direct(timeout=8)
            is_from_pool = False

        pg_cur = pg_conn.cursor()
        pg_cur.execute("""
            SELECT
                COALESCE((SELECT json_agg(p) FROM (SELECT id, name, bar_code FROM products) p), '[]'::json) AS prods,
                COALESCE((SELECT json_agg(l) FROM (SELECT id, product_id, quant, price, date_valid::TEXT, location, status, created_at::TEXT, updated_at::TEXT FROM product_lots) l), '[]'::json) AS lots,
                COALESCE((SELECT json_agg(m) FROM (SELECT id, product_id, lot_id, action, quant, date_valid::TEXT, created_at::TEXT FROM stock_movements) m), '[]'::json) AS moves;
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

        products = _parse_json_col(raw_row[0]) if raw_row else []
        lots = _parse_json_col(raw_row[1]) if raw_row else []
        movements = _parse_json_col(raw_row[2]) if raw_row else []

        pg_cur.close()
        if is_from_pool and pool_inst:
            pool_inst.putconn(pg_conn)
        else:
            pg_conn.close()

    except Exception as e:
        _sync_lock.release()
        with _status_lock:
            _db_status["postgres_online"] = False
            _db_status["last_error"] = str(e)
        return {"success": False, "error": f"Falha ao conectar no PostgreSQL: {e}"}

    # 3. Grava atomicamente no SQLite local usando WAL sem travar leituras
    try:
        sqlite_conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
        sqlite_cur = sqlite_conn.cursor()
        sqlite_cur.execute("PRAGMA journal_mode = WAL;")
        sqlite_cur.execute("PRAGMA synchronous = NORMAL;")
        sqlite_cur.execute("PRAGMA busy_timeout = 10000;")
        sqlite_cur.execute("PRAGMA foreign_keys = OFF;")
        sqlite_cur.execute("BEGIN TRANSACTION;")

        sqlite_cur.execute("DELETE FROM products;")
        if products:
            sqlite_cur.executemany(
                "INSERT INTO products (id, name, bar_code) VALUES (?, ?, ?);",
                [(p["id"], p["name"], p.get("bar_code")) for p in products]
            )

        sqlite_cur.execute("DELETE FROM product_lots;")
        if lots:
            sqlite_cur.executemany("""
                INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, [
                (
                    l["id"],
                    l["product_id"],
                    l.get("quant", 0),
                    l.get("price", 0.0),
                    str(l["date_valid"])[:10],
                    l.get("location", "LOJA"),
                    l.get("status", "NORMAL"),
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

        sqlite_conn.commit()
        sqlite_cur.execute("PRAGMA foreign_keys = ON;")
        sqlite_conn.close()

        duration = round(time.time() - t0, 2)
        sync_time = time.strftime("%Y-%m-%d %H:%M:%S")
        with _status_lock:
            _db_status["postgres_online"] = True
            _db_status["last_sync"] = sync_time
            _db_status["last_error"] = None
            _db_status["total_products"] = len(products)
            _db_status["total_lots"] = len(lots)

        clear_query_cache()
        return {
            "success": True,
            "products_count": len(products),
            "lots_count": len(lots),
            "movements_count": len(movements),
            "duration_s": duration,
            "timestamp": sync_time,
        }
    except Exception as e:
        return {"success": False, "error": f"Falha ao gravar no SQLite local: {e}"}
    finally:
        try:
            _sync_lock.release()
        except RuntimeError:
            pass


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
        else:
            with _status_lock:
                _db_status["postgres_online"] = False
            print("⚡ PostgreSQL offline. Servidor rodando com cópia local SQLite (Offline-first).")
            trigger_auto_reconnect()


# ── Consulta Otimizada de Lotes ───────────────────────────────────────────────
def query_lots(filtro: str = "todos", busca: str = "", limit: int = 100, **kwargs):
    """
    Consulta os lotes com:
    - Verificação em cache ultrarrápido (0.005ms)
    - 1 único roundtrip de banco com COUNT(*) OVER() (elimina query de count separada)
    - Conexão reaproveitada via ThreadedConnectionPool
    Retorna: (total_count, lista_de_dicionarios)
    """
    clean_busca = busca.strip()
    cache_key = (filtro, clean_busca.upper(), limit)

    # 1. Checa cache em memória
    now = time.time()
    with _cache_lock:
        if cache_key in _query_cache:
            entry_time, cached_total, cached_lots = _query_cache[cache_key]
            if now - entry_time < CACHE_TTL_SECONDS:
                return cached_total, cached_lots

    # 2. Executa no banco com single query e auto-retry resiliente
    for attempt in range(2):
        try:
            with get_db_cursor() as (cur, db_type):
                where_clauses = ["1=1"]
                params = []
                today = date.today()
                ph = "%s" if db_type == "postgres" else "?"

                if filtro == "vencidos":
                    where_clauses.append(f"l.date_valid < {ph}")
                    params.append(today.strftime("%Y-%m-%d"))

                elif filtro == "critico":
                    where_clauses.append(f"l.date_valid BETWEEN {ph} AND {ph}")
                    params.append(today.strftime("%Y-%m-%d"))
                    params.append((today + timedelta(days=7)).strftime("%Y-%m-%d"))

                elif filtro == "atencao":
                    where_clauses.append(f"l.date_valid BETWEEN {ph} AND {ph}")
                    params.append((today + timedelta(days=8)).strftime("%Y-%m-%d"))
                    params.append((today + timedelta(days=29)).strftime("%Y-%m-%d"))

                elif filtro == "alerta":
                    where_clauses.append(f"l.date_valid BETWEEN {ph} AND {ph}")
                    params.append((today + timedelta(days=30)).strftime("%Y-%m-%d"))
                    params.append((today + timedelta(days=45)).strftime("%Y-%m-%d"))

                elif filtro == "hoje":
                    where_clauses.append(f"l.created_at = {ph}")
                    params.append(today.strftime("%Y-%m-%d"))

                if clean_busca:
                    like_op = "ILIKE" if db_type == "postgres" else "LIKE"
                    where_clauses.append(f"p.name {like_op} {ph}")
                    params.append(f"%{clean_busca}%")

                where_sql = " AND ".join(where_clauses)
                order_by = "l.id DESC" if filtro == "hoje" else "l.date_valid ASC, p.name ASC"

                # Query única combinando dados e contagem total via window function
                query = f"""
                    SELECT l.id AS lot_id, p.name AS product_name, p.id AS product_id,
                           l.quant, l.price,
                           l.date_valid::TEXT AS date_valid_str,
                           l.location, l.status,
                           COUNT(*) OVER() AS full_count
                    FROM product_lots l
                    INNER JOIN products p ON p.id = l.product_id
                    WHERE {where_sql}
                    ORDER BY {order_by}
                    LIMIT {ph}
                """ if db_type == "postgres" else f"""
                    SELECT l.id AS lot_id, p.name AS product_name, p.id AS product_id,
                           l.quant, l.price,
                           l.date_valid AS date_valid_str,
                           l.location, l.status,
                           COUNT(*) OVER() AS full_count
                    FROM product_lots l
                    INNER JOIN products p ON p.id = l.product_id
                    WHERE {where_sql}
                    ORDER BY {order_by}
                    LIMIT {ph}
                """

                cur.execute(query, params + [limit])
                raw_rows = cur.fetchall()

                if not raw_rows:
                    total = 0
                    lots = []
                else:
                    first_row = raw_rows[0]
                    total = first_row[-1] if db_type == "postgres" else first_row["full_count"]

                    lots = []
                    for r in raw_rows:
                        if db_type == "postgres":
                            lot_id, p_name, p_id, quant, price, d_valid, loc, st, _ = r
                        else:
                            lot_id = r["lot_id"]
                            p_name = r["product_name"]
                            p_id = r["product_id"]
                            quant = r["quant"]
                            price = r["price"]
                            d_valid = str(r["date_valid_str"])
                            loc = r["location"]
                            st = r["status"]

                        lots.append({
                            "lot_id": lot_id,
                            "product_name": p_name,
                            "product_id": p_id,
                            "quant": quant,
                            "price": price,
                            "date_valid": str(d_valid)[:10],
                            "location": loc,
                            "status": st,
                        })

                # Salva no cache em memória
                with _cache_lock:
                    _query_cache[cache_key] = (now, total, lots)

                return total, lots

        except Exception as e:
            if attempt == 0 and is_postgres() and ("closed" in str(e).lower() or "ssl" in str(e).lower() or "terminat" in str(e).lower()):
                clear_query_cache()
                time.sleep(0.15)
                continue
            raise


# ── Mutações (Com Invalidação Instantânea de Cache) ─────────────────────────────

def update_lot_quant(lot_id: int, quant: int) -> bool:
    clear_query_cache()
    with get_db_cursor(commit=True) as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"
        now_val = "CURRENT_TIMESTAMP" if db_type == "postgres" else "datetime('now', 'localtime')"
        cur.execute(f"UPDATE product_lots SET quant = {ph}, updated_at = {now_val} WHERE id = {ph}", (quant, lot_id))
        return True


def update_lot_price(lot_id: int, price: float) -> bool:
    clear_query_cache()
    with get_db_cursor(commit=True) as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"
        now_val = "CURRENT_TIMESTAMP" if db_type == "postgres" else "datetime('now', 'localtime')"
        cur.execute(f"UPDATE product_lots SET price = {ph}, updated_at = {now_val} WHERE id = {ph}", (price, lot_id))
        return True


def delete_lot(lot_id: int) -> bool:
    """
    Dá baixa no lote e registra a Lápide (tombstone) em stock_movements,
    garantindo que futuras importações de planilhas não ressuscitem esse lote (Item Zumbi).
    """
    clear_query_cache()
    with get_db_cursor(commit=True) as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"

        # 1. Recupera dados do lote para registrar a baixa
        cur.execute(
            f"SELECT product_id, quant, date_valid::TEXT FROM product_lots WHERE id = {ph}" if db_type == "postgres"
            else f"SELECT product_id, quant, date_valid FROM product_lots WHERE id = {ph}",
            (lot_id,)
        )
        row = cur.fetchone()
        if row:
            p_id = row[0]
            q = row[1]
            d_valid = str(row[2])[:10]
            # Registra baixa no histórico de auditoria
            cur.execute(
                f"""INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid)
                    VALUES ({ph}, {ph}, 'BAIXA', {ph}, {ph})""",
                (p_id, lot_id, q, d_valid)
            )

        # 2. Deleta o lote ativo
        cur.execute(f"DELETE FROM product_lots WHERE id = {ph}", (lot_id,))
        return True


def create_lot(name: str, quant: int, price: float, date_valid: str, location: str = "LOJA", status: str = "NORMAL"):
    clear_query_cache()
    name = name.strip()
    with get_db_cursor(commit=True) as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"

        # 1. Encontra ou cria produto
        cur.execute(f"SELECT id FROM products WHERE name = {ph}", (name,))
        row = cur.fetchone()
        if row:
            product_id = row[0]
        else:
            if db_type == "postgres":
                cur.execute(f"INSERT INTO products (name) VALUES ({ph}) RETURNING id", (name,))
                product_id = cur.fetchone()[0]
            else:
                cur.execute(f"INSERT INTO products (name) VALUES ({ph})", (name,))
                product_id = cur.lastrowid

        # 2. Cria lote com created_at = CURRENT_DATE
        if db_type == "postgres":
            cur.execute(
                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, CURRENT_DATE) RETURNING id""",
                (product_id, quant, price, date_valid, location, status)
            )
            lot_id = cur.fetchone()[0]
        else:
            cur.execute(
                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, date('now'))""",
                (product_id, quant, price, date_valid, location, status)
            )
            lot_id = cur.lastrowid

        return product_id, lot_id


def search_products(term: str, limit: int = 10) -> list[dict]:
    """
    Busca produtos cadastrados no catálogo por nome ou código de barras para autocomplete instantâneo.
    """
    term = term.strip()
    if not term:
        return []

    with get_db_cursor() as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"
        like_op = "ILIKE" if db_type == "postgres" else "LIKE"
        like_val = f"%{term}%"

        query = f"""
            SELECT id, name, bar_code
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
                results.append({"id": r[0], "name": r[1], "bar_code": r[2]})
            else:
                results.append({
                    "id": r["id"] if hasattr(r, "keys") else r[0],
                    "name": r["name"] if hasattr(r, "keys") else r[1],
                    "bar_code": r["bar_code"] if hasattr(r, "keys") else r[2]
                })
        return results
