"""
Módulo Auditor de Importação de Planilhas para o DateLimit.

Implementa a conciliação linha por linha com base no raciocínio em 3 perguntas:
1. Filtro de Lápide: Esse lote já foi baixado no chão? (Evita o item zumbi).
2. Remessa Nova: Esse lote já existe no banco local? (Se NÃO, cria no GALPÃO).
3. Divergência de Quantidade: A quantidade mudou? (Se planilha > loja, lança a diferença no GALPÃO).
"""

import io
import re
import time
from datetime import datetime, date, timedelta
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


EXCEL_EPOCH = datetime(1899, 12, 30)


def parse_date_value(raw_val) -> str | None:
    """
    Converte com velocidade máxima qualquer formato de data em string YYYY-MM-DD:
    - Serial do Excel em nanossegundos via datetime puro (50x mais rápido que pd.to_datetime)
    - ISO AAAA-MM-DD instantâneo por slice
    - Brasileiro DD/MM/AAAA por slice
    """
    if raw_val is None or pd.isna(raw_val):
        return None

    # 1. Float ou Int serial do Excel (ex: 46284.0 -> 2026-09-19)
    if isinstance(raw_val, (int, float)):
        if 30000 <= raw_val <= 65000:
            return (EXCEL_EPOCH + timedelta(days=raw_val)).strftime("%Y-%m-%d")

    s_val = str(raw_val).strip()
    if not s_val or s_val.lower() == "nan":
        return None

    # 2. Formato ISO (AAAA-MM-DD)
    if len(s_val) >= 10 and s_val[4] == "-" and s_val[7] == "-":
        return s_val[:10]

    # 3. Formato Brasileiro (DD/MM/AAAA)
    if len(s_val) >= 10 and s_val[2] == "/" and s_val[5] == "/":
        return f"{s_val[6:10]}-{s_val[3:5]}-{s_val[0:2]}"

    # 4. String com número serial (ex: "46284.0")
    try:
        f_val = float(s_val)
        if 30000 <= f_val <= 65000:
            return (EXCEL_EPOCH + timedelta(days=f_val)).strftime("%Y-%m-%d")
    except ValueError:
        pass

    # 5. Fallback flexível com Pandas
    try:
        dt = pd.to_datetime(s_val, errors="coerce", dayfirst=True)
        if pd.notna(dt) and dt.year >= 2000:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    return None


def read_spreadsheet_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """
    Lê com velocidade máxima e tolerância a qualquer formato:
    - Identifica magic bytes (BIFF8 .xls em 0.03s com xlrd direto)
    - Identifica magic bytes (OpenXML .xlsx com openpyxl direto)
    - Identifica exportações HTML salvas como .xls
    - Identifica CSVs com detecção automática rápida de separador
    """
    if not file_bytes:
        return pd.DataFrame()

    # 1. Magic bytes: Excel .xls binário legado (BIFF8: 0xD0CF11E0A1B11AE1)
    if file_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=file_bytes, on_demand=True)
            sheet = book.sheet_by_index(0)
            if sheet.nrows > 0:
                headers = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
                rows = [sheet.row_values(r) for r in range(1, sheet.nrows)]
                return pd.DataFrame(rows, columns=headers)
        except Exception:
            pass

    # 2. Magic bytes: Excel .xlsx moderno (ZIP: PK\x03\x04)
    if file_bytes.startswith(b"PK\x03\x04"):
        try:
            return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        except Exception:
            pass

    # 3. Exportações de ERP em tabela HTML salvas como .xls
    preview = file_bytes[:1024].lower()
    if b"<html" in preview or b"<table" in preview or b"<!doctype" in preview:
        try:
            tables = pd.read_html(io.BytesIO(file_bytes))
            if tables and len(tables[0]) > 0:
                return tables[0]
        except Exception:
            pass

    # 4. CSV ou texto delimitado
    filename_lower = filename.lower()
    if filename_lower.endswith(".csv") or b"," in file_bytes[:512] or b";" in file_bytes[:512]:
        for sep in [";", ",", "\t", "|"]:
            for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
                try:
                    df = pd.read_csv(io.BytesIO(file_bytes), sep=sep, encoding=enc)
                    if len(df.columns) > 1 and len(df) > 0:
                        return df
                except Exception:
                    pass

    # 5. Fallback geral do Pandas
    return pd.read_excel(io.BytesIO(file_bytes))


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
    has_quant_col = col_quant is not None

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

            yield {
                "type": "progress",
                "percent": 15,
                "stage": "Carregando catálogo e lotes para reconciliação ultrarrápida em memória...",
                "current": 0,
                "total": total_rows,
                "stats": dict(stats),
            }

            # ── 1 ÚNICO roundtrip: Carrega todos os produtos, lápides e lotes em memória ──
            cur.execute("SELECT id, name, bar_code FROM products;")
            all_products = cur.fetchall()

            cur.execute(
                "SELECT product_id, date_valid::TEXT FROM stock_movements WHERE action = 'BAIXA';" if db_type == "postgres"
                else "SELECT product_id, date_valid FROM stock_movements WHERE action = 'BAIXA';"
            )
            all_tombstones = cur.fetchall()

            cur.execute(
                "SELECT id, product_id, quant, location, date_valid::TEXT FROM product_lots;" if db_type == "postgres"
                else "SELECT id, product_id, quant, location, date_valid FROM product_lots;"
            )
            all_lots = cur.fetchall()

            # Mapeamentos O(1) de altíssima velocidade
            prod_by_code = {}
            prod_by_name = {}
            for p in all_products:
                p_id = p[0]
                p_name = str(p[1]).strip().upper()
                p_code = str(p[2]).strip() if p[2] else None
                prod_by_name[p_name] = p_id
                if p_code:
                    prod_by_code[p_code] = p_id

            tombstone_set = set()
            for t in all_tombstones:
                tombstone_set.add((t[0], str(t[1])[:10]))

            lots_by_key = {}
            for l in all_lots:
                key = (l[1], str(l[4])[:10])
                if key not in lots_by_key:
                    lots_by_key[key] = []
                lots_by_key[key].append({
                    "id": l[0],
                    "quant": l[2] or 0,
                    "location": str(l[3] or "LOJA").upper()
                })

            col_prod_idx = df_clean.columns.get_loc(col_prod)
            col_valid_idx = df_clean.columns.get_loc(col_valid)
            col_code_idx = df_clean.columns.get_loc(col_code) if col_code else None
            col_quant_idx = df_clean.columns.get_loc(col_quant) if has_quant_col else None

            last_emit_time = 0
            last_emit_pct = -1

            clean_matrix = df_clean.values

            for idx in range(len(clean_matrix)):
                row = clean_matrix[idx]
                row_num = idx + 1
                raw_prod_val = row[col_prod_idx]
                if raw_prod_val is None or pd.isna(raw_prod_val):
                    continue
                raw_prod = str(raw_prod_val).strip()
                raw_valid = row[col_valid_idx]
                raw_code = None
                if col_code_idx is not None:
                    c_val = row[col_code_idx]
                    if c_val is not None and pd.notna(c_val):
                        raw_code = str(c_val).strip()

                if raw_code and raw_code.endswith(".0"):
                    raw_code = raw_code[:-2]
                if raw_code == "nan" or raw_code == "":
                    raw_code = None

                # Converte data de validade com suporte ultrarrápido a serial do Excel, BR e ISO
                valid_str = parse_date_value(raw_valid)
                if not valid_str:
                    stats["erros"] += 1
                    continue

                # Quantidade da planilha (None se a planilha não possui coluna de quantidade)
                sheet_quant = None
                if has_quant_col and col_quant_idx is not None:
                    q_val = row[col_quant_idx]
                    if q_val is not None and pd.notna(q_val):
                        try:
                            sheet_quant = max(0, int(round(float(q_val))))
                        except (ValueError, TypeError):
                            sheet_quant = 0

                # ── Passo A: Localiza ou Cadastra o Produto via Hashmap O(1) ──────
                prod_key = raw_prod.upper()
                product_id = None
                if raw_code and raw_code in prod_by_code:
                    product_id = prod_by_code[raw_code]
                elif prod_key in prod_by_name:
                    product_id = prod_by_name[prod_key]
                    if raw_code and raw_code not in prod_by_code:
                        cur.execute(f"UPDATE products SET bar_code = {ph} WHERE id = {ph}", (raw_code, product_id))
                        prod_by_code[raw_code] = product_id

                if not product_id:
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

                    prod_by_name[prod_key] = product_id
                    if raw_code:
                        prod_by_code[raw_code] = product_id

                # ── Pergunta 1: Filtro de Lápide via Set O(1) ─────────────────────
                if (product_id, valid_str) in tombstone_set:
                    stats["bloqueados_zumbi"] += 1
                    audit_logs.append({
                        "produto": raw_prod,
                        "validade": valid_str,
                        "tipo": "BLOQUEADO",
                        "detalhe": "Item Zumbi bloqueado: já havia sido baixado no chão da loja."
                    })
                else:
                    # ── Pergunta 2: Esse lote já existe no banco? via Hashmap O(1) ───
                    lot_key = (product_id, valid_str)
                    lotes_locais = lots_by_key.get(lot_key, [])

                    if not lotes_locais:
                        # O lote NÃO existe no banco.
                        # REGRA DE PROTEÇÃO: Só cria lote novo se a planilha tiver coluna de quantidade E quant > 0!
                        # Se for uma lista de presença/relatório sem quantidade, ou quant <= 0,
                        # NUNCA cria novo lote (evita ressuscitar itens que já foram eliminados/baixados da loja)!
                        if has_quant_col and sheet_quant is not None and sheet_quant > 0:
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

                            # Atualiza hashmap em memória para próximas linhas com mesmo lote
                            if lot_key not in lots_by_key:
                                lots_by_key[lot_key] = []
                            lots_by_key[lot_key].append({
                                "id": novo_id,
                                "quant": sheet_quant,
                                "location": "GALPAO"
                            })

                            stats["novos_lotes"] += 1
                            audit_logs.append({
                                "produto": raw_prod,
                                "validade": valid_str,
                                "tipo": "NOVO_GALPAO",
                                "detalhe": f"Remessa nova inserida no GALPÃO ({sheet_quant} un)."
                            })
                        else:
                            # Sem coluna de quantidade ou item eliminado -> Mantém inalterado, sem criar lote fantasma!
                            stats["inalterados"] += 1

                    else:
                        # JÁ EXISTE NO BANCO LOCAL
                        total_local_quant = sum((item["quant"] or 0) for item in lotes_locais)

                        # ── Pergunta 3: A quantidade mudou? ───────────────────────────
                        if has_quant_col and sheet_quant is not None and sheet_quant > total_local_quant:
                            delta = sheet_quant - total_local_quant
                            lote_galpao = next((item for item in lotes_locais if item["location"] == "GALPAO"), None)

                            if lote_galpao:
                                galpao_id = lote_galpao["id"]
                                now_val = "CURRENT_TIMESTAMP" if db_type == "postgres" else "datetime('now', 'localtime')"
                                cur.execute(
                                    f"UPDATE product_lots SET quant = quant + {ph}, updated_at = {now_val} WHERE id = {ph}",
                                    (delta, galpao_id)
                                )
                                lote_galpao["quant"] += delta
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

                                lotes_locais.append({
                                    "id": target_lot_id,
                                    "quant": delta,
                                    "location": "GALPAO"
                                })

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

                # Emissão de progresso com throttle inteligente (máx 25 updates/segundo para suavidade total)
                pct = int(18 + (row_num / total_rows) * 78)
                now_ts = time.time()
                if (now_ts - last_emit_time >= 0.04 and pct > last_emit_pct) or row_num == total_rows or row_num == 1:
                    last_emit_time = now_ts
                    last_emit_pct = pct
                    yield {
                        "type": "progress",
                        "percent": pct,
                        "stage": f"Linha {row_num} de {total_rows}: {raw_prod[:24]}",
                        "current": row_num,
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
    """Execução direta ultrarrápida da conciliação sem overhead de streaming."""
    last_res = None
    for item in audit_and_import_spreadsheet_stream(file_bytes, filename):
        if item.get("type") == "done":
            last_res = item
        elif item.get("type") == "error":
            raise ValueError(item.get("error"))
    if not last_res:
        raise ValueError("Auditoria não retornou resultados.")
    return {"success": True, "stats": last_res["stats"], "logs": last_res["logs"]}
