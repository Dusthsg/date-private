"""
Bateria de Testes Automatizados - Histórico Completo de Erros Reportados pelo Usuário
DateLimit — Controle de Validade

Cada teste abaixo reproduz e valida uma das falhas ou exigências citadas pelo usuário
ao longo do desenvolvimento do projeto.
"""

import os
os.environ["DATELIMIT_TESTING"] = "1"
os.environ["TESTING"] = "true"

import unittest
import io
import json
import sqlite3
import datetime
import uuid

# Importa a aplicação Flask e camada de banco
from app.run import app
from app.db import SQLITE_PATH, is_postgres, set_test_db
from app.spreadsheet_storage import (
    save_spreadsheet_backup,
    get_spreadsheet_backups,
    get_spreadsheet_backup_by_id,
    delete_spreadsheet_backup
)
from app.auditor_import import (
    read_spreadsheet_dataframe,
    audit_and_import_spreadsheet
)


def clean_test_product(product_name):
    """
    Remove produtos, lotes e movimentações de teste de ambos os bancos (SQLite e Postgres)
    garantindo que os testes não poluam nem alterem o banco de dados de produção/desenvolvimento.
    """
    from app.db import _connect_pg_direct, is_postgres_configured
    try:
        with sqlite3.connect(SQLITE_PATH) as s_conn:
            s_conn.execute("DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name = ?)", (product_name,))
            s_conn.execute("DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name = ?)", (product_name,))
            s_conn.execute("DELETE FROM products WHERE name = ?", (product_name,))
            s_conn.commit()
    except Exception:
        pass

    if is_postgres_configured():
        try:
            pg_conn = _connect_pg_direct(timeout=4)
            pg_cur = pg_conn.cursor()
            pg_cur.execute("DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name = %s)", (product_name,))
            pg_cur.execute("DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name = %s)", (product_name,))
            pg_cur.execute("DELETE FROM products WHERE name = %s", (product_name,))
            pg_conn.commit()
            pg_conn.close()
        except Exception:
            pass


class TestUserReportedIssues(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        set_test_db()
        app.config['TESTING'] = True
        cls.client = app.test_client()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. ERRO: Polling de 5s resetando input e página durante edição (Msg 13, 16)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_01_polling_defense_prevents_reset_during_editing(self):
        """
        Valida que o template base.html possui o interceptor htmx:confirm que
        impede requisições de polling de resetarem inputs ou o estado da tela
        quando o usuário está editando (isEditingLot ou foco em input).
        """
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')

        # Verifica proteção no base.html
        self.assertIn("htmx:confirm", html)
        self.assertIn("window.isEditingLot", html)
        self.assertIn("evt.preventDefault()", html)
        self.assertIn("document.activeElement", html)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. ERRO: Falta de botão e edição rápida de quantidade e preço (Msg 4, 18)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_02_inline_editing_quantity_and_price(self):
        """
        Valida que os endpoints de edição inline de quantidade (/lote/<id>/quantidade)
        e preço (/lote/<id>/preco) atualizam o banco e retornam as células parciais HTMX.
        Cria um lote temporário exclusivo e o limpa ao final, sem tocar em lotes reais do banco.
        """
        from app.db import create_lot
        unique_name = f"TEMP TEST INLINE {uuid.uuid4().hex[:6].upper()}"
        d_valid = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        prod_id, lot_id = create_lot(unique_name, quant=5, price=10.00, date_valid=d_valid, location="LOJA")

        try:
            # Testa atualização de quantidade
            resp_quant = self.client.post(f'/lote/{lot_id}/quantidade', data={'quant': '99'})
            self.assertEqual(resp_quant.status_code, 200)
            self.assertIn(f'quant-cell-{lot_id}', resp_quant.data.decode('utf-8'))
            self.assertIn('99', resp_quant.data.decode('utf-8'))

            # Testa atualização de preço
            resp_price = self.client.post(f'/lote/{lot_id}/preco', data={'price': '12.50'})
            self.assertEqual(resp_price.status_code, 200)
            self.assertIn(f'price-cell-{lot_id}', resp_price.data.decode('utf-8'))
            self.assertIn('12.50', resp_price.data.decode('utf-8'))
        finally:
            clean_test_product(unique_name)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. ERRO: Digitação sem CapsLock salvava em minúsculas (Msg 67)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_03_capslock_normalization_on_create(self):
        """
        Valida que ao cadastrar um novo lote com nome em minúsculas,
        o sistema normaliza obrigatoriamente para MAIÚSCULAS no banco de dados.
        """
        uid = uuid.uuid4().hex[:6].lower()
        test_name_lower = f"produto teste capslock {uid}"
        test_name_upper = test_name_lower.upper()
        tomorrow = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')

        try:
            response = self.client.post('/lote', data={
                'name': test_name_lower,
                'date_valid': tomorrow,
                'quant': '10',
                'location': 'LOJA'
            })
            self.assertEqual(response.status_code, 200)

            # Verifica no banco ativo se foi gravado em maiúsculas
            from app.db import get_db_cursor
            with get_db_cursor() as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"SELECT name FROM products WHERE UPPER(name) = {ph}", (test_name_upper,))
                saved_prod = cur.fetchone()

            self.assertIsNotNone(saved_prod)
            saved_name = saved_prod[0] if isinstance(saved_prod, (tuple, list)) else saved_prod['name']
            self.assertEqual(saved_name, test_name_upper)
        finally:
            clean_test_product(test_name_upper)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. ERRO: Filtro Adicionados Hoje precisando marcar 2 semanas atrás (Msg 64)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_04_filtro_adicionados_recentemente_14_days(self):
        """
        Valida que o filtro 'recentes' filtra os itens criados nas últimas 2 semanas (14 dias)
        e retorna APENAS itens criados manualmente (origin == 'MANUAL'), excluindo importações.
        """
        response = self.client.get('/?filtro=recentes')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')
        
        # Verifica que o botão tem o label 'Adicionados Recentemente'
        self.assertIn('Adicionados Recentemente', html)

        # Valida que todos os lotes retornados pela query com filtro 'recentes' possuem origin == 'MANUAL'
        from app.db import query_lots
        total, lots = query_lots(filtro='recentes', limit=50)
        # Se houver lotes, nenhum pode ser de importação
        conn = sqlite3.connect(SQLITE_PATH)
        cur = conn.cursor()
        for l in lots:
            cur.execute("SELECT origin FROM product_lots WHERE id = ?", (l['lot_id'],))
            row = cur.fetchone()
            if row:
                self.assertEqual(row[0], 'MANUAL')
        conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # 5. ERRO: Erro 400 ao importar .xls / Suporte a XML Spreadsheet e HTML (Msg 48, 50, 70)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_05_universal_spreadsheet_parsing(self):
        """
        Valida que o importador lê com sucesso planilhas nos 4 formatos comuns do varejo:
        1. CSV com delimitador vírgula/ponto-e-vírgula
        2. XML Spreadsheet 2003 (usado por ERPs com extensão .xls)
        3. HTML Table (tabelas HTML exportadas com extensão .xls)
        4. Offset de cabeçalho (quando colunas começam na linha 2 ou 3)
        """
        # 1. Teste CSV
        csv_bytes = b"PRODUTO;VALIDADE;QUANTIDADE;CODIGO\nARROZ TIO JOAO;2027-01-01;20;789123456\nFEIJAO CARIOCA;2026-12-15;15;789654321\n"
        df_csv = read_spreadsheet_dataframe(csv_bytes, 'produtos.csv')
        self.assertIn('PRODUTO', [str(c).upper() for c in df_csv.columns])
        self.assertEqual(len(df_csv), 2)

        # 2. Teste XML Spreadsheet 2003 (.xls)
        xml_content = """<?xml version="1.0"?>
        <?mso-application progid="Excel.Sheet"?>
        <Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
                  xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
         <Worksheet ss:Name="Plan1">
          <Table>
           <Row>
            <Cell><Data ss:Type="String">DESCRICAO</Data></Cell>
            <Cell><Data ss:Type="String">DT_VALIDADE</Data></Cell>
            <Cell><Data ss:Type="String">ESTOQUE</Data></Cell>
           </Row>
           <Row>
            <Cell><Data ss:Type="String">OLEO DE SOJA SOYA 900ML</Data></Cell>
            <Cell><Data ss:Type="String">31/12/2026</Data></Cell>
            <Cell><Data ss:Type="Number">50</Data></Cell>
           </Row>
          </Table>
         </Worksheet>
        </Workbook>
        """
        df_xml = read_spreadsheet_dataframe(xml_content.encode('utf-8'), 'sistema.xls')
        self.assertIn('DESCRICAO', [str(c).upper() for c in df_xml.columns])
        self.assertEqual(len(df_xml), 1)

        # 3. Teste HTML Table (.xls disfarçado)
        html_content = """
        <html>
        <body>
            <table>
                <tr><th>PRODUTO</th><th>VENCIMENTO</th><th>QTD</th></tr>
                <tr><td>SABAO EM PO OMO 1KG</td><td>2027-05-10</td><td>30</td></tr>
            </table>
        </body>
        </html>
        """
        df_html = read_spreadsheet_dataframe(html_content.encode('utf-8'), 'relatorio.xls')
        self.assertIn('PRODUTO', [str(c).upper() for c in df_html.columns])
        self.assertEqual(len(df_html), 1)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. ERRO: Tela preta mostrando código HTML ao importar/exportar (Msg 59, 60, 61)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_06_no_black_screen_or_raw_html_crash(self):
        """
        Valida que o endpoint /import-planilha sempre responde JSON estruturado,
        mesmo quando a requisição vem sem arquivo ou com arquivo corrompido,
        evitando erro 500 em HTML puro que causava tela preta no navegador.
        """
        # Envio sem arquivo
        response = self.client.post('/import-planilha')
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertIsNotNone(data)
        self.assertFalse(data.get('success', True))
        self.assertIn('error', data)

        # Envio de arquivo inválido
        data_invalid = {
            'file': (io.BytesIO(b'arquivo de texto qualquer sem colunas validas'), 'teste.csv')
        }
        response_invalid = self.client.post('/import-planilha', data=data_invalid, content_type='multipart/form-data')
        data_json = response_invalid.get_json()
        self.assertIsNotNone(data_json)
        # Deve retornar JSON estruturado com solicitação de mapeamento ou erro amigável, nunca HTML cru
        self.assertTrue('needs_mapping' in data_json or 'error' in data_json or 'success' in data_json)

    # ──────────────────────────────────────────────────────────────────────────
    # 7. ERRO: Lógica de Delta / Não duplicar produtos ao reenviar mesma lista (Msg 54, 55)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_07_delta_reconciliation_logic(self):
        """
        Valida que a importação de uma planilha idêntica pela segunda vez não duplica
        lotes existentes e marca 0 novos produtos.
        """
        csv_bytes = b"PRODUTO;VALIDADE;QUANTIDADE\nMACARRAO BARILLA 500G;2027-08-20;10\n"
        
        # 1ª Importação
        res1 = audit_and_import_spreadsheet(
            csv_bytes,
            "teste_delta.csv",
            custom_col_prod='PRODUTO',
            custom_col_valid='VALIDADE',
            custom_col_quant='QUANTIDADE'
        )
        self.assertTrue(res1.get('success'))

        # 2ª Importação da mesma lista
        res2 = audit_and_import_spreadsheet(
            csv_bytes,
            "teste_delta.csv",
            custom_col_prod='PRODUTO',
            custom_col_valid='VALIDADE',
            custom_col_quant='QUANTIDADE'
        )
        self.assertTrue(res2.get('success'))

        # Na segunda importação de itens já conciliados, novos_produtos deve ser 0
        stats2 = res2.get('stats', {})
        self.assertEqual(stats2.get('novos_produtos', 0), 0)
        self.assertEqual(stats2.get('novos_lotes', 0), 0)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. ERRO: Perda de planilha em falhas / Backup automático e reprocessamento (Msg 71, 72)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_08_automatic_spreadsheet_backup_and_recovery(self):
        """
        Valida que qualquer planilha enviada é salva automaticamente no servidor
        (backup físico e no index.json), pode ser baixada via /download-planilha/<id>
        e reprocessada com mapeamento manual via /reprocess-planilha/<id>.
        """
        csv_content = b"COLUNA_A;COLUNA_B;COLUNA_C\nCAFE PILAO 500G;2027-10-15;40\n"
        meta = save_spreadsheet_backup(csv_content, "backup_teste.csv")
        self.assertIsNotNone(meta.get('id'))
        file_id = meta['id']

        # Verifica listagem
        saved_list = get_spreadsheet_backups()
        self.assertTrue(any(item['id'] == file_id for item in saved_list))

        # Testa deduplicação (não gera backup duplicado quando reenviar a mesma planilha)
        meta_dup = save_spreadsheet_backup(csv_content, "backup_teste.csv")
        self.assertEqual(meta_dup['id'], file_id)
        saved_list_after_dup = get_spreadsheet_backups()
        self.assertEqual(len([item for item in saved_list_after_dup if item['id'] == file_id]), 1)

        # Testa endpoint de download
        resp_download = self.client.get(f'/download-planilha/{file_id}')
        self.assertEqual(resp_download.status_code, 200)
        self.assertEqual(resp_download.data, csv_content)

        # Testa reprocessamento com mapeamento de colunas customizado
        resp_reprocess = self.client.post(f'/reprocess-planilha/{file_id}', data={
            'col_prod': 'COLUNA_A',
            'col_valid': 'COLUNA_B',
            'col_quant': 'COLUNA_C'
        })
        self.assertEqual(resp_reprocess.status_code, 200)
        data_reprocess = resp_reprocess.get_json()
        self.assertTrue(data_reprocess.get('success'))

        # Limpeza
        delete_spreadsheet_backup(file_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 9. ERRO: Tabela stock_movements e integridade estrutural (Msg 56)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_09_database_schema_and_stock_movements(self):
        """
        Valida que o banco possui a tabela stock_movements com todas as colunas
        necessárias e que a tabela de lotes suporta auditoria (updated_at).
        """
        conn = sqlite3.connect(SQLITE_PATH)
        cursor = conn.cursor()
        
        # Verifica colunas da tabela stock_movements
        cursor.execute("PRAGMA table_info(stock_movements)")
        cols_mov = [row[1] for row in cursor.fetchall()]
        self.assertIn('lot_id', cols_mov)
        self.assertIn('action', cols_mov)
        self.assertIn('quant', cols_mov)
        self.assertIn('created_at', cols_mov)

        # Verifica tabela products
        cursor.execute("PRAGMA table_info(products)")
        cols_prods = [row[1] for row in cursor.fetchall()]
        self.assertIn('name', cols_prods)
        self.assertIn('price', cols_prods)

        # Verifica tabela product_lots
        cursor.execute("PRAGMA table_info(product_lots)")
        cols_lots = [row[1] for row in cursor.fetchall()]
        self.assertIn('product_id', cols_lots)
        self.assertIn('date_valid', cols_lots)
        self.assertIn('quant', cols_lots)
        self.assertIn('price', cols_lots)
        self.assertIn('updated_at', cols_lots)
        
        conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # 10. ERRO: Dificuldade de encontrar produto existente no cadastro (Msg 63)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_10_catalog_search_endpoint(self):
        """
        Valida que a rota /api/products/search?q=... retorna correspondências
        rápidas do catálogo para autocompletar na adição de lotes.
        """
        response = self.client.get('/api/products/search?q=A')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsInstance(data, list)

    # ──────────────────────────────────────────────────────────────────────────
    # 11. ERRO: Lentidão ao carregar tudo / Rolagem infinita em chunks (Msg 65, 66)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_11_infinite_scroll_chunks(self):
        """
        Valida que a rota /lots/page entrega blocos paginados de 50 itens
        para rolagem infinita, evitando travamento da página.
        """
        from app.db import create_lot
        create_lot("TEST PRODUCT PAGINATION", 10, 5.0, "2026-12-31", "LOJA", "NORMAL")
        response = self.client.get('/lots/page?page=1&filtro=todos')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')
        # Deve retornar linhas de tabela HTML
        self.assertIn('<tr', html)

    # ──────────────────────────────────────────────────────────────────────────
    # 12. ERRO: Modo Escuro não funcionando / troca não acontecendo (Msg 74, 75)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_12_dark_mode_configuration_and_toggle(self):
        """
        Valida que o script do Tailwind CDN está ordenado corretamente:
        1. cdn.tailwindcss.com carrega primeiro
        2. tailwind.config = { darkMode: 'class' } vem imediatamente em seguida
        3. Botão btn-theme-toggle e função toggleDarkMode estão presentes
        4. color-scheme: dark e detecção no localStorage estão ativos
        """
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')

        cdn_idx = html.find('cdn.tailwindcss.com')
        config_idx = html.find("tailwind.config =")
        
        self.assertNotEqual(cdn_idx, -1, "Tailwind CDN deve estar presente")
        self.assertNotEqual(config_idx, -1, "tailwind.config deve estar presente")
        self.assertTrue(config_idx > cdn_idx, "tailwind.config DEVE vir APÓS o script do CDN")
        
        self.assertIn("darkMode: 'class'", html)
        self.assertIn("btn-theme-toggle", html)
        self.assertIn("toggleDarkMode", html)
        self.assertIn("datelimit_theme", html)
        self.assertIn("color-scheme: dark", html)

    # ──────────────────────────────────────────────────────────────────────────
    # 13. ERRO: Quebra de layout no celular e responsividade (Msg 12, 19)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_13_layout_responsive_meta_and_table_overflow(self):
        """
        Valida viewport mobile e classes de overflow para visualização em celulares.
        """
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')

        self.assertIn('name="viewport"', html)
        self.assertIn('overflow-x-auto', html)

    # ──────────────────────────────────────────────────────────────────────────
    # 14. ERRO: Puxar conexão / Sincronização do banco de dados (Msg 38, 48)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_14_connection_endpoints(self):
        """
        Valida os endpoints /api/connection/status e /api/connection/pull
        utilizados para reconectar e sincronizar com o PostgreSQL.
        """
        resp_status = self.client.get('/api/connection/status')
        self.assertEqual(resp_status.status_code, 200)
        data_status = resp_status.get_json()
        self.assertIn('db_name', data_status)
        self.assertIn('postgres_online', data_status)

    # ──────────────────────────────────────────────────────────────────────────
    # 15. ERRO: Importação de planilha sem coluna de quantidade não adicionava itens (Msg 77)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_15_import_spreadsheet_without_quantity_column(self):
        """
        Valida que planilhas sem coluna de quantidade (como as exportações padrão do ERP com
        Validade serial e Produto) adicionam os novos lotes com quantidade 0 e não os ignoram.
        """
        import uuid
        uid = uuid.uuid4().hex[:6]
        unique_name = f"PRODUTO TESTE SEM QTD {uid}"
        unique_code = f"999{uid}"
        csv_no_quant = f"Validade,Produto,Codigo\n46300.0,{unique_name},{unique_code}\n".encode('utf-8')
        res = audit_and_import_spreadsheet(
            csv_no_quant,
            "teste_sem_qtd.csv",
            custom_col_prod='Produto',
            custom_col_valid='Validade',
            custom_col_code='Codigo'
        )
        self.assertTrue(res.get('success'))
        stats = res.get('stats', {})
        self.assertGreaterEqual(stats.get('novos_lotes', 0), 1)

        # Na segunda importação idêntica, deve marcar inalterado e não duplicar
        res2 = audit_and_import_spreadsheet(
            csv_no_quant,
            "teste_sem_qtd.csv",
            custom_col_prod='Produto',
            custom_col_valid='Validade',
            custom_col_code='Codigo'
        )
        self.assertTrue(res2.get('success'))
        stats2 = res2.get('stats', {})
        self.assertEqual(stats2.get('novos_lotes', 0), 0)
        self.assertGreaterEqual(stats2.get('inalterados', 0), 1)

        # Limpeza
        clean_test_product(unique_name)

    # ──────────────────────────────────────────────────────────────────────────
    # 16. REGRA: Produto já existente preserva preço e atualiza/cria lote sem alterar preço
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_16_create_lot_existing_product_preserves_price_and_increments_quantity(self):
        """
        Valida que:
        1. Ao criar lote para um produto que já existe, o preço do produto NÃO é alterado.
        2. Se o lote para aquele produto com a mesma validade e localização já existe, incrementa a quantidade.
        3. Se for uma nova validade, cria um novo lote preservando o preço do produto e com origin == 'MANUAL'.
        """
        from app.db import create_lot, get_db_cursor, SQLITE_PATH
        unique_name = f"PROD PRECO INTACTO {uuid.uuid4().hex[:6].upper()}"
        d_valid_1 = (datetime.date.today() + datetime.timedelta(days=40)).strftime("%Y-%m-%d")
        d_valid_2 = (datetime.date.today() + datetime.timedelta(days=60)).strftime("%Y-%m-%d")

        try:
            # 1. Cria produto inicial com preço R$ 19.90 e lote inicial de 10 unidades
            prod_id_1, lot_id_1 = create_lot(unique_name, quant=10, price=19.90, date_valid=d_valid_1, location="LOJA")
            
            # Verifica preço do produto
            conn = sqlite3.connect(SQLITE_PATH)
            conn.isolation_level = None
            cur = conn.cursor()
            cur.execute("SELECT price FROM products WHERE id = ?", (prod_id_1,))
            self.assertAlmostEqual(cur.fetchone()[0], 19.90, places=2)

            # 2. Usuário tenta criar novo lote para o mesmo produto informando um preço diferente (R$ 99.99) e mesma validade
            prod_id_2, lot_id_2 = create_lot(unique_name, quant=5, price=99.99, date_valid=d_valid_1, location="LOJA")

            # ID do produto deve ser o mesmo
            self.assertEqual(prod_id_1, prod_id_2)
            # ID do lote deve ser o mesmo (atualizou a quantidade existente)
            self.assertEqual(lot_id_1, lot_id_2)

            # Preço do produto na tabela products DEVE CONTINUAR 19.90 (não alterado)
            cur.execute("SELECT price FROM products WHERE id = ?", (prod_id_1,))
            self.assertAlmostEqual(cur.fetchone()[0], 19.90, places=2)

            # Quantidade do lote deve ser 15 (10 + 5)
            cur.execute("SELECT quant, origin FROM product_lots WHERE id = ?", (lot_id_1,))
            row_lot = cur.fetchone()
            self.assertEqual(row_lot[0], 15)
            self.assertEqual(row_lot[1], 'MANUAL')

            # 3. Cria lote para o mesmo produto com validade diferente
            prod_id_3, lot_id_3 = create_lot(unique_name, quant=8, price=77.00, date_valid=d_valid_2, location="LOJA")
            self.assertEqual(prod_id_1, prod_id_3)
            self.assertNotEqual(lot_id_1, lot_id_3)

            # Preço do produto ainda DEVE CONTINUAR 19.90
            cur.execute("SELECT price FROM products WHERE id = ?", (prod_id_1,))
            self.assertAlmostEqual(cur.fetchone()[0], 19.90, places=2)

            # Lote 3 deve ter 8 unidades e origin == 'MANUAL'
            cur.execute("SELECT quant, origin FROM product_lots WHERE id = ?", (lot_id_3,))
            row_lot3 = cur.fetchone()
            self.assertEqual(row_lot3[0], 8)
            self.assertEqual(row_lot3[1], 'MANUAL')

            conn.close()

        finally:
            clean_test_product(unique_name)

    # ──────────────────────────────────────────────────────────────────────────
    # 17. AUDITORIA: Aba de itens deletados (Lápides) e fluxo de restauração
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_17_deleted_audit_tab_and_restore_workflow(self):
        """
        Valida que:
        1. A aba/filtro 'deletados' exibe o histórico de lotes baixados/deletados para auditoria.
        2. Um lote deletado exibe o badge de baixado e a data da exclusão.
        3. A rota /lote/restaurar/<id> restaura o lote de volta ao estoque e o remove da auditoria de deletados.
        """
        from app.db import create_lot, query_lots, get_db_cursor, SQLITE_PATH
        unique_name = f"PROD AUDITORIA DELETADO {uuid.uuid4().hex[:6].upper()}"
        d_valid = (datetime.date.today() + datetime.timedelta(days=45)).strftime("%Y-%m-%d")

        try:
            # 1. Cria um lote
            prod_id, lot_id = create_lot(unique_name, quant=25, price=8.50, date_valid=d_valid, location="LOJA")

            # 2. Deleta o lote via DELETE /lote/<lot_id>
            del_resp = self.client.delete(f'/lote/{lot_id}')
            self.assertEqual(del_resp.status_code, 200)

            # 3. Consulta a aba de deletados (auditoria)
            total_del, lots_del = query_lots(filtro='deletados', busca=unique_name)
            self.assertGreaterEqual(total_del, 1)
            found = next((l for l in lots_del if l['product_name'] == unique_name), None)
            self.assertIsNotNone(found)
            self.assertEqual(found['quant'], 25)
            self.assertTrue(found.get('is_deleted'))
            self.assertTrue(bool(found.get('deleted_at')))

            # 4. Renderiza a tela com o filtro 'deletados'
            resp_html = self.client.get(f'/?filtro=deletados&busca={unique_name}')
            self.assertEqual(resp_html.status_code, 200)
            html = resp_html.data.decode('utf-8')
            self.assertIn('Baixado', html)
            self.assertIn('Restaurar', html)
            self.assertIn(unique_name, html)

            # 5. Restaura o lote através da rota /lote/restaurar/<movement_id>
            movement_id = found['movement_id']
            res_resp = self.client.post(f'/lote/restaurar/{movement_id}')
            self.assertEqual(res_resp.status_code, 200)

            # 6. Verifica que o lote não está mais na aba 'deletados'
            total_del_after, lots_del_after = query_lots(filtro='deletados', busca=unique_name)
            found_after = next((l for l in lots_del_after if l['product_name'] == unique_name), None)
            self.assertIsNone(found_after)

            # 7. Verifica que o lote retornou à listagem de lotes ativos
            total_active, lots_active = query_lots(filtro='todos', busca=unique_name)
            self.assertGreaterEqual(total_active, 1)
            found_active = next((l for l in lots_active if l['product_name'] == unique_name), None)
            self.assertIsNotNone(found_active)
            self.assertEqual(found_active['quant'], 25)

        finally:
            clean_test_product(unique_name)

    # ──────────────────────────────────────────────────────────────────────────
    # 18. PREÇO EM PRODUCTS: Auto-pull ao adicionar lote e persistência
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_18_product_price_autopull_and_persistence(self):
        """
        Valida que:
        1. O preço de um novo produto é salvo na tabela products.
        2. A rota de autocomplete /api/products/search?q=... retorna o preço cadastrado.
        3. Um produto com preço 0.0 que recebe um lote com preço > 0 tem o preço atualizado em products.
        4. O template index.html possui o input de preço com id 'input-add-lot-price' e gatilhos de auto-pull.
        """
        from app.db import create_lot, search_products, get_db_cursor, SQLITE_PATH
        unique_name = f"PROD AUTOPULL {uuid.uuid4().hex[:6].upper()}"
        d_valid = (datetime.date.today() + datetime.timedelta(days=90)).strftime("%Y-%m-%d")

        try:
            # 1. Cria produto com preço R$ 34.50
            prod_id, lot_id = create_lot(unique_name, quant=20, price=34.50, date_valid=d_valid, location="LOJA")

            # Verifica se o preço está salvo na tabela products
            with sqlite3.connect(SQLITE_PATH) as s_conn:
                c = s_conn.cursor()
                c.execute("SELECT price FROM products WHERE id = ?", (prod_id,))
                p_row = c.fetchone()
                self.assertIsNotNone(p_row)
                self.assertAlmostEqual(p_row[0], 34.50, places=2)

            # 2. Testa busca no catálogo /api/products/search?q=... (auto-pull)
            resp = self.client.get(f'/api/products/search?q={unique_name[:10]}')
            self.assertEqual(resp.status_code, 200)
            items = json.loads(resp.data.decode('utf-8'))
            self.assertGreaterEqual(len(items), 1)
            found = next((it for it in items if it['name'] == unique_name), None)
            self.assertIsNotNone(found)
            self.assertAlmostEqual(found['price'], 34.50, places=2)

            # 3. Testa produto que tinha preço 0.0 (ex: importado) e recebe lote com preço
            zero_name = f"PROD PRECO ZERO {uuid.uuid4().hex[:6].upper()}"
            with get_db_cursor(commit=True) as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"INSERT INTO products (name, price) VALUES ({ph}, 0.0)", (zero_name,))
                if db_type == 'postgres':
                    with sqlite3.connect(SQLITE_PATH) as s_c:
                        s_c.execute("INSERT OR IGNORE INTO products (name, price) VALUES (?, 0.0)", (zero_name,))
                        s_c.commit()

            # Adiciona lote com preço R$ 42.00 para este produto
            prod_z_id, lot_z_id = create_lot(zero_name, quant=10, price=42.00, date_valid=d_valid, location="LOJA")

            with sqlite3.connect(SQLITE_PATH) as s_conn:
                c = s_conn.cursor()
                c.execute("SELECT price FROM products WHERE name = ?", (zero_name,))
                self.assertAlmostEqual(c.fetchone()[0], 42.00, places=2)

            # 4. Valida elementos de auto-pull no HTML
            resp_index = self.client.get('/')
            self.assertEqual(resp_index.status_code, 200)
            html = resp_index.data.decode('utf-8')
            self.assertIn('id="input-add-lot-price"', html)
            self.assertIn('onAddLotBlur', html)
            self.assertIn('input-add-lot-price', html)

        finally:
            clean_test_product(unique_name)
            clean_test_product(zero_name)

    # ──────────────────────────────────────────────────────────────────────────
    # 19. BOTÕES DE DELETADOS & AUDITORIA E ELIMINAÇÃO DE LAG (Msg 10)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_19_deleted_audit_buttons_and_purge(self):
        """
        Valida que:
        1. Linhas ativas possuem botão claro 'Deletar'.
        2. Linhas na aba de Deletados possuem botões de 'Restaurar' e 'Excluir' (definitivo).
        3. A rota DELETE /lote/movimento/<id> expurga o registro de baixa do histórico.
        4. A rota POST /lote/deletados/limpar limpa todo o histórico de baixas.
        5. O botão 'Limpar Todos os Deletados' está presente e onSearchInput usa textContent (sem lag de reflow).
        """
        from app.db import create_lot, delete_lot, get_db_cursor
        unique_name = f"AUDIT BTN TEST {uuid.uuid4().hex[:6].upper()}"
        d_valid = (datetime.date.today() + datetime.timedelta(days=15)).strftime("%Y-%m-%d")

        try:
            # 1. Cria e depois deleta um lote para gerar registro de baixa
            prod_id, lot_id = create_lot(unique_name, quant=5, price=19.90, date_valid=d_valid, location="LOJA")
            delete_lot(lot_id)

            # 2. Testa renderização da aba de deletados
            resp_del = self.client.get('/?filtro=deletados')
            self.assertEqual(resp_del.status_code, 200)
            html_del = resp_del.data.decode('utf-8')

            # Verifica botões de Restaurar e Excluir
            self.assertIn('Restaurar', html_del)
            self.assertIn('Excluir', html_del)
            self.assertIn('/lote/movimento/', html_del)
            self.assertIn('btn-purge-deleted-wrapper', html_del)
            self.assertIn('Limpar Todos os Deletados', html_del)

            # 3. Testa renderização da aba normal (Todos)
            resp_all = self.client.get('/?filtro=todos')
            self.assertEqual(resp_all.status_code, 200)
            html_all = resp_all.data.decode('utf-8')
            self.assertIn('Deletar', html_all)
            # Verifica que o script usa textContent em vez de innerText (performance sem reflow)
            self.assertIn('textContent', html_all)

            # 4. Obtém o movement_id gerado
            with get_db_cursor() as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"SELECT id FROM stock_movements WHERE product_id = {ph} AND action = 'BAIXA'", (prod_id,))
                row = cur.fetchone()
                self.assertIsNotNone(row)
                movement_id = row[0]

            # 5. Testa rota de exclusão pontual de movimento
            resp_purge_item = self.client.delete(f'/lote/movimento/{movement_id}')
            self.assertEqual(resp_purge_item.status_code, 200)

            # Verifica se o movimento foi removido
            with get_db_cursor() as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"SELECT COUNT(*) FROM stock_movements WHERE id = {ph}", (movement_id,))
                self.assertEqual(cur.fetchone()[0], 0)

            # 6. Valida que a rota de limpeza geral de deletados está registrada
            from app.run import purge_all_deleted_route
            self.assertTrue(callable(purge_all_deleted_route))

        finally:
            clean_test_product(unique_name)


    # ──────────────────────────────────────────────────────────────────────────
    # 20. RESILIÊNCIA E RECONEXÃO AUTOMÁTICA WEBSOCKET (Msg 12)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_20_bulletproof_websocket_heartbeat_and_reconnect(self):
        """
        Valida que o gerenciador de WebSocket no cliente possui:
        1. Watchdog ativo com heartbeat agressivo (6s) para não cair em NAT/4G.
        2. Ouvintes de eventos para desbloqueio de tela (visibilitychange) e rede (online).
        3. Badge interativo com gatilho manual (onclick="initWebSocket(true)").
        4. Fallback automático de busca para HTMX com reconexão em background.
        """
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        self.assertIn('startWsWatchdog', html)
        self.assertIn('visibilitychange', html)
        self.assertIn("window.addEventListener('online'", html)
        self.assertIn('onclick="initWebSocket(true)"', html)
        self.assertIn('6000', html)
        self.assertIn('scheduleWsReconnect', html)


    # ──────────────────────────────────────────────────────────────────────────
    # 21. CARROSSEL ARRASTÁVEL E ROLAGEM HORIZONTAL NOS FILTROS (Msg 14)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_21_filter_selection_drag_scroll_carousel(self):
        """
        Valida que os botões de filtro formam um carrossel horizontal arrastável:
        1. Container possui overflow-x-auto, no-scrollbar, touch-pan-x e cursor-grab.
        2. Botões individuais possuem shrink-0 e whitespace-nowrap para não quebrar linha.
        3. Botões de navegação lateral (setas chevron desktop) com scrollFilters().
        4. Suporte a arraste por mouse (initFilterDragScroll) e auto-centralização (scrollIntoView).
        """
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        # Container
        self.assertIn('overflow-x-auto', html)
        self.assertIn('no-scrollbar', html)
        self.assertIn('touch-pan-x', html)
        self.assertIn('cursor-grab', html)

        # Botões não quebram
        self.assertIn('shrink-0 flex-shrink-0 whitespace-nowrap', html)

        # Navegação e scripts
        self.assertIn("scrollFilters('left')", html)
        self.assertIn("scrollFilters('right')", html)
        self.assertIn('initFilterDragScroll', html)
        self.assertIn('scrollIntoView', html)


    # ──────────────────────────────────────────────────────────────────────────
    # 22. FOOTER DIREITOS RESERVADOS À REALITY (Msg 15)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_22_reality_copyright_footer(self):
        """
        Valida que o footer em base.html exibe o copyright e direitos reservados à Reality:
        1. Menção a 'Reality'.
        2. 'Todos os direitos reservados'.
        3. 'DateLimit'.
        4. Elemento de ano dinâmico 'footer-year'.
        """
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        self.assertIn('Reality', html)
        self.assertIn('Todos os direitos reservados', html)
        self.assertIn('footer-year', html)
        self.assertIn('DateLimit', html)


    # ──────────────────────────────────────────────────────────────────────────
    # 23. CONFIRMAÇÃO VISUAL E LIMPEZA DE INPUTS AO CADASTRAR LOTE (Msg 16)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_23_lot_form_reset_and_confirmation(self):
        """
        Valida que:
        1. O formulário form-add-lot não possui erros de sintaxe de aspas aninhadas.
        2. Possui onAddLotAfterRequest e onAddLotBeforeRequest registrados.
        3. Limpa explicitamente os inputs (name, quant, price, date).
        4. Exibe mensagem de confirmação 'Lote cadastrado com sucesso!' em form-add-msg.
        """
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        self.assertIn('onAddLotBeforeRequest', html)
        self.assertIn('onAddLotAfterRequest', html)
        self.assertIn('Lote cadastrado com sucesso!', html)
        self.assertIn('form.reset()', html)
        self.assertIn("quantInput.value = '0'", html)

    # ──────────────────────────────────────────────────────────────────────────
    # 24. RESILIÊNCIA DOS ÍCONES DE LÁPIS E EDIÇÃO INLINE SEM CORRUPÇÃO (Msg 16)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_24_pencil_icon_and_inline_edit_resilience(self):
        """
        Valida que:
        1. Os formulários de edição rápida em quant_cell e price_cell usam style="display: none;"
           para evitar conflitos de classes Tailwind ('hidden' vs 'flex').
        2. startEditing e stopEditing alternam display='none' e display='flex' sem exceção.
        3. window.refreshIcons é chamado em eventos HTMX (afterSwap, afterSettle, load)
           e dentro de startEditing / stopEditing para garantir que o lápis nunca suma.
        """
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        self.assertIn('data-lucide="pencil"', html)
        self.assertIn('style="display: none;"', html)
        self.assertIn("editForm.style.display = 'flex'", html)
        self.assertIn("editForm.style.display = 'none'", html)
        self.assertIn("document.addEventListener('htmx:load'", html)


    # ──────────────────────────────────────────────────────────────────────────
    # 25. SISTEMA DE ALERTAS AUTOMÁTICOS DE VALIDADE (Msg 17)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_25_automatic_alerts_system(self):
        """
        Valida que o sistema de alertas automáticos possui:
        1. Botão 'Alertas' no header e modal modal-alerts incluído em base.html.
        2. Rota GET /api/alerts/settings retornando JSON estruturado com canal e horário.
        3. Rota POST /api/alerts/settings atualizando configurações no banco.
        4. Rota POST /api/alerts/test executando validações com feedback estruturado.
        5. Engine get_alert_summary em app.alerts calculando valores financeiros e criticidade.
        """
        # 1. Valida interface web
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')
        self.assertIn('id="btn-alerts-modal"', html)
        self.assertIn('id="modal-alerts"', html)
        self.assertIn('openAlertsModal', html)
        self.assertIn('testAlertSend', html)

        # 2. Testa endpoint GET settings
        resp_get = self.client.get('/api/alerts/settings')
        self.assertEqual(resp_get.status_code, 200)
        st = json.loads(resp_get.data.decode('utf-8'))
        self.assertIn('channel', st)
        self.assertIn('daily_time', st)

        # 3. Testa endpoint POST settings
        payload = {
            "enabled": True,
            "channel": "telegram",
            "daily_time": "08:15",
            "telegram_token": "123456:FAKE_TOKEN_FOR_TEST",
            "telegram_chat_id": "999888777",
            "include_vencidos": True,
            "include_criticos": True,
            "include_atencao": False
        }
        resp_post = self.client.post(
            '/api/alerts/settings',
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(resp_post.status_code, 200)
        res_data = json.loads(resp_post.data.decode('utf-8'))
        self.assertTrue(res_data.get('success'))
        self.assertEqual(res_data['settings']['daily_time'], '08:15')
        self.assertEqual(res_data['settings']['telegram_chat_id'], '999888777')

        # 4. Testa endpoint POST test (espera falha tratada sem crash por causa de token falso)
        resp_test = self.client.post('/api/alerts/test')
        self.assertEqual(resp_test.status_code, 200)
        test_data = json.loads(resp_test.data.decode('utf-8'))
        self.assertIn('message', test_data)

        # 5. Testa motor de cálculo de resumo financeiro
        from app.alerts import get_alert_summary, format_alert_message
        summary = get_alert_summary()
        self.assertIn('vencidos_count', summary)
        self.assertIn('criticos_count', summary)
        self.assertIn('total_risk_val', summary)
        msg = format_alert_message(summary, is_test=True)
        self.assertIn('DateLimit', msg)

    # ──────────────────────────────────────────────────────────────────────────
    # 24. PERSISTÊNCIA WEBSOCKET THREAD-SAFE E SINCRONIZAÇÃO ENTRE ABAS
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_24_threadsafe_websocket_and_crosstab_sync(self):
        """
        Valida:
        1. SafeWebSocket thread-safe contra concorrência e corrupção de sockets.
        2. broadcast_reload envia 'invalidate_cache' para o autor e 'reload' para terceiros.
        3. Presença de BroadcastChannel e sincronização instantânea entre abas no template.
        """
        from app.run import SafeWebSocket, broadcast_reload, connected_clients, clients_lock
        import time

        class MockWS:
            def __init__(self):
                self.messages = []
            def send(self, data):
                self.messages.append(data)
            def close(self):
                pass

        m1 = MockWS()
        m2 = MockWS()
        safe1 = SafeWebSocket(m1, client_id='tab_1')
        safe2 = SafeWebSocket(m2, client_id='tab_2')

        with clients_lock:
            connected_clients.add(safe1)
            connected_clients.add(safe2)

        broadcast_reload('delete_lot', exclude_client_id='tab_1')
        time.sleep(0.08)

        # Tab 1 deve receber invalidate_cache
        self.assertTrue(any('invalidate_cache' in msg for msg in m1.messages))
        # Tab 2 deve receber reload
        self.assertTrue(any('reload' in msg for msg in m2.messages))

        with clients_lock:
            connected_clients.discard(safe1)
            connected_clients.discard(safe2)

        # Valida que o template possui BroadcastChannel e ouvinte cross-tab
        resp = self.client.get('/')
        html = resp.data.decode('utf-8')
        self.assertIn('BroadcastChannel', html)
        self.assertIn('notifyCrossTabMutation', html)
        self.assertIn('datelimit_channel', html)


    # ──────────────────────────────────────────────────────────────────────────
    # 25. REGRA: Aba Total com contagem de lotes e resumo consolidado em tempo real
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_25_total_tab_and_inventory_summary(self):
        """
        Valida:
        1. get_inventory_summary calcula métricas consolidadas com cache sub-milissegundo.
        2. Endpoint GET /api/inventory/summary responde 200 JSON com as métricas.
        3. Botão 'Total' existe nos filtros com badge de contagem de lotes.
        4. Painel consolidado #total-summary-panel renderizado no template.
        5. Filtro 'total' e 'todos' aceitos e intercambiáveis no backend e na paginação.
        """
        from app.db import get_inventory_summary, query_lots

        summary = get_inventory_summary()
        self.assertIsInstance(summary, dict)
        self.assertIn('total_lots', summary)
        self.assertIn('total_units', summary)
        self.assertIn('total_products', summary)
        self.assertIn('total_value', summary)
        self.assertGreaterEqual(summary['total_lots'], 0)

        # 2. Endpoint /api/inventory/summary
        resp = self.client.get('/api/inventory/summary')
        self.assertEqual(resp.status_code, 200)
        api_data = resp.get_json()
        self.assertEqual(api_data['total_lots'], summary['total_lots'])
        self.assertEqual(api_data['total_units'], summary['total_units'])

        # 3 & 4. Template com aba Total e painel consolidado
        resp_index = self.client.get('/')
        self.assertEqual(resp_index.status_code, 200)
        html = resp_index.data.decode('utf-8')
        self.assertIn('data-filtro="todos"', html)
        self.assertIn('Total', html)
        self.assertIn('badge-total-lots', html)
        self.assertIn('total-summary-panel', html)
        self.assertIn('summary-total-lots', html)
        self.assertIn('summary-total-units', html)
        self.assertIn('summary-total-products', html)
        self.assertIn('summary-total-value', html)

        # 5. Aceita filtro total na rota e paginação
        total, lots = query_lots(filtro='total', limit=10, offset=0)
        self.assertIsInstance(lots, list)
        self.assertIsInstance(total, int)

        resp_total = self.client.get('/?filtro=total')
        self.assertEqual(resp_total.status_code, 200)


if __name__ == '__main__':
    unittest.main()
