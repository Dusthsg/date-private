import sys
from pathlib import Path

DIR_ATUAL = Path(__file__).resolve().parent
RAIZ_PROJETO = DIR_ATUAL.parent
sys.path.append(str(RAIZ_PROJETO))

import pandas as pd
from database import conn, cursor

EXCEL_PATH = DIR_ATUAL / "lista.xlsx"

# 1. Leitura do Excel
df = pd.read_excel(EXCEL_PATH)
df_filt = df[["Validade", "Produto"]].copy().dropna(subset=["Produto", "Validade"])
df_filt["date_valid"] = pd.to_datetime(df_filt["Validade"]).dt.strftime('%Y-%m-%d')
df_filt["name"] = df_filt["Produto"].str.strip()

# 2. Processamento Linha por Linha com Transação
for _, row in df_filt.iterrows():
    p_name = row["name"]
    p_valid = row["date_valid"]

    # Passo A: Insere o produto na tabela 'products' se não existir
    cursor.execute("INSERT OR IGNORE INTO products (name) VALUES (?);", (p_name,))
    
    # Passo B: Recupera o ID do produto
    cursor.execute("SELECT id FROM products WHERE name = ?;", (p_name,))
    product_id = cursor.fetchone()[0]

    # Passo C: Evita duplicar exatamente o MESMO LOTE (mesmo produto + mesma data)
    cursor.execute(
        "SELECT id FROM product_lots WHERE product_id = ? AND date_valid = ?;",
        (product_id, p_valid)
    )
    existe_lote = cursor.fetchone()

    if not existe_lote:
        cursor.execute(
            "INSERT INTO product_lots (product_id, quant, price, date_valid) VALUES (?, ?, ?, ?);",
            (product_id, 0, 0.0, p_valid)
        )

conn.commit()
print("-> Importação relacional de lotes concluída com sucesso!")