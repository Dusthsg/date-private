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
        # 1. Coluna price na tabela products
        cursor.execute("PRAGMA table_info(products);")
        p_cols = [c[1] for c in cursor.fetchall()]
        if "price" not in p_cols:
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
        updated_p = cursor.rowcount
        print(f"  -> SQLite: {updated_p} produtos sincronizados com preço.")

        # 2. Coluna origin na tabela product_lots
        cursor.execute("PRAGMA table_info(product_lots);")
        l_cols = [c[1] for c in cursor.fetchall()]
        if "origin" not in l_cols:
            cursor.execute("ALTER TABLE product_lots ADD COLUMN origin TEXT DEFAULT 'MANUAL';")
            print("  -> Coluna 'origin' adicionada à tabela 'product_lots'.")
        else:
            print("  -> Coluna 'origin' já existia na tabela 'product_lots'.")

        # Classifica lotes de importação vs manuais
        cursor.execute("""
            UPDATE product_lots 
            SET origin = 'IMPORTACAO' 
            WHERE id IN (
                SELECT lot_id FROM stock_movements WHERE action IN ('ENTRADA', 'AJUSTE_DELTA') AND lot_id IS NOT NULL
            ) OR location = 'GALPAO';
        """)
        imported_count = cursor.rowcount
        cursor.execute("UPDATE product_lots SET origin = 'MANUAL' WHERE origin IS NULL;")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_origin ON product_lots(origin);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created_origin ON product_lots(created_at, origin);")

        conn.commit()
        print(f"  -> SQLite: {imported_count} lotes marcados como IMPORTACAO; restantes como MANUAL.")
    except Exception as e:
        conn.rollback()
        print(f"  ❌ Erro no SQLite: {e}")
    finally:
        conn.close()

def migrate_postgres():
    if not DATABASE_URL:
        print("ℹ️ DATABASE_URL não configurada. Pulando migração do PostgreSQL.")
        return

    print("🔌 Conectando ao PostgreSQL para migrar...")
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Coluna price em products
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS price REAL DEFAULT 0.0;")
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
        updated_p = cur.rowcount
        print(f"  -> PostgreSQL: {updated_p} produtos sincronizados com preço.")

        # 2. Coluna origin em product_lots
        cur.execute("ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS origin VARCHAR(20) DEFAULT 'MANUAL';")
        cur.execute("""
            UPDATE product_lots 
            SET origin = 'IMPORTACAO' 
            WHERE (id IN (
                SELECT lot_id FROM stock_movements WHERE action IN ('ENTRADA', 'AJUSTE_DELTA') AND lot_id IS NOT NULL
            ) OR location = 'GALPAO');
        """)
        imported_count = cur.rowcount
        cur.execute("UPDATE product_lots SET origin = 'MANUAL' WHERE origin IS NULL;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_origin ON product_lots(origin);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_product_lots_created_origin ON product_lots(created_at, origin);")

        conn.commit()
        print(f"  -> PostgreSQL: {imported_count} lotes marcados como IMPORTACAO; restantes como MANUAL.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ❌ Erro no PostgreSQL: {e}")

def migrate():
    print("🚀 Executando migração: Produtos e Origem dos Lotes...")
    migrate_sqlite()
    migrate_postgres()
    print("✅ Migração finalizada com sucesso!\n")

if __name__ == "__main__":
    migrate()
