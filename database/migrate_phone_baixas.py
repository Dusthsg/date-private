"""
Script de Migração e Sincronização de Baixas do Histórico (stock_movements)
Importa registros de BAIXA de database/products_my_phone.db para SQLite e PostgreSQL.
"""
import os
import sys
import sqlite3
import psycopg2
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import SQLITE_PATH, DATABASE_URL, clear_query_cache
PHONE_DB = BASE_DIR / "database" / "products_my_phone.db"
if not PHONE_DB.exists():
    PHONE_DB = BASE_DIR / "database" / "products_my_phone.db.bak"

def migrate_baixas():
    if not PHONE_DB.exists():
        print(f"❌ Arquivo de banco do celular não encontrado: {PHONE_DB}")
        return 0

    print(f"📂 Lendo baixas de: {PHONE_DB}")
    phone_conn = sqlite3.connect(str(PHONE_DB))
    phone_cur = phone_conn.cursor()

    phone_cur.execute("""
        SELECT sm.lot_id, sm.action, sm.quant, sm.date_valid, sm.created_at, p.name
        FROM stock_movements sm
        LEFT JOIN products p ON sm.product_id = p.id
        WHERE sm.action = 'BAIXA'
        ORDER BY sm.id ASC
    """)
    records = phone_cur.fetchall()
    print(f"🔍 Total de registros de BAIXA encontrados no banco do celular: {len(records)}")

    # Conexão SQLite principal
    sq_conn = sqlite3.connect(str(SQLITE_PATH), timeout=10.0)
    sq_conn.execute("PRAGMA journal_mode = WAL;")
    sq_cur = sq_conn.cursor()

    # Conexão PostgreSQL (se configurado)
    pg_conn = None
    pg_cur = None
    if DATABASE_URL:
        try:
            pg_conn = psycopg2.connect(DATABASE_URL)
            pg_cur = pg_conn.cursor()
            print("🌐 PostgreSQL conectado.")
        except Exception as e:
            print(f"⚠️ Aviso: Não foi possível conectar ao Postgres: {e}")

    inserted_count = 0
    skipped_count = 0

    for lot_id, action, quant, date_valid, created_at, name in records:
        if not name:
            continue
        clean_name = name.strip().upper()

        # 1. Obter ou criar product_id no SQLite
        sq_cur.execute("SELECT id FROM products WHERE UPPER(name) = ?", (clean_name,))
        row = sq_cur.fetchone()
        if row:
            sq_p_id = row[0]
        else:
            sq_cur.execute("INSERT INTO products (name, price) VALUES (?, 0.0)", (clean_name,))
            sq_p_id = sq_cur.lastrowid

        # 2. Obter ou criar product_id no Postgres
        pg_p_id = sq_p_id
        if pg_cur:
            pg_cur.execute("SELECT id FROM products WHERE UPPER(name) = %s", (clean_name,))
            pg_row = pg_cur.fetchone()
            if pg_row:
                pg_p_id = pg_row[0]
            else:
                pg_cur.execute("INSERT INTO products (name, price) VALUES (%s, 0.0) RETURNING id", (clean_name,))
                pg_p_id = pg_cur.fetchone()[0]

        # 3. Verifica duplicidade no SQLite
        sq_cur.execute(
            "SELECT id FROM stock_movements WHERE product_id = ? AND date_valid = ? AND action = 'BAIXA' AND created_at = ?",
            (sq_p_id, str(date_valid)[:10], str(created_at))
        )
        if sq_cur.fetchone():
            skipped_count += 1
            continue

        # 4. Insere no SQLite
        sq_cur.execute(
            """INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sq_p_id, lot_id, 'BAIXA', quant or 0, str(date_valid)[:10], str(created_at))
        )

        # 5. Insere no PostgreSQL
        if pg_cur:
            pg_cur.execute(
                """INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (pg_p_id, lot_id, 'BAIXA', quant or 0, str(date_valid)[:10], str(created_at))
            )

        inserted_count += 1

    sq_conn.commit()
    sq_conn.close()

    if pg_conn:
        pg_conn.commit()
        pg_conn.close()

    clear_query_cache()

    print(f"✅ Migração concluída: {inserted_count} baixas inseridas com sucesso ({skipped_count} duplicatas ignoradas).")
    return inserted_count

if __name__ == "__main__":
    migrate_baixas()
