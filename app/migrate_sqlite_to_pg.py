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

    # Limpa dados antigos para sobreescrever exatamente com o banco mais atualizado
    print("🧹 Limpando dados antigos do PostgreSQL para sobreposição completa...")
    p_cur.execute("TRUNCATE TABLE product_lots, products RESTART IDENTITY CASCADE;")

    # Inserir produtos preservando id original via batch execute_values
    print("⏳ Migrando produtos...")
    from psycopg2.extras import execute_values
    
    execute_values(
        p_cur,
        """
        INSERT INTO products (id, name)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
        """,
        products,
        page_size=500
    )
    prod_inserted = len(products)

    # Atualizar sequencia do Postgres
    p_cur.execute("SELECT setval(pg_get_serial_sequence('products', 'id'), COALESCE(MAX(id), 1)) FROM products;")

    # Inserir lotes preservando id original via batch execute_values
    print("⏳ Migrando lotes...")
    lots_formatted = [
        (l_id, p_id, quant, price, d_valid, loc or 'LOJA', st or 'NORMAL')
        for l_id, p_id, quant, price, d_valid, loc, st in lots
    ]
    execute_values(
        p_cur,
        """
        INSERT INTO product_lots (id, product_id, quant, price, date_valid, location, status)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            product_id = EXCLUDED.product_id,
            quant = EXCLUDED.quant,
            price = EXCLUDED.price,
            date_valid = EXCLUDED.date_valid,
            location = EXCLUDED.location,
            status = EXCLUDED.status
        """,
        lots_formatted,
        page_size=500
    )
    lots_inserted = len(lots_formatted)

    # Atualizar sequencia do Postgres
    p_cur.execute("SELECT setval(pg_get_serial_sequence('product_lots', 'id'), COALESCE(MAX(id), 1)) FROM product_lots;")

    p_conn.commit()
    p_conn.close()
    s_conn.close()

    print(f"✅ Migração concluída com sucesso!")
    print(f"   {prod_inserted} produtos e {lots_inserted} lotes sincronizados no PostgreSQL.")


if __name__ == "__main__":
    migrate()
