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

    # Ler products
    s_cur.execute("SELECT id, name FROM products ORDER BY id ASC")
    products = s_cur.fetchall()
    print(f"   Encontrados {len(products)} produtos no SQLite.")

    # Ler lots
    s_cur.execute("SELECT id, product_id, quant, price, date_valid, location, status FROM product_lots ORDER BY id ASC")
    lots = s_cur.fetchall()
    print(f"   Encontrados {len(lots)} lotes no SQLite.")

    print(f"🔌 Conectando ao PostgreSQL...")
    p_conn = psycopg2.connect(DATABASE_URL)
    p_cur = p_conn.cursor()

    # Criar tabelas
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
            status TEXT DEFAULT 'NORMAL'
        );
    """)
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_valid ON product_lots(date_valid);")
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_product ON product_lots(product_id);")
    p_cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);")

    # Inserir produtos preservando id original
    print("⏳ Migrando produtos...")
    prod_inserted = 0
    for p_id, p_name in products:
        p_cur.execute("""
            INSERT INTO products (id, name)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
        """, (p_id, p_name))
        prod_inserted += 1

    # Atualizar sequencia do Postgres
    p_cur.execute("SELECT setval(pg_get_serial_sequence('products', 'id'), COALESCE(MAX(id), 1)) FROM products;")

    # Inserir lotes preservando id original
    print("⏳ Migrando lotes...")
    lots_inserted = 0
    for l_id, p_id, quant, price, d_valid, loc, st in lots:
        p_cur.execute("""
            INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (l_id, p_id, quant, price, d_valid, loc or 'LOJA', st or 'NORMAL'))
        lots_inserted += 1

    # Atualizar sequencia do Postgres
    p_cur.execute("SELECT setval(pg_get_serial_sequence('product_lots', 'id'), COALESCE(MAX(id), 1)) FROM product_lots;")

    p_conn.commit()
    p_conn.close()
    s_conn.close()

    print(f"✅ Migração concluída com sucesso!")
    print(f"   {prod_inserted} produtos e {lots_inserted} lotes sincronizados no PostgreSQL.")


if __name__ == "__main__":
    migrate()
