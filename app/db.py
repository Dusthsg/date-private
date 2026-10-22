"""
Camada de persistência/banco de dados para DateLimit.
Suporta PostgreSQL (padrão quando DATABASE_URL está configurada) e SQLite (fallback).
"""

import os
import sqlite3
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
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False


def is_postgres() -> bool:
    return bool(DATABASE_URL and PSYCOPG2_AVAILABLE)


def get_connection():
    """
    Retorna uma conexão aberta com o banco.
    Se DATABASE_URL estiver configurada, conecta ao PostgreSQL.
    Caso contrário, conecta ao SQLite local.
    """
    if is_postgres():
        conn = psycopg2.connect(DATABASE_URL)
        return conn, "postgres"
    else:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn, "sqlite"


def init_db():
    """Cria tabelas e índices se ainda não existirem."""
    conn, db_type = get_connection()
    try:
        cur = conn.cursor()
        if db_type == "postgres":
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
                    status TEXT DEFAULT 'NORMAL'
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

            # Garante que a coluna 'price' existe no PostgreSQL
            cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
            conn.commit()
        else:
            cur.execute("PRAGMA foreign_keys = ON;")
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
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

            # Garante que a coluna 'price' existe no SQLite
            cur.execute("PRAGMA table_info(product_lots);")
            cols = [col[1] for col in cur.fetchall()]
            if "price" not in cols:
                cur.execute("ALTER TABLE product_lots ADD COLUMN price REAL DEFAULT 0.0;")
            conn.commit()
    finally:
        conn.close()


def query_lots(filtro: str = "todos", busca: str = "", limit: int = 100, **kwargs):
    """
    Consulta os lotes com suporte a:
    - filtro de data (vencidos, critico, atencao, alerta, todos)
    - busca por nome única usando LIKE (%termo%)
    - limite de registros para máxima fluidez
    Retorna: (total_count, lista_de_dicionarios)
    """
    conn, db_type = get_connection()
    try:
        where_clauses = ["1=1"]
        params = []
        today = date.today()

        # Placeholders: %s no Postgres, ? no SQLite
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

        if busca:
            like_op = "ILIKE" if db_type == "postgres" else "LIKE"
            where_clauses.append(f"p.name {like_op} {ph}")
            params.append(f"%{busca}%")

        where_sql = " AND ".join(where_clauses)

        # 1. Total count
        count_sql = f"""
            SELECT COUNT(*)
            FROM product_lots l
            INNER JOIN products p ON p.id = l.product_id
            WHERE {where_sql}
        """
        cur = conn.cursor()
        cur.execute(count_sql, params)
        total = cur.fetchone()[0]

        # 2. Registros
        query = f"""
            SELECT l.id AS lot_id, p.name AS product_name, p.id AS product_id,
                   l.quant, l.price,
                   l.date_valid::TEXT AS date_valid_str,
                   l.location, l.status
            FROM product_lots l
            INNER JOIN products p ON p.id = l.product_id
            WHERE {where_sql}
            ORDER BY l.date_valid ASC, p.name ASC
            LIMIT {ph}
        """ if db_type == "postgres" else f"""
            SELECT l.id AS lot_id, p.name AS product_name, p.id AS product_id,
                   l.quant, l.price,
                   l.date_valid AS date_valid_str,
                   l.location, l.status
            FROM product_lots l
            INNER JOIN products p ON p.id = l.product_id
            WHERE {where_sql}
            ORDER BY l.date_valid ASC, p.name ASC
            LIMIT {ph}
        """

        cur.execute(query, params + [limit])
        raw_rows = cur.fetchall()

        lots = []
        for r in raw_rows:
            # Normalizar para dict
            if db_type == "postgres":
                lot_id, p_name, p_id, quant, price, d_valid, loc, st = r
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

        return total, lots
    finally:
        conn.close()


def update_lot_quant(lot_id: int, quant: int) -> bool:
    conn, db_type = get_connection()
    try:
        ph = "%s" if db_type == "postgres" else "?"
        cur = conn.cursor()
        cur.execute(f"UPDATE product_lots SET quant = {ph} WHERE id = {ph}", (quant, lot_id))
        conn.commit()
        return True
    finally:
        conn.close()


def update_lot_price(lot_id: int, price: float) -> bool:
    conn, db_type = get_connection()
    try:
        ph = "%s" if db_type == "postgres" else "?"
        cur = conn.cursor()
        cur.execute(f"UPDATE product_lots SET price = {ph} WHERE id = {ph}", (price, lot_id))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_lot(lot_id: int) -> bool:
    conn, db_type = get_connection()
    try:
        ph = "%s" if db_type == "postgres" else "?"
        cur = conn.cursor()
        cur.execute(f"DELETE FROM product_lots WHERE id = {ph}", (lot_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def create_lot(name: str, quant: int, price: float, date_valid: str, location: str = "LOJA", status: str = "NORMAL"):
    """
    Cria ou encontra o produto pelo nome e cadastra um novo lote.
    Retorna (product_id, lot_id).
    """
    name = name.strip()
    conn, db_type = get_connection()
    try:
        cur = conn.cursor()
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

        # 2. Cria lote
        if db_type == "postgres":
            cur.execute(
                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}) RETURNING id""",
                (product_id, quant, price, date_valid, location, status)
            )
            lot_id = cur.fetchone()[0]
        else:
            cur.execute(
                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
                (product_id, quant, price, date_valid, location, status)
            )
            lot_id = cur.lastrowid

        conn.commit()
        return product_id, lot_id
    finally:
        conn.close()
