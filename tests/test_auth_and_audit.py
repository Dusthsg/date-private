"""
Bateria de Testes Automatizados - Sistema de Login, Permissões e Auditoria Completa
DateLimit — Branch: feat/auth-audit
"""

import unittest
import os
import json
import uuid
import sqlite3
from app.run import app
from app.db import (
    SQLITE_PATH,
    get_user_by_username,
    get_user_by_pin,
    list_users,
    create_user,
    update_user,
    delete_user,
    log_audit,
    query_audit_logs
)


class TestAuthAndAudit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        admin = get_user_by_username("admin")
        if admin:
            update_user(admin["id"], name="Administrador", password="admin123", pin="1234", role="admin", active=True)
        else:
            create_user("admin", "Administrador", "admin123", pin="1234", role="admin")

    @classmethod
    def tearDownClass(cls):
        admin = get_user_by_username("admin")
        if admin:
            update_user(admin["id"], name="Administrador", password="admin123", pin="1234", role="admin", active=True)

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_01_default_admin_exists(self):
        """Verifica se o usuário default admin foi criado com sucesso."""
        admin = get_user_by_username("admin")
        self.assertIsNotNone(admin)
        self.assertEqual(admin["username"], "admin")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["pin"], "1234")

    def test_02_login_with_valid_and_invalid_credentials(self):
        """Testa o login com senha correta e incorreta."""
        # Senha incorreta
        res_fail = self.client.post("/login", data={
            "login_type": "password",
            "username": "admin",
            "password": "wrongpassword"
        })
        self.assertEqual(res_fail.status_code, 200)
        self.assertIn("incorretos", res_fail.data.decode("utf-8").lower())

        # Senha correta
        res_ok = self.client.post("/login", data={
            "login_type": "password",
            "username": "admin",
            "password": "admin123"
        })
        self.assertEqual(res_ok.status_code, 302)
        self.assertEqual(res_ok.headers.get("Location"), "/")

    def test_03_login_with_pin(self):
        """Testa o login rápido por PIN no terminal."""
        res_fail = self.client.post("/login", data={
            "login_type": "pin",
            "pin": "9999"
        })
        self.assertEqual(res_fail.status_code, 200)

        res = self.client.post("/login", data={
            "login_type": "pin",
            "pin": "1234"
        })
        self.assertEqual(res.status_code, 302)

    def test_04_create_and_delete_operator(self):
        """Cria e exclui um operador, validando unicidade."""
        u_name = f"op_{uuid.uuid4().hex[:6]}"
        ok, msg = create_user(u_name, "Operador Teste", "senha123", pin="4321", role="operador")
        self.assertTrue(ok)

        # Não deve permitir duplicidade
        ok_dup, _ = create_user(u_name, "Operador Teste 2", "senha123")
        self.assertFalse(ok_dup)

        user = get_user_by_username(u_name)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "operador")

        # Exclui
        ok_del, _ = delete_user(user["id"])
        self.assertTrue(ok_del)
        self.assertIsNone(get_user_by_username(u_name))

    def test_05_cannot_delete_last_admin(self):
        """Impede que o último admin seja excluído acidentalmente."""
        admin = get_user_by_username("admin")
        self.assertIsNotNone(admin)
        ok, msg = delete_user(admin["id"])
        self.assertFalse(ok)
        self.assertIn("único administrador", msg.lower())

    def test_06_audit_logging_and_query(self):
        """Testa gravação e recuperação de logs de auditoria."""
        log_audit("admin", "TEST_ACTION", "Ação de teste automatizado de auditoria", product_name="Produto Teste")
        total, logs = query_audit_logs(limit=10, action="TEST_ACTION")
        self.assertGreaterEqual(total, 1)
        self.assertEqual(logs[0]["action"], "TEST_ACTION")
        self.assertIn("teste automatizado", logs[0]["details"])

    def test_07_audit_endpoint_access_control(self):
        """Apenas administradores podem ver o endpoint de logs de auditoria."""
        # Sem login
        res_anon = self.client.get("/api/audit/logs")
        self.assertEqual(res_anon.status_code, 403)

        # Logado como operador comum
        with self.client.session_transaction() as sess:
            sess["user_id"] = 9999
            sess["role"] = "operador"
            sess["username"] = "operador_comum"

        res_op = self.client.get("/api/audit/logs")
        self.assertEqual(res_op.status_code, 403)

        # Logado como admin
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "admin"
            sess["username"] = "admin"

        res_admin = self.client.get("/api/audit/logs")
        self.assertEqual(res_admin.status_code, 200)
        data = res_admin.get_json()
        self.assertIn("logs", data)
        self.assertIn("total", data)

    def test_08_switch_operator_session(self):
        """Testa a rota de troca rápida de operador em chão de loja e mutação completa de sessão."""
        u_name = f"op_sw_{uuid.uuid4().hex[:6]}"
        create_user(u_name, "Operador Caixa", "123456", pin="7788", role="operador")
        u = get_user_by_username(u_name)
        self.assertIsNotNone(u)

        try:
            # 1. Troca rápida para o operador
            res = self.client.post("/api/operator/switch", json={"pin": "7788"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["operator_name"], "Operador Caixa")
            self.assertEqual(data["role"], "operador")
            self.assertEqual(data["user_id"], u["id"])

            # 2. Valida que a sessão Flask foi 100% sincronizada
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["user_id"], u["id"])
                self.assertEqual(sess["username"], u_name)
                self.assertEqual(sess["name"], "Operador Caixa")
                self.assertEqual(sess["role"], "operador")
                self.assertEqual(sess["active_operator"], "Operador Caixa")

            # 3. Operador não acessa /admin
            res_admin_blocked = self.client.get("/admin")
            self.assertEqual(res_admin_blocked.status_code, 403)

            # 4. Troca rápida para Administrador (PIN 1234)
            res_switch_admin = self.client.post("/api/operator/switch", json={"pin": "1234"})
            self.assertEqual(res_switch_admin.status_code, 200)
            data_adm = res_switch_admin.get_json()
            self.assertEqual(data_adm["role"], "admin")

            # 5. Valida que agora a sessão tem papel admin e acessa /admin com sucesso
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["role"], "admin")
                self.assertEqual(sess["name"], "Administrador")
            res_admin_ok = self.client.get("/admin")
            self.assertEqual(res_admin_ok.status_code, 200)

            # 6. Testa PIN inválido
            res_inv = self.client.post("/api/operator/switch", json={"pin": "0000"})
            self.assertEqual(res_inv.status_code, 401)
        finally:
            u_clean = get_user_by_username(u_name)
            if u_clean:
                delete_user(u_clean["id"])

    def test_09_admin_panel_page_access(self):
        """Testa o acesso à página dedicada do painel de administração (/admin)."""
        # Anônimo deve ser redirecionado para /login
        res_anon = self.client.get("/admin")
        self.assertEqual(res_anon.status_code, 302)
        self.assertEqual(res_anon.headers.get("Location"), "/login")

        # Operador comum deve receber 403 Proibido
        with self.client.session_transaction() as sess:
            sess["user_id"] = 999
            sess["role"] = "operador"
            sess["username"] = "operador_teste"
            sess["name"] = "Operador Teste"

        res_op = self.client.get("/admin")
        self.assertEqual(res_op.status_code, 403)

        # Admin deve conseguir carregar a página normalmente (200)
        admin = get_user_by_username("admin")
        with self.client.session_transaction() as sess:
            sess["user_id"] = admin["id"]
            sess["role"] = "admin"
            sess["username"] = "admin"
            sess["name"] = "Administrador"

        res_admin = self.client.get("/admin")
        self.assertEqual(res_admin.status_code, 200)
        content = res_admin.data.decode("utf-8")
        self.assertIn("Painel de Administração", content)
        self.assertIn("Meu Perfil de Administrador", content)
        self.assertIn("API Secret Token", content)
        self.assertIn("Usuários & Operadores de Loja", content)
        self.assertIn("Quem Mexeu", content)

    def test_10_admin_profile_update_and_token_generation(self):
        """Testa geração de token de API e atualização de perfil do administrador."""
        admin = get_user_by_username("admin")
        with self.client.session_transaction() as sess:
            sess["user_id"] = admin["id"]
            sess["role"] = "admin"
            sess["username"] = "admin"
            sess["name"] = "Administrador"

        # 1. Gera token
        res_tok = self.client.post("/api/admin/generate-token")
        self.assertEqual(res_tok.status_code, 200)
        tok_data = res_tok.get_json()
        self.assertTrue(tok_data["success"])
        self.assertTrue(tok_data["token"].startswith("dkl_live_"))
        new_token = tok_data["token"]

        # 2. Atualiza perfil
        res_profile = self.client.post("/api/admin/profile", json={
            "name": "Admin Gestor Master",
            "username": "admin",
            "pin": "9988",
            "api_token": new_token
        })
        self.assertEqual(res_profile.status_code, 200)
        p_data = res_profile.get_json()
        self.assertTrue(p_data["success"])

        # Verifica persistência no banco
        admin_db = get_user_by_username("admin")
        self.assertEqual(admin_db["name"], "Admin Gestor Master")
        self.assertEqual(admin_db["pin"], "9988")
        self.assertEqual(admin_db["api_token"], new_token)

        # Verifica se a sessão ativa foi atualizada
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("name"), "Admin Gestor Master")

        # Restaura admin padrão para não afetar próximas execuções
        update_user(admin_db["id"], name="Administrador", pin="1234")

    def test_11_role_manager_and_estimated_value_visibility(self):
        """Valida que 'Valor Estimado' é visível para admin/gerente e oculto para operador, e que cards/lista estão ativos."""
        # 1. Cria usuário gerente
        username_gerente = f"ger_{uuid.uuid4().hex[:6]}"
        ok, _ = create_user(username_gerente, "Gerente Geral", "gerente123", pin="4321", role="gerente")
        self.assertTrue(ok)
        ger_user = get_user_by_username(username_gerente)
        self.assertIsNotNone(ger_user)
        self.assertEqual(ger_user["role"], "gerente")

        # 2. Cria usuário operador
        username_op = f"op_{uuid.uuid4().hex[:6]}"
        ok_op, _ = create_user(username_op, "Operador Loja", "op123", pin="7788", role="operador")
        self.assertTrue(ok_op)
        op_user = get_user_by_username(username_op)
        self.assertIsNotNone(op_user)
        self.assertEqual(op_user["role"], "operador")

        # 3. Operador acessa index: Valor Estimado NÃO deve ser visível no resumo
        with self.client.session_transaction() as sess:
            sess["user_id"] = op_user["id"]
            sess["role"] = "operador"
            sess["username"] = op_user["username"]
            sess["name"] = op_user["name"]
        
        res_op = self.client.get("/")
        self.assertEqual(res_op.status_code, 200)
        html_op = res_op.data.decode("utf-8")
        self.assertNotIn('id="summary-card-valor"', html_op)

        # 4. Gerente acessa index: Valor Estimado DEVE ser visível
        with self.client.session_transaction() as sess:
            sess["user_id"] = ger_user["id"]
            sess["role"] = "gerente"
            sess["username"] = ger_user["username"]
            sess["name"] = ger_user["name"]

        res_ger = self.client.get("/")
        self.assertEqual(res_ger.status_code, 200)
        html_ger = res_ger.data.decode("utf-8")
        self.assertIn('id="summary-card-valor"', html_ger)
        self.assertIn("Valor Estimado", html_ger)

        # 5. Gerente acessa /admin: permitido e exibe Valor Estimado
        res_admin_ger = self.client.get("/admin")
        self.assertEqual(res_admin_ger.status_code, 200)
        html_admin = res_admin_ger.data.decode("utf-8")
        self.assertIn("Valor Estimado", html_admin)
        self.assertIn('id="stat-total-value"', html_admin)

        # 6. Verifica que botões de Lista e Cards estão presentes e claramente identificados
        self.assertIn('id="btn-view-table"', html_ger)
        self.assertIn('id="btn-view-cards"', html_ger)
        self.assertIn(">Lista</span>", html_ger)
        self.assertIn(">Cards</span>", html_ger)

        # 7. Verifica correção de sobreposição de cabeçalho (sem top: 53px sticky deslocado)
        self.assertNotIn("top: 53px;", html_ger)

        # Limpeza
        delete_user(ger_user["id"])
        delete_user(op_user["id"])


if __name__ == "__main__":
    unittest.main()
