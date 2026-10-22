"""
Módulo Auditor de Importação de Planilhas para o DateLimit.

Implementa a conciliação linha por linha com base no raciocínio em 3 perguntas:
1. Filtro de Lápide: Esse lote já foi baixado no chão? (Evita o item zumbi).
2. Remessa Nova: Esse lote já existe no banco local? (Se NÃO, cria no GALPÃO).
3. Divergência de Quantidade: A quantidade mudou? (Se planilha > loja, lança a diferença no GALPÃO).
"""

import io
import re
from datetime import datetime, date
import pandas as pd

from app.db import get_db_cursor, clear_query_cache


def normalize_col_name(col: str) -> str:
    """Normaliza nome de coluna para identificação insensível a maiúsculas e acentos."""
    s = str(col).strip().upper()
    s = s.replace("Ã", "A").replace("Á", "A").replace("Â", "A")
    s = s.replace("É", "E").replace("Ê", "E")
    s = s.replace("Í", "I")
    s = s.replace("Ó", "O").replace("Ô", "O").replace("Õ", "O")
    s = s.replace("Ú", "U")
    s = s.replace("Ç", "C")
    return s


def find_column(df, candidates):
    """Localiza a primeira coluna do dataframe que combina com a lista de candidatos."""
    norm_cols = {normalize_col_name(c): c for c in df.columns}
    for cand in candidates:
        cand_norm = normalize_col_name(cand)
        if cand_norm in norm_cols:
            return norm_cols[cand_norm]
    return None


def audit_and_import_spreadsheet(file_bytes: bytes, filename: str) -> dict:
    """
    Executa a conciliação linha por linha da planilha enviada.
    Retorna estatísticas detalhadas e log dos itens auditados.
    """
    filename_lower = filename.lower()
    file_io = io.BytesIO(file_bytes)

    # 1. Leitura do arquivo (Excel ou CSV)
    try:
        if filename_lower.endswith(".csv"):
            try:
                df = pd.read_csv(file_io, sep=";", encoding="utf-8")
                if len(df.columns) <= 1:
                    file_io.seek(0)
                    df = pd.read_csv(file_io, sep=",", encoding="utf-8")
            except UnicodeDecodeError:
                file_io.seek(0)
                df = pd.read_csv(file_io, sep=";", encoding="latin1")
        else:
            df = pd.read_excel(file_io)
    except Exception as e:
        raise ValueError(f"Não foi possível ler o arquivo de planilha: {str(e)}")

    if df.empty:
        raise ValueError("A planilha enviada está vazia.")

    # 2. Identificação de colunas essenciais
    col_prod = find_column(df, ["Produto", "Descricao", "Nome", "Item", "Descricao do Produto"])
    col_valid = find_column(df, ["Validade", "Vencimento", "Data Validade", "Dt Validade", "Data"])
    col_code = find_column(df, ["Codigo", "Cod", "EAN", "Codigo Barras", "Bar_code", "Cod Produto"])
    col_quant = find_column(df, ["Quantidade", "Qtd", "Estoque", "Saldo", "Fisico", "Quant"])

    if not col_prod:
        raise ValueError("Coluna de Produto/Descrição não encontrada na planilha. Verifique o cabeçalho.")
    if not col_valid:
        raise ValueError("Coluna de Validade/Vencimento não encontrada na planilha. Verifique o cabeçalho.")

    # 3. Limpeza inicial de linhas nulas
    df_clean = df.dropna(subset=[col_prod, col_valid]).copy()
    if df_clean.empty:
        raise ValueError("Nenhuma linha com Produto e Validade válidos foi encontrada.")

    stats = {
        "total_linhas": len(df_clean),
        "novos_lotes": 0,
        "ajustes_delta": 0,
        "bloqueados_zumbi": 0,
        "inalterados": 0,
        "erros": 0,
    }
    audit_logs = []

    # 4. Auditoria Linha por Linha no Banco
    with get_db_cursor(commit=True) as (cur, db_type):
        ph = "%s" if db_type == "postgres" else "?"

        for _, row in df_clean.iterrows():
            raw_prod = str(row[col_prod]).strip()
            raw_valid = row[col_valid]
            raw_code = str(row[col_code]).strip() if col_code and pd.notna(row[col_code]) else None
            
            # Limpa código de barras removendo decimais (ex: 7000646.0 -> 7000646)
            if raw_code and raw_code.endswith(".0"):
                raw_code = raw_code[:-2]
            if raw_code == "nan" or raw_code == "":
                raw_code = None

            # Converte data de validade com segurança
            try:
                date_parsed = pd.to_datetime(raw_valid, errors="coerce")
                if pd.isna(date_parsed):
                    stats["erros"] += 1
                    continue
                valid_str = date_parsed.strftime("%Y-%m-%d")
            except Exception:
                stats["erros"] += 1
                continue

            # Quantidade da planilha (default 0)
            sheet_quant = 0
            if col_quant and pd.notna(row[col_quant]):
                try:
                    sheet_quant = max(0, int(round(float(row[col_quant]))))
                except (ValueError, TypeError):
                    sheet_quant = 0

            # ── Passo A: Localiza ou Cadastra o Produto ────────────────────────
            product_id = None
            if raw_code:
                cur.execute(f"SELECT id, name FROM products WHERE bar_code = {ph} LIMIT 1", (raw_code,))
                p_match = cur.fetchone()
                if p_match:
                    product_id = p_match[0]

            if not product_id:
                cur.execute(f"SELECT id FROM products WHERE name = {ph} LIMIT 1", (raw_prod,))
                p_match = cur.fetchone()
                if p_match:
                    product_id = p_match[0]
                    # Atualiza código se antes estava nulo
                    if raw_code:
                        cur.execute(f"UPDATE products SET bar_code = {ph} WHERE id = {ph}", (raw_code, product_id))
                else:
                    # Cadastra novo produto
                    if db_type == "postgres":
                        cur.execute(
                            f"INSERT INTO products (name, bar_code) VALUES ({ph}, {ph}) RETURNING id",
                            (raw_prod, raw_code)
                        )
                        product_id = cur.fetchone()[0]
                    else:
                        cur.execute(
                            f"INSERT INTO products (name, bar_code) VALUES ({ph}, {ph})",
                            (raw_prod, raw_code)
                        )
                        product_id = cur.lastrowid

            # ── Pergunta 1: Esse lote já foi baixado no chão? (Filtro de Lápide) ─
            # Checa se há registro de BAIXA em stock_movements para este produto e validade
            cur.execute(
                f"""SELECT id, created_at FROM stock_movements 
                    WHERE product_id = {ph} AND date_valid = {ph} AND action = 'BAIXA' 
                    LIMIT 1""",
                (product_id, valid_str)
            )
            foi_baixado = cur.fetchone()
            if foi_baixado:
                stats["bloqueados_zumbi"] += 1
                audit_logs.append({
                    "produto": raw_prod,
                    "validade": valid_str,
                    "tipo": "BLOQUEADO",
                    "detalhe": "Item Zumbi bloqueado: já havia sido baixado no chão da loja."
                })
                continue  # Ignora a linha da planilha!

            # ── Pergunta 2: Esse lote já existe no meu banco local? ────────────
            cur.execute(
                f"""SELECT id, quant, location FROM product_lots 
                    WHERE product_id = {ph} AND date_valid = {ph} 
                    ORDER BY id ASC""",
                (product_id, valid_str)
            )
            lotes_locais = cur.fetchall()

            if not lotes_locais:
                # NÃO EXISTE -> Remessa Nova (Entra direto no GALPÃO)
                if db_type == "postgres":
                    cur.execute(
                        f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at, updated_at)
                            VALUES ({ph}, {ph}, 0.0, {ph}, 'GALPAO', 'NORMAL', CURRENT_DATE, CURRENT_TIMESTAMP)
                            RETURNING id""",
                        (product_id, sheet_quant, valid_str)
                    )
                    novo_id = cur.fetchone()[0]
                else:
                    cur.execute(
                        f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at, updated_at)
                            VALUES ({ph}, {ph}, 0.0, {ph}, 'GALPAO', 'NORMAL', date('now'), datetime('now', 'localtime'))""",
                        (product_id, sheet_quant, valid_str)
                    )
                    novo_id = cur.lastrowid

                # Registra em stock_movements
                cur.execute(
                    f"""INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid)
                        VALUES ({ph}, {ph}, 'ENTRADA', {ph}, {ph})""",
                    (product_id, novo_id, sheet_quant, valid_str)
                )

                stats["novos_lotes"] += 1
                audit_logs.append({
                    "produto": raw_prod,
                    "validade": valid_str,
                    "tipo": "NOVO_GALPAO",
                    "detalhe": f"Remessa nova inserida no GALPÃO ({sheet_quant} un)."
                })

            else:
                # JÁ EXISTE NO BANCO LOCAL
                # Calcula total local contado fisicamente
                total_local_quant = sum(r[1] for r in lotes_locais)

                # ── Pergunta 3: A quantidade mudou? ───────────────────────────
                if sheet_quant > total_local_quant:
                    # Divergência detectada: planilha tem excedente
                    delta = sheet_quant - total_local_quant

                    # Verifica se já existe lote deste produto no GALPAO para somar o delta
                    lote_galpao = next((r for r in lotes_locais if str(r[2]).upper() == "GALPAO"), None)

                    if lote_galpao:
                        galpao_id = lote_galpao[0]
                        now_val = "CURRENT_TIMESTAMP" if db_type == "postgres" else "datetime('now', 'localtime')"
                        cur.execute(
                            f"UPDATE product_lots SET quant = quant + {ph}, updated_at = {now_val} WHERE id = {ph}",
                            (delta, galpao_id)
                        )
                        target_lot_id = galpao_id
                    else:
                        # Cria lote excedente de reforço no GALPÃO
                        if db_type == "postgres":
                            cur.execute(
                                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at, updated_at)
                                    VALUES ({ph}, {ph}, 0.0, {ph}, 'GALPAO', 'NORMAL', CURRENT_DATE, CURRENT_TIMESTAMP)
                                    RETURNING id""",
                                (product_id, delta, valid_str)
                            )
                            target_lot_id = cur.fetchone()[0]
                        else:
                            cur.execute(
                                f"""INSERT INTO product_lots (product_id, quant, price, date_valid, location, status, created_at, updated_at)
                                    VALUES ({ph}, {ph}, 0.0, {ph}, 'GALPAO', 'NORMAL', date('now'), datetime('now', 'localtime'))""",
                                (product_id, delta, valid_str)
                            )
                            target_lot_id = cur.lastrowid

                    # Registra ajuste delta em stock_movements
                    cur.execute(
                        f"""INSERT INTO stock_movements (product_id, lot_id, action, quant, date_valid)
                            VALUES ({ph}, {ph}, 'AJUSTE_DELTA', {ph}, {ph})""",
                        (product_id, target_lot_id, delta, valid_str)
                    )

                    stats["ajustes_delta"] += 1
                    audit_logs.append({
                        "produto": raw_prod,
                        "validade": valid_str,
                        "tipo": "AJUSTE_DELTA",
                        "detalhe": f"Divergência (Planilha {sheet_quant} > Loja {total_local_quant}): reforço de +{delta} un no GALPÃO."
                    })

                else:
                    # Quantidade igual ou planilha não tem excedente -> Mantém chão da loja intacto
                    stats["inalterados"] += 1

    # Invalida cache de leitura para que as telas mostrem os dados novos instantaneamente
    clear_query_cache()

    return {
        "stats": stats,
        "logs": audit_logs[:30]  # Retorna amostra das ações principais para a interface
    }
