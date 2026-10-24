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


PROD_CANDIDATES = [
    "Produto", "Descricao", "Nome", "Item", "Mercadoria", "Desc",
    "Descricao do Produto", "Nome do Produto", "Prod", "Produto Descricao",
    "Descricao Reduzida", "Descricao_Produto", "Desc_Produto", "Dsc_Produto",
    "Produto/Descricao", "Descricao Produto"
]

VALID_CANDIDATES = [
    "Validade", "Vencimento", "Data Validade", "Dt Validade", "Data",
    "Vencto", "Dt Vencto", "Dt_Vencto", "Dt_Validade", "Data Vencimento",
    "Data_Vencimento", "Dt.Validade", "Dt.Vencimento", "Val", "Data_Val",
    "Data_Vencto", "Dt. Vencto"
]

CODE_CANDIDATES = [
    "Codigo", "Cod", "EAN", "Codigo Barras", "Bar_code", "Cod Produto",
    "Cod_Produto", "Cod Barra", "Cod_Barra", "CodBar", "GTIN",
    "Codigo de Barras", "Plu", "Cod.Barra", "Cod_Barras"
]

QUANT_CANDIDATES = [
    "Quantidade", "Qtd", "Qtde", "Estoque", "Saldo", "Fisico", "Quant",
    "Qtd Estoque", "Saldo Atual", "Qtd_Estoque", "Qtd_Loja", "Loja",
    "Galpao", "Qtd_Fisica"
]


def read_spreadsheet_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """
    Lê com tolerância máxima arquivos .xlsx, .xls legados, CSVs e exportações de ERP
    (que frequentemente exportam tabelas HTML ou CSV com extensão .xls).
    """
    filename_lower = filename.lower()
    file_io = io.BytesIO(file_bytes)

    # 1. Se o arquivo terminar com .csv
    if filename_lower.endswith(".csv"):
        for sep in [";", ",", "\t", "|"]:
            for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
                try:
                    file_io.seek(0)
                    df = pd.read_csv(file_io, sep=sep, encoding=enc)
                    if len(df.columns) > 1 and len(df) > 0:
                        return df
                except Exception:
                    pass
        file_io.seek(0)
        return pd.read_csv(file_io, sep=None, engine="python", encoding="latin1")

    # 2. Arquivos Excel (.xlsx ou .xls)
    last_error = None

    # Tentativa A: leitura padrão do Pandas
    try:
        file_io.seek(0)
        return pd.read_excel(file_io)
    except Exception as e:
        last_error = e

    # Tentativa B: se for .xls antigo, força a engine xlrd
    if filename_lower.endswith(".xls"):
        try:
            file_io.seek(0)
            import xlrd
            return pd.read_excel(file_io, engine="xlrd")
        except Exception as e:
            last_error = e

    # Tentativa C: Tabela HTML salva como .xls (muito comum em ERPs de supermercado)
    try:
        file_io.seek(0)
        tables = pd.read_html(file_io)
        if tables and len(tables[0]) > 0:
            return tables[0]
    except Exception:
        pass

    # Tentativa D: Texto delimitado / CSV salvo com extensão .xls
    for sep in [";", ",", "\t"]:
        for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
            try:
                file_io.seek(0)
                df = pd.read_csv(file_io, sep=sep, encoding=enc)
                if len(df.columns) > 1 and len(df) > 0:
                    return df
            except Exception:
                pass

    raise ValueError(
        f"Não foi possível abrir o arquivo '{filename}'. "
        f"Caso seja um .xls antigo, você pode salvá-lo como Pasta de Trabalho do Excel (.xlsx) ou CSV. "
        f"(Detalhe técnico: {last_error})"
    )


def audit_and_import_spreadsheet_stream(file_bytes: bytes, filename: str):
    """
    Gerador que executa a conciliação linha por linha e emite atualizações
    de progresso em tempo real (0% a 100%) para cada linha processada.
    """
    yield {
        "type": "progress",
        "percent": 5,
        "stage": f"Lendo e descompactando '{filename}'...",
        "current": 0,
        "total": 0,
    }

    try:
        df = read_spreadsheet_dataframe(file_bytes, filename)
    except Exception as e:
        yield {"type": "error", "error": f"Erro ao ler arquivo: {str(e)}"}
        return

    if df.empty:
        yield {"type": "error", "error": "A planilha enviada está vazia."}
        return

    yield {
        "type": "progress",
        "percent": 10,
        "stage": "Identificando colunas de produto e validade...",
        "current": 0,
        "total": len(df),
    }

    # 2. Identificação de colunas essenciais
    col_prod = find_column(df, PROD_CANDIDATES)
    col_valid = find_column(df, VALID_CANDIDATES)

    # Se não localizou, tenta detectar se o cabeçalho real está em alguma linha abaixo
    if not col_prod or not col_valid:
        for i in range(min(15, len(df))):
            row_str = " ".join([normalize_col_name(v) for v in df.iloc[i].dropna().astype(str)])
            has_p = any(c in row_str for c in ["PRODUT", "DESCRIC", "NOME", "MERCAD", "ITEM"])
            has_v = any(c in row_str for c in ["VALID", "VENCT", "VENCIM", "DATA"])
            if has_p and has_v:
                df.columns = df.iloc[i].astype(str).str.strip()
                df = df.iloc[i + 1:].reset_index(drop=True)
                col_prod = find_column(df, PROD_CANDIDATES)
                col_valid = find_column(df, VALID_CANDIDATES)
                break

    col_code = find_column(df, CODE_CANDIDATES)
    col_quant = find_column(df, QUANT_CANDIDATES)

    if not col_prod:
        cols_found = ", ".join([str(c) for c in df.columns[:8]])
        yield {"type": "error", "error": f"Coluna de Produto/Descrição não identificada. Colunas encontradas: [{cols_found}]"}
        return
    if not col_valid:
        cols_found = ", ".join([str(c) for c in df.columns[:8]])
        yield {"type": "error", "error": f"Coluna de Validade/Vencimento não identificada. Colunas encontradas: [{cols_found}]"}
        return

    # 3. Limpeza inicial de linhas nulas
    df_clean = df.dropna(subset=[col_prod, col_valid]).copy()
    if df_clean.empty:
        yield {"type": "error", "error": "Nenhuma linha com Produto e Validade válidos foi encontrada."}
        return

    total_rows = len(df_clean)
    stats = {
        "total_linhas": total_rows,
        "novos_lotes": 0,
        "ajustes_delta": 0,
        "bloqueados_zumbi": 0,
        "inalterados": 0,
        "erros": 0,
    }
    audit_logs = []

    yield {
        "type": "progress",
        "percent": 12,
        "stage": f"Estrutura validada! Conciliando {total_rows} linhas no banco de dados...",
        "current": 0,
        "total": total_rows,
        "stats": dict(stats),
    }

    try:
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = "%s" if db_type == "postgres" else "?"

            for idx, (_, row) in enumerate(df_clean.iterrows(), start=1):
                raw_prod = str(row[col_prod]).strip()
                raw_valid = row[col_valid]
                raw_code = str(row[col_code]).strip() if col_code and pd.notna(row[col_code]) else None
                
                # Limpa código de barras removendo decimais (ex: 7000646.0 -> 7000646)
                if raw_code and raw_code.endswith(".0"):
                    raw_code = raw_code[:-2]
                if raw_code == "nan" or raw_code == "":
                    raw_code = None

                # Converte data de validade com segurança (suporte nativo ao padrão brasileiro DD/MM/AAAA)
                try:
                    date_parsed = pd.to_datetime(raw_valid, errors="coerce", dayfirst=True)
                    if pd.isna(date_parsed):
                        date_parsed = pd.to_datetime(raw_valid, errors="coerce", dayfirst=False)
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
                        if raw_code:
                            cur.execute(f"UPDATE products SET bar_code = {ph} WHERE id = {ph}", (raw_code, product_id))
                    else:
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
                else:
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
                        total_local_quant = sum((r[1] or 0) for r in lotes_locais)

                        # ── Pergunta 3: A quantidade mudou? ───────────────────────────
                        if sheet_quant > total_local_quant:
                            delta = sheet_quant - total_local_quant
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
                            stats["inalterados"] += 1

                # Emissão de progresso proporcional (12% a 95%)
                percent = int(12 + (idx / total_rows) * 83)
                if total_rows <= 150 or idx % max(1, total_rows // 80) == 0 or idx == total_rows:
                    yield {
                        "type": "progress",
                        "percent": percent,
                        "stage": f"Linha {idx} de {total_rows}: {raw_prod[:24]}",
                        "current": idx,
                        "total": total_rows,
                        "stats": dict(stats),
                    }

        clear_query_cache()

        yield {
            "type": "progress",
            "percent": 100,
            "stage": "Finalizando conciliação e atualizando estoque...",
            "current": total_rows,
            "total": total_rows,
            "stats": dict(stats),
        }

        yield {
            "type": "done",
            "percent": 100,
            "stats": stats,
            "logs": audit_logs[:30],
        }

    except Exception as exc:
        yield {"type": "error", "error": f"Erro durante a gravação no banco: {str(exc)}"}


def audit_and_import_spreadsheet(file_bytes: bytes, filename: str) -> dict:
    """Compatibilidade direta para chamadas síncronas."""
    last_res = None
    for item in audit_and_import_spreadsheet_stream(file_bytes, filename):
        if item.get("type") == "done":
            last_res = item
        elif item.get("type") == "error":
            raise ValueError(item.get("error"))
    if not last_res:
        raise ValueError("Auditoria não retornou resultados.")
    return {"stats": last_res["stats"], "logs": last_res["logs"]}
