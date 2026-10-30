import sqlite3
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SQLITE_PATH = BASE_DIR / "database" / "products.db"

# Carrega variáveis de ambiente
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env.local")
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

def migrate_sqlite():
    print(f"📂 Migrando SQLite local: {SQLITE_PATH}...")
    conn = sqlite3.connect(str(SQLITE_PATH))
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(products);")
        cols = [c[1] for c in cursor.fetchall()]
        if "price" not in cols:
            cursor.execute("ALTER TABLE products ADD COLUMN price REAL DEFAULT 0.0;")
            print("  -> Coluna 'price' adicionada à tabela 'products'.")
        else:
            print("  -> Coluna 'price' já existia na tabela 'products'.")

        cursor.execute("""
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
        updated = cursor.rowcount
        conn.commit()
        print(f"  -> SQLite: {updated} produtos atualizados com seus preços.")
    except Exception as e:
        conn.rollback()
        print(f"  ❌ Erro no SQLite: {e}")
    finally:
        conn.close()

def migrate_postgres():
    if not DATABASE_URL:
        print("ℹ️ DATABASE_URL não configurada. Pulando migração do PostgreSQL.")
        return

    print("🔌 Conectando ao PostgreSQL para migrar 'price' para 'products'...")
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
        print("  -> Coluna 'price' garantida na tabela 'products' (PostgreSQL).")

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
        updated = cur.rowcount
        conn.commit()
        print(f"  -> PostgreSQL: {updated} produtos atualizados com seus preços.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ❌ Erro no PostgreSQL: {e}")

def migrate():
    print("🚀 Iniciando migração: Alocação de Preço para a tabela Products...")
    migrate_sqlite()
    migrate_postgres()
    print("✅ Migração finalizada com sucesso!\n")

if __name__ == "__main__":
    migrate()
