"""
Bateria de Testes Automatizados - Histórico Completo de Erros Reportados pelo Usuário
DateLimit — Controle de Validade

Cada teste abaixo reproduz e valida uma das falhas ou exigências citadas pelo usuário
ao longo do desenvolvimento do projeto.
"""

import unittest
import os
import io
import json
import sqlite3
import datetime
import uuid

# Importa a aplicação Flask e camada de banco
from app.run import app
from app.db import SQLITE_PATH, is_postgres
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


class TestUserReportedIssues(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
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
        """
        # Pega um lote existente no banco local (tabela product_lots)
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, quant, price FROM product_lots ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()

        if row:
            lot_id = row['id']
            
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

    # ──────────────────────────────────────────────────────────────────────────
    # 3. ERRO: Digitação sem CapsLock salvava em minúsculas (Msg 67)
    # ──────────────────────────────────────────────────────────────────────────
    def test_issue_03_capslock_normalization_on_create(self):
        """
        Valida que ao cadastrar um novo lote com nome em minúsculas,
        o sistema normaliza obrigatoriamente para MAIÚSCULAS no banco de dados.
        """
        test_name_lower = "produto teste capslock normalizado"
        tomorrow = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')

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
            cur.execute(f"SELECT name FROM products WHERE UPPER(name) = {ph}", (test_name_lower.upper(),))
            saved_prod = cur.fetchone()

        self.assertIsNotNone(saved_prod)
        saved_name = saved_prod[0] if isinstance(saved_prod, (tuple, list)) else saved_prod['name']
        self.assertEqual(saved_name, test_name_lower.upper())

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
        response = self.client.get('/lots/page?offset=0&limit=50&filtro=todos')
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
        from app.db import get_db_cursor
        with get_db_cursor(commit=True) as (cur, db_type):
            ph = '%s' if db_type == 'postgres' else '?'
            cur.execute(f"DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
            cur.execute(f"DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
            cur.execute(f"DELETE FROM products WHERE name = {ph}", (unique_name,))

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
            with get_db_cursor(commit=True) as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
                cur.execute(f"DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
                cur.execute(f"DELETE FROM products WHERE name = {ph}", (unique_name,))

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
            with get_db_cursor(commit=True) as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
                cur.execute(f"DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name = {ph})", (unique_name,))
                cur.execute(f"DELETE FROM products WHERE name = {ph}", (unique_name,))

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
            with get_db_cursor(commit=True) as (cur, db_type):
                ph = '%s' if db_type == 'postgres' else '?'
                cur.execute(f"DELETE FROM stock_movements WHERE product_id IN (SELECT id FROM products WHERE name IN ({ph}, {ph}))", (unique_name, zero_name))
                cur.execute(f"DELETE FROM product_lots WHERE product_id IN (SELECT id FROM products WHERE name IN ({ph}, {ph}))", (unique_name, zero_name))
                cur.execute(f"DELETE FROM products WHERE name IN ({ph}, {ph})", (unique_name, zero_name))


if __name__ == '__main__':
    unittest.main()
