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
    # Remove pontuações e caracteres especiais
    s = re.sub(r"[._\-/\\]+", " ", s).strip()
    return s


def find_column(df, candidates, custom_name=None):
    """
    Localiza a coluna do dataframe que combina com a lista de candidatos.
    Suporta nome customizado explícito, correspondência exata e contenção aproximada (fuzzy).
    """
    if custom_name and custom_name in df.columns:
        return custom_name

    norm_cols = {normalize_col_name(c): c for c in df.columns}

    # 1. Correspondência exata normalizada
    for cand in candidates:
        cand_norm = normalize_col_name(cand)
        if cand_norm in norm_cols:
            return norm_cols[cand_norm]

    # 2. Correspondência aproximada (subpalavra / substring)
    for cand in candidates:
        cand_norm = normalize_col_name(cand)
        if len(cand_norm) >= 3:
            for col_norm, original_col in norm_cols.items():
                if cand_norm == col_norm or f" {cand_norm} " in f" {col_norm} " or col_norm.startswith(cand_norm) or cand_norm.startswith(col_norm):
                    return original_col

    return None


PROD_CANDIDATES = [
    "Produto", "Descricao", "Nome", "Item", "Mercadoria", "Desc",
    "Descricao do Produto", "Nome do Produto", "Prod", "Produto Descricao",
    "Descricao Reduzida", "Descricao_Produto", "Desc_Produto", "Dsc_Produto",
    "Produto/Descricao", "Descricao Produto", "Ds_Produto", "Descr", "Mat_Desc",
    "Material", "Artigo", "Denominacao", "Txt_Produto", "Nome_Item"
]

VALID_CANDIDATES = [
    "Validade", "Vencimento", "Data Validade", "Dt Validade", "Data",
    "Vencto", "Dt Vencto", "Dt_Vencto", "Dt_Validade", "Data Vencimento",
    "Data_Vencimento", "Dt.Validade", "Dt.Vencimento", "Val", "Data_Val",
    "Data_Vencto", "Dt. Vencto", "D_Val", "DT_VAL", "Venc", "Dt_Venc",
    "Expiracao", "Dt Expiracao", "Dt_Expiracao", "Data Expiracao", "Valid",
    "Vcto", "Dt_Vcto", "Data_Vcto"
]

CODE_CANDIDATES = [
    "Codigo", "Cod", "EAN", "Codigo Barras", "Bar_code", "Cod Produto",
    "Cod_Produto", "Cod Barra", "Cod_Barra", "CodBar", "GTIN",
    "Codigo de Barras", "Plu", "Cod.Barra", "Cod_Barras", "SKU", "Ref",
    "Referencia", "Id_Produto", "Cod_Item", "Cód", "Cód.", "Cod_Prod"
]

QUANT_CANDIDATES = [
    "Quantidade", "Qtd", "Qtde", "Estoque", "Saldo", "Fisico", "Quant",
    "Qtd Estoque", "Saldo Atual", "Qtd_Estoque", "Qtd_Loja", "Loja",
    "Galpao", "Qtd_Fisica", "Saldo_Fisico", "Saldo_Estoque", "Qt_Estoque",
    "Estoque_Loja", "Qtd_Saldo", "Qtd_Disp", "Qtde_Estoque"
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


def parse_xml_spreadsheet(file_bytes: bytes) -> pd.DataFrame:
    """
    Parseia planilhas exportadas em XML Spreadsheet 2003
    (comum em ERPs como Totvs, Linx, SysPDV, Senior salvas com extensão .xls).
    """
    try:
        import xml.etree.ElementTree as ET
        # Suporta arquivos com declarações de encoding variadas (latin1, windows-1252, utf-8)
        try:
            root = ET.fromstring(file_bytes)
        except Exception:
            # Fallback para decode ignorando erros se houver caracteres inválidos
            text = file_bytes.decode("latin1", errors="replace")
            root = ET.fromstring(text.encode("utf-8"))

        rows = []
        for elem in root.iter():
            if elem.tag.endswith("Row"):
                row_vals = []
                col_idx = 1
                for cell in elem.iter():
                    if cell.tag.endswith("Cell"):
                        idx_attr = None
                        for k, v in cell.attrib.items():
                            if k.endswith("Index"):
                                try:
                                    idx_attr = int(v)
                                except (ValueError, TypeError):
                                    pass
                                break
                        if idx_attr and idx_attr > col_idx:
                            while col_idx < idx_attr:
                                row_vals.append("")
                                col_idx += 1
                        cell_text = ""
                        for data_elem in cell.iter():
                            if data_elem.tag.endswith("Data") and data_elem.text:
                                cell_text = str(data_elem.text).strip()
                                break
                        row_vals.append(cell_text)
                        col_idx += 1
                if any(bool(c) for c in row_vals):
                    rows.append(row_vals)

        if len(rows) > 0:
            max_cols = max(len(r) for r in rows)
            padded = [r + [""] * (max_cols - len(r)) for r in rows]
            headers = [str(h).strip() if str(h).strip() else f"Coluna_{i+1}" for i, h in enumerate(padded[0])]
            return pd.DataFrame(padded[1:], columns=headers)
    except Exception:
        pass
    return pd.DataFrame()


from html.parser import HTMLParser


class HTMLTableExtractor(HTMLParser):
    """Extrator nativo de tabelas HTML em Python puro (sem necessidade de lxml ou bs4)."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self.current_table = []
        self.current_row = []
        self.current_cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            self.current_table = []
        elif t == "tr":
            self.current_row = []
        elif t in ("td", "th"):
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th"):
            self.in_cell = False
            self.current_row.append("".join(self.current_cell).strip())
        elif t == "tr":
            if any(bool(c) for c in self.current_row):
                self.current_table.append(self.current_row)
        elif t == "table":
            if self.current_table:
                self.tables.append(self.current_table)

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def parse_html_table(file_bytes: bytes) -> pd.DataFrame:
    """Extrai tabelas HTML salvas com extensão .xls de sistemas legados de PDV/ERP."""
    parser = HTMLTableExtractor()
    for enc in ("utf-8", "latin1", "cp1252", "iso-8859-1"):
        try:
            text = file_bytes.decode(enc)
            parser.feed(text)
            break
        except Exception:
            continue

    if not parser.tables:
        return pd.DataFrame()

    rows = parser.tables[0]
    if len(rows) > 1:
        max_cols = max(len(r) for r in rows)
        padded = [r + [""] * (max_cols - len(r)) for r in rows]
        headers = [str(h).strip() if str(h).strip() else f"Coluna_{i+1}" for i, h in enumerate(padded[0])]
        return pd.DataFrame(padded[1:], columns=headers)
    elif len(rows) == 1:
        return pd.DataFrame(rows)
    return pd.DataFrame()


def deep_scan_headers(df: pd.DataFrame, candidates_prod, candidates_valid):
    """
    Varre as primeiras 25 linhas caso as colunas principais não estejam na primeira linha
    (muito comum em relatórios com cabeçalhos de empresa e data no topo).
    """
    col_p = find_column(df, candidates_prod)
    col_v = find_column(df, candidates_valid)
    if col_p and col_v:
        return df, col_p, col_v

    for i in range(min(25, len(df))):
        row_str = " ".join([normalize_col_name(v) for v in df.iloc[i].dropna().astype(str)])
        has_p = any(c in row_str for c in ["PRODUT", "DESCRIC", "NOME", "MERCAD", "ITEM", "PROD", "MAT", "DSC"])
        has_v = any(c in row_str for c in ["VALID", "VENCT", "VENCIM", "DATA", "VENC", "VAL", "EXPIR"])
        if has_p and has_v:
            new_headers = [str(c).strip() if str(c).strip() and str(c).strip().lower() != "nan" else f"Coluna_{j+1}" for j, c in enumerate(df.iloc[i])]
            df_candidate = df.iloc[i + 1:].copy().reset_index(drop=True)
            df_candidate.columns = new_headers
            c_p = find_column(df_candidate, candidates_prod)
            c_v = find_column(df_candidate, candidates_valid)
            if c_p and c_v:
                return df_candidate, c_p, c_v

    return df, col_p, col_v


def read_spreadsheet_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """
    Lê com tolerância universal e fallback em camadas:
    1. XML Spreadsheet 2003 (ERPs brasileiros com extensão .xls)
    2. HTML Table salvo como .xls (varre até 8KB de cabeçalho)
    3. BIFF8 .xls binário legado (com xlrd e leitura integral de linhas)
    4. OpenXML .xlsx moderno com openpyxl
    5. CSV / TSV delimitado com múltiplos separadores e encodings
    6. Fallbacks com pandas read_excel, read_html e read_csv engine python
    """
    if not file_bytes:
        return pd.DataFrame()

    preview_start = file_bytes[:8192].lower()

    # 1. XML Spreadsheet 2003 (começa com <?xml ou contém namespace do Office Spreadsheet)
    if b"<?xml" in preview_start or b"urn:schemas-microsoft-com:office:spreadsheet" in preview_start or b"<workbook" in preview_start:
        df_xml = parse_xml_spreadsheet(file_bytes)
        if not df_xml.empty and len(df_xml.columns) > 1:
            return df_xml

    # 2. Exportações de ERP em tabela HTML salvas como .xls
    if b"<html" in preview_start or b"<table" in preview_start or b"<!doctype" in preview_start or b"<tr" in preview_start:
        df_html = parse_html_table(file_bytes)
        if not df_html.empty and len(df_html.columns) > 1:
            return df_html
        try:
            tables = pd.read_html(io.BytesIO(file_bytes))
            if tables and len(tables[0]) > 0:
                return tables[0]
        except Exception:
            pass

    # 3. Magic bytes: Excel .xls binário legado (BIFF8: 0xD0CF11E0A1B11AE1)
    if file_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=file_bytes, on_demand=True)
            sheet = book.sheet_by_index(0)
            if sheet.nrows > 0:
                raw_rows = []
                for r in range(sheet.nrows):
                    raw_rows.append([sheet.cell_value(r, c) for c in range(sheet.ncols)])
                if len(raw_rows) > 1:
                    headers = [str(c).strip() if str(c).strip() else f"Coluna_{i+1}" for i, c in enumerate(raw_rows[0])]
                    return pd.DataFrame(raw_rows[1:], columns=headers)
        except Exception:
            pass
        # Fallback pd.read_excel para BIFF8
        try:
            return pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
        except Exception:
            pass

    # 4. Magic bytes: Excel .xlsx moderno (ZIP: PK\x03\x04)
    if file_bytes.startswith(b"PK\x03\x04"):
        try:
            return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        except Exception:
            pass

    # 5. CSV ou texto delimitado
    filename_lower = filename.lower()
    if filename_lower.endswith(".csv") or b"," in file_bytes[:1024] or b";" in file_bytes[:1024] or b"\t" in file_bytes[:1024]:
        for sep in [";", ",", "\t", "|"]:
            for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252", "iso-8859-1"]:
                try:
                    df = pd.read_csv(io.BytesIO(file_bytes), sep=sep, encoding=enc)
                    if len(df.columns) > 1 and len(df) > 0:
                        return df
                except Exception:
                    pass

    # 6. Fallback final resiliente com pandas
    try:
        return pd.read_excel(io.BytesIO(file_bytes))
    except Exception:
        pass
    try:
        tables = pd.read_html(io.BytesIO(file_bytes))
        if tables and len(tables[0]) > 0:
            return tables[0]
    except Exception:
        pass
    try:
        return pd.read_csv(io.BytesIO(file_bytes), sep=None, engine="python")
    except Exception:
        pass

    return pd.DataFrame()


def audit_and_import_spreadsheet_stream(
    file_bytes: bytes,
    filename: str,
    custom_col_prod: str = None,
    custom_col_valid: str = None,
    custom_col_code: str = None,
    custom_col_quant: str = None
):
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
        yield {"type": "error", "error": "A planilha enviada está vazia ou não contém dados legíveis."}
        return

    yield {
        "type": "progress",
        "percent": 10,
        "stage": "Identificando estrutura de colunas e cabeçalhos...",
        "current": 0,
        "total": len(df),
    }

    # 2. Varredura inteligente de cabeçalhos nas primeiras 25 linhas
    df, col_prod, col_valid = deep_scan_headers(df, PROD_CANDIDATES, VALID_CANDIDATES)

    # Aplica seleções manuais se fornecidas
    if custom_col_prod and custom_col_prod in df.columns:
        col_prod = custom_col_prod
    if custom_col_valid and custom_col_valid in df.columns:
        col_valid = custom_col_valid

    col_code = find_column(df, CODE_CANDIDATES, custom_col_code)
    col_quant = find_column(df, QUANT_CANDIDATES, custom_col_quant)
    has_quant_col = col_quant is not None

    # Se ainda faltar produto ou validade, emite evento de mapeamento manual em vez de falhar
    if not col_prod or not col_valid:
        cols_list = [str(c) for c in df.columns]
        sample_rows = []
        for _, r in df.head(5).iterrows():
            row_dict = {}
            for c in df.columns:
                val = r[c]
                row_dict[str(c)] = str(val) if pd.notna(val) else ""
            sample_rows.append(row_dict)

        yield {
            "type": "needs_mapping",
            "columns": cols_list,
            "preview": sample_rows,
            "suggestions": {
                "col_prod": col_prod or (cols_list[1] if len(cols_list) > 1 else cols_list[0]),
                "col_valid": col_valid or (cols_list[2] if len(cols_list) > 2 else (cols_list[1] if len(cols_list) > 1 else cols_list[0])),
                "col_code": col_code or (cols_list[0] if len(cols_list) > 0 else ""),
                "col_quant": col_quant or ""
            },
            "error": "Não conseguimos confirmar com 100% de precisão as colunas de Produto e Validade. Por favor, confirme o mapeamento abaixo."
        }
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


def audit_and_import_spreadsheet(
    file_bytes: bytes,
    filename: str,
    custom_col_prod: str = None,
    custom_col_valid: str = None,
    custom_col_code: str = None,
    custom_col_quant: str = None
) -> dict:
    """Execução direta ultrarrápida da conciliação com suporte a mapeamento de colunas."""
    last_res = None
    for item in audit_and_import_spreadsheet_stream(
        file_bytes,
        filename,
        custom_col_prod=custom_col_prod,
        custom_col_valid=custom_col_valid,
        custom_col_code=custom_col_code,
        custom_col_quant=custom_col_quant
    ):
        if item.get("type") == "done":
            last_res = item
        elif item.get("type") == "needs_mapping":
            return {
                "success": False,
                "needs_mapping": True,
                "columns": item.get("columns", []),
                "preview": item.get("preview", []),
                "suggestions": item.get("suggestions", {}),
                "error": item.get("error", "Confirmação de colunas necessária")
            }
        elif item.get("type") == "error":
            raise ValueError(item.get("error"))
    if not last_res:
        raise ValueError("Auditoria não retornou resultados.")
    return {"success": True, "stats": last_res["stats"], "logs": last_res["logs"]}
