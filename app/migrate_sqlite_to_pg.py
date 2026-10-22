"""
Script para migrar todos os dados do SQLite local (products.db) para o PostgreSQL.

Uso:
    $env:DATABASE_URL = "postgresql://usuario:senha@host:5432/nome_banco"
    python -m app.migrate_sqlite_to_pg
"""

import os
import sqlite3
import psycopg2
from pathlib import Path

# Carrega .env ou .env.local se existir
try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BASE_DIR / ".env.local")
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent.parent

SQLITE_PATH = BASE_DIR / "database" / "products.db"
DATABASE_URL = os.environ.get("DATABASE_URL")

def migrate():
    if not DATABASE_URL:
        print("❌ ERRO: A variável de ambiente DATABASE_URL não está configurada!")
        print("Defina DATABASE_URL com a URL do seu PostgreSQL:")
        print("  $env:DATABASE_URL = 'postgresql://user:pass@host:5432/dbname'")
        return

    if not SQLITE_PATH.exists():
        print(f"❌ ERRO: Banco SQLite não encontrado em {SQLITE_PATH}")
        return

    print(f"📂 Lendo dados de {SQLITE_PATH}...")
    s_conn = sqlite3.connect(str(SQLITE_PATH))
    s_cur = s_conn.cursor()

    # Ler products (com bar_code se existir)
    s_cur.execute("PRAGMA table_info(products);")
    p_cols = [c[1] for c in s_cur.fetchall()]
    if "bar_code" in p_cols:
        s_cur.execute("SELECT id, name, bar_code FROM products ORDER BY id ASC")
        products = s_cur.fetchall()
    else:
        s_cur.execute("SELECT id, name, NULL FROM products ORDER BY id ASC")
        products = s_cur.fetchall()
    print(f"   Encontrados {len(products)} produtos no SQLite.")

    # Ler lots (com created_at e updated_at se existirem)
    s_cur.execute("PRAGMA table_info(product_lots);")
    l_cols = [c[1] for c in s_cur.fetchall()]
    has_created = "created_at" in l_cols
    has_updated = "updated_at" in l_cols
    has_price = "price" in l_cols

    s_cur.execute("SELECT id, product_id, quant, date_valid, location, status FROM product_lots ORDER BY id ASC")
    raw_lots = s_cur.fetchall()

    lots_formatted = []
    for row in raw_lots:
        l_id, p_id, quant, d_valid, loc, st = row
        price = 0.0
        c_at = None
        u_at = None
        if has_price:
            try:
                s_cur.execute("SELECT price FROM product_lots WHERE id = ?", (l_id,))
                price = s_cur.fetchone()[0] or 0.0
            except Exception:
                price = 0.0
        if has_created:
            try:
                s_cur.execute("SELECT created_at FROM product_lots WHERE id = ?", (l_id,))
                c_at = s_cur.fetchone()[0]
            except Exception:
                c_at = None
        if has_updated:
            try:
                s_cur.execute("SELECT updated_at FROM product_lots WHERE id = ?", (l_id,))
                u_at = s_cur.fetchone()[0]
            except Exception:
                u_at = None
        lots_formatted.append((l_id, p_id, quant, price, str(d_valid)[:10], loc or 'LOJA', st or 'NORMAL', c_at, u_at))
    print(f"   Encontrados {len(lots_formatted)} lotes no SQLite.")

    print(f"🔌 Conectando ao PostgreSQL...")
    p_conn = psycopg2.connect(DATABASE_URL)
    p_cur = p_conn.cursor()

    # Criar tabelas e colunas
    p_cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            bar_code TEXT UNIQUE
        );
    """)
    p_cur.execute("""
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
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

    # Limpa dados antigos para sobreescrever exatamente com o banco mais atualizado do celular
    print("🧹 Atualizando PostgreSQL com os dados mais recentes do SQLite...")
    p_cur.execute("TRUNCATE TABLE product_lots, products RESTART IDENTITY CASCADE;")

    # Inserir produtos preservando id original
    print("⏳ Migrando produtos...")
    from psycopg2.extras import execute_values
    
    execute_values(
        p_cur,
        """
        INSERT INTO products (id, name, bar_code)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, bar_code = EXCLUDED.bar_code
        """,
        products,
        page_size=500
    )
    prod_inserted = len(products)

    # Inserir lotes
    print("⏳ Migrando lotes...")
    lots_payload = [
        (l[0], l[1], l[2], l[3], l[4], l[5], l[6], l[7] or '2026-10-22')
        for l in lots_formatted
    ]
    execute_values(
        p_cur,
        """
        INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status, created_at)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            product_id = EXCLUDED.product_id,
            quant = EXCLUDED.quant,
            price = EXCLUDED.price,
            date_valid = EXCLUDED.date_valid,
            location = EXCLUDED.location,
            status = EXCLUDED.status,
            created_at = EXCLUDED.created_at
        """,
        lots_payload,
        page_size=500
    )
    lots_inserted = len(lots_payload)

    # Migrar stock_movements se existir
    s_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_movements';")
    if s_cur.fetchone():
        s_cur.execute("SELECT id, product_id, lot_id, action, quant, date_valid, created_at FROM stock_movements;")
        moves = s_cur.fetchall()
        if moves:
            p_cur.execute("""
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
            execute_values(
                p_cur,
                """
                INSERT INTO stock_movements (id, product_id, lot_id, action, quant, date_valid, created_at)
                VALUES %s
                ON CONFLICT (id) DO NOTHING
                """,
                moves,
                page_size=500
            )

    # Atualizar sequencias do Postgres
    p_cur.execute("SELECT setval(pg_get_serial_sequence('products', 'id'), COALESCE(MAX(id), 1)) FROM products;")
    p_cur.execute("SELECT setval(pg_get_serial_sequence('product_lots', 'id'), COALESCE(MAX(id), 1)) FROM product_lots;")

    p_conn.commit()
    p_conn.close()
    s_conn.close()

    print(f"✅ Migração concluída com sucesso!")
    print(f"   {prod_inserted} produtos e {lots_inserted} lotes sincronizados no PostgreSQL.")


if __name__ == "__main__":
    migrate()
